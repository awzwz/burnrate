"""scheduler: durable timers. A timer is a row; firing it publishes an event. Nothing is lost on restart.

Kinds and the topic each one fires:
    payday            -> payday.due
    due_safety        -> due.safety
    statement_expected-> statement.expected
    renewal_notice    -> renewal.notice.due
    renewal           -> renewal.due
Real users are served by the tick loop (timer-dispatcher). A demo user's timers fire when the demo clock jumps.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus
from burnrate.common.context import Ctx, get_ctx, now, real_now, reset_ctx, set_ctx
from burnrate.common.db import UTCDateTime, make_base, new_id, session

SVC = "scheduler"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/scheduler", tags=["scheduler"])
log = logging.getLogger("burnrate.scheduler")

TOPICS = {"payday": "payday.due", "due_safety": "due.safety", "statement_expected": "statement.expected",
          "renewal_notice": "renewal.notice.due", "renewal": "renewal.due"}


class Timer(Base):
    __tablename__ = "timers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    due_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending | fired | canceled
    demo: Mapped[bool] = mapped_column(default=False)
    tz: Mapped[str] = mapped_column(String(64), default="")
    fired_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class TimerReq(BaseModel):
    kind: str = Field(pattern="^(payday|due_safety|statement_expected|renewal_notice|renewal)$")
    due_at: dt.datetime
    payload: dict = {}
    dedupe_key: str = Field(min_length=3, max_length=120)


@router.post("/timers")
async def upsert_timer(req: TimerReq):
    """Create a timer, or move a pending one with the same key. A fired timer is not re-armed."""
    ctx = get_ctx()
    due = req.due_at if req.due_at.tzinfo else req.due_at.replace(tzinfo=dt.UTC)
    async with session(SVC) as s:
        t = (await s.execute(select(Timer).where(Timer.dedupe_key == req.dedupe_key))).scalar_one_or_none()
        if t:
            if t.status == "pending":
                t.due_at, t.payload = due, req.payload
            return {"id": t.id, "status": t.status, "created": False}
        t = Timer(id=new_id(), user_id=ctx.user_id, kind=req.kind, due_at=due, payload=req.payload, dedupe_key=req.dedupe_key,
                  status="pending", demo=ctx.demo, tz=ctx.tz, created_at=real_now())
        s.add(t)
        return {"id": t.id, "status": "pending", "created": True}


class CancelReq(BaseModel):
    prefix: str = Field(min_length=3)


@router.post("/cancel")
async def cancel(req: CancelReq):
    async with session(SVC) as s:
        r = await s.execute(update(Timer).where(Timer.user_id == get_ctx().user_id, Timer.status == "pending",
                                                Timer.dedupe_key.startswith(req.prefix)).values(status="canceled"))
        return {"canceled": r.rowcount}


async def _fire_due(user_id: str | None, moment: dt.datetime, limit: int = 500) -> int:
    """Publish every pending timer that is due. For one user: under the caller's context (the demo clock).
    For everyone: real users only, each under its own context."""
    fired = 0
    async with session(SVC) as s:
        q = select(Timer).where(Timer.status == "pending", Timer.due_at <= moment)
        q = q.where(Timer.user_id == user_id) if user_id else q.where(Timer.demo.is_(False))
        if s.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)  # several dispatchers never take the same timer
        rows = (await s.execute(q.order_by(Timer.due_at).limit(limit))).scalars().all()
        for t in rows:
            token = None if user_id else set_ctx(Ctx(user_id=t.user_id, demo=t.demo, tz=t.tz))
            try:
                await bus.emit(s, SVC, TOPICS[t.kind], {"user_id": t.user_id, "kind": t.kind, "timer_id": t.id,
                                                         "due_at": t.due_at.isoformat(), **(t.payload or {})}, key=t.user_id)
            finally:
                if token is not None:
                    reset_ctx(token)
            t.status, t.fired_at = "fired", real_now()
            fired += 1
    return fired


@router.post("/dispatch")
async def dispatch():
    """Fire the calling user's due timers at the caller's "now" (used after a demo clock jump)."""
    return {"fired": await _fire_due(get_ctx().user_id, now())}


@router.get("/timers")
async def list_timers():
    async with session(SVC) as s:
        rows = (await s.execute(select(Timer).where(Timer.user_id == get_ctx().user_id).order_by(Timer.due_at))).scalars().all()
        return {"timers": [{"id": t.id, "kind": t.kind, "due_at": t.due_at.isoformat(), "status": t.status, "payload": t.payload} for t in rows]}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        rows = (await s.execute(select(Timer.status, func.count()).group_by(Timer.status))).all()
    return {"timers": {st: n for st, n in rows}}


async def tick_loop(interval: float = 5.0) -> None:
    """timer-dispatcher: fire due timers of real users, forever."""
    while True:
        try:
            n = await _fire_due(None, real_now())
            if n:
                log.info("fired %s timers", n)
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        await asyncio.sleep(interval)


async def startup(app) -> list:
    """Local mode has no separate dispatcher deployment: run the tick inside the process."""
    from burnrate.common.config import settings

    st = settings()
    if st.is_local and not st.is_test:
        return [asyncio.create_task(tick_loop(2.0))]
    return []
