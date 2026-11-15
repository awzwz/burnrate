"""Events between services: transactional outbox, at-least-once delivery, idempotent consumers.

Writing:   `await emit(s, "cards", "card.ledger.changed", {...})` inside the same DB transaction as the data.
Reading:   `@subscribe("card.ledger.changed", worker="plan-recalc", service="plan")` on an async handler.

Local mode: a relay loop in the single process reads every outbox and calls the handlers directly.
Cluster:    each service relays its own outbox to Kafka; worker deployments consume their topics.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from burnrate.common import db
from burnrate.common.config import settings
from burnrate.common.context import Ctx, get_ctx, real_now, reset_ctx, set_ctx

log = logging.getLogger("burnrate.bus")
MAX_ATTEMPTS = 8


@dataclass(frozen=True)
class Event:
    id: str
    topic: str
    key: str
    payload: dict
    headers: dict = field(default_factory=dict)
    created_at: dt.datetime | None = None


Handler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True)
class Subscription:
    topic: str
    worker: str  # deployment that runs the handler in the cluster; also the consumer group
    service: str  # service whose database keeps the processed-events marks
    handler: Handler


_subs: list[Subscription] = []


def subscribe(topic: str, *, worker: str, service: str):
    def deco(fn: Handler) -> Handler:
        _subs.append(Subscription(topic, worker, service, fn))
        return fn

    return deco


def subscriptions(worker: str | None = None) -> list[Subscription]:
    return [s for s in _subs if worker is None or s.worker == worker]


async def emit(s: AsyncSession, service: str, topic: str, payload: dict, *, key: str = "") -> str:
    """Add an outbox row to the caller's transaction. Returns the event id."""
    row = db.base_of(service).Outbox(
        id=db.new_id(), topic=topic, key=key or get_ctx().user_id, payload=payload,
        headers=get_ctx().headers(), created_at=real_now(),
    )
    s.add(row)
    return row.id


async def _handle(sub: Subscription, ev: Event) -> None:
    """Run one handler once per (worker, event id), under the context the event was written in."""
    Processed = db.base_of(sub.service).Processed
    async with db.session(sub.service) as s:
        if await s.get(Processed, (sub.worker, ev.id)):
            return
    token = set_ctx(Ctx.from_headers(ev.headers))
    try:
        await sub.handler(ev)
    finally:
        reset_ctx(token)
    async with db.session(sub.service) as s:
        s.add(Processed(consumer=sub.worker, event_id=ev.id, at=real_now()))


async def dispatch(ev: Event, worker: str | None = None) -> None:
    for sub in subscriptions(worker):
        if sub.topic == ev.topic:
            await _handle(sub, ev)


# ---------- local relay: outbox -> handlers, in process ----------

_drain_lock = asyncio.Lock()


async def _pending(service: str, limit: int = 200) -> list[Event]:
    Outbox = db.base_of(service).Outbox
    async with db.session(service) as s:
        rows = (await s.execute(
            select(Outbox).where(Outbox.published_at.is_(None), Outbox.attempts < MAX_ATTEMPTS)
            .order_by(Outbox.created_at, Outbox.id).limit(limit)
        )).scalars().all()
        return [Event(r.id, r.topic, r.key, r.payload, r.headers or {}, r.created_at) for r in rows]


async def _mark(service: str, event_id: str, ok: bool) -> None:
    Outbox = db.base_of(service).Outbox
    async with db.session(service) as s:
        values = {"published_at": real_now()} if ok else {"attempts": Outbox.attempts + 1}
        await s.execute(update(Outbox).where(Outbox.id == event_id).values(**values))


async def drain(max_rounds: int = 100) -> int:
    """Deliver everything that is waiting, including events produced by handlers. Returns events delivered."""
    delivered = 0
    async with _drain_lock:
        for _ in range(max_rounds):
            batch: list[tuple[str, Event]] = []
            for service in db.known_services():
                batch += [(service, ev) for ev in await _pending(service)]
            if not batch:
                break
            batch.sort(key=lambda x: (x[1].created_at, x[1].id))
            for service, ev in batch:
                try:
                    await dispatch(ev)
                    await _mark(service, ev.id, True)
                    delivered += 1
                except Exception:  # a failing handler must not stop the relay
                    log.exception("handler failed topic=%s id=%s", ev.topic, ev.id)
                    await _mark(service, ev.id, False)
    return delivered


async def local_relay_loop(interval: float = 0.2) -> None:
    while True:
        try:
            await drain()
        except Exception:
            log.exception("relay loop error")
        await asyncio.sleep(interval)


# ---------- cluster relay: outbox -> Kafka, and Kafka -> handlers ----------

async def kafka_relay_loop(service: str, interval: float = 0.3) -> None:
    """Runs inside every service process: publish this service's outbox rows to Kafka."""
    from aiokafka import AIOKafkaProducer

    producer = AIOKafkaProducer(bootstrap_servers=settings().kafka_bootstrap, acks="all", enable_idempotence=True)
    await producer.start()
    try:
        while True:
            try:
                for ev in await _pending(service):
                    body = json.dumps({"id": ev.id, "payload": ev.payload, "headers": ev.headers,
                                       "created_at": ev.created_at.isoformat() if ev.created_at else None}).encode()
                    await producer.send_and_wait(ev.topic, body, key=(ev.key or ev.id).encode())
                    await _mark(service, ev.id, True)
            except Exception:
                log.exception("kafka relay error service=%s", service)
            await asyncio.sleep(interval)
    finally:
        await producer.stop()


async def kafka_consume_loop(worker: str) -> None:
    """Runs in a worker deployment: consume the topics this worker subscribed to, commit after success."""
    from aiokafka import AIOKafkaConsumer

    subs = subscriptions(worker)
    topics = sorted({s.topic for s in subs})
    if not topics:
        raise RuntimeError(f"worker {worker} has no subscriptions")
    consumer = AIOKafkaConsumer(*topics, bootstrap_servers=settings().kafka_bootstrap, group_id=worker,
                                enable_auto_commit=False, auto_offset_reset="earliest")
    await consumer.start()
    log.info("worker %s consuming %s", worker, topics)
    try:
        async for msg in consumer:
            data = json.loads(msg.value)
            ev = Event(data["id"], msg.topic, (msg.key or b"").decode(), data["payload"], data.get("headers") or {},
                       dt.datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None)
            for attempt in range(MAX_ATTEMPTS):
                try:
                    await dispatch(ev, worker)
                    break
                except Exception:
                    log.exception("handler failed worker=%s topic=%s id=%s attempt=%s", worker, ev.topic, ev.id, attempt)
                    await asyncio.sleep(min(30, 0.5 * 2 ** attempt))
            await consumer.commit()
    finally:
        await consumer.stop()
