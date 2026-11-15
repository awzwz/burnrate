"""demo: a guest account with sample data and a clock the guest controls.

The demo runs on the production code path: same engine, same services, same events. Two things differ:
the data is a persona's, and "now" is shifted by an offset kept in the guest's session cookie.
Jumping the clock fires the guest's due timers, so SMS and emails appear in their inbox as they would in real life.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus, clients, persona
from burnrate.common.config import settings
from burnrate.common.context import Ctx, get_ctx, now, real_now, reset_ctx, set_ctx, today, user_tz
from burnrate.common.db import UTCDateTime, make_base, session
from burnrate.common.errors import DomainError, Forbidden
from burnrate.engine.core import next_on_or_after

SVC = "demo"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/demo", tags=["demo"])
log = logging.getLogger("burnrate.demo")


class DemoSession(Base):
    __tablename__ = "sessions"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    persona: Mapped[str] = mapped_column(String(16))
    d0: Mapped[str] = mapped_column(String(10))  # the payday the story starts on
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    jumps: Mapped[int] = mapped_column(default=0)


def _at(day: dt.date, hour: int, minute: int, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=tz).astimezone(dt.UTC)


async def _burn(cards: list[dict], on: dt.date) -> float:
    r = await clients.post("engine", "/internal/engine/burn", json={"cards": cards, "on": on.isoformat()})
    return r["burn_per_day"]


class StartReq(BaseModel):
    persona: str = "maya"
    tz: str = ""


@router.post("/start")
async def start(req: StartReq):
    """Create the persona as a real account with real history, and set the clock to her payday morning."""
    if not settings().demo_enabled:
        raise Forbidden("The demo is switched off.", code="demo_disabled")
    tzname = req.tz or settings().default_timezone
    tz = ZoneInfo(tzname)
    user = await clients.post("identity", "/internal/identity/users", json={"demo": True, "tz": tzname})
    real_today = real_now().astimezone(tz).date()
    d0 = persona.first_payday_on_or_after(real_today)
    offset = int((_at(d0, 8, 30, tz) - real_now()).total_seconds())
    hist = persona.history(d0)
    started = dt.date.fromisoformat(hist["started_on"])
    token = set_ctx(Ctx(user_id=user["id"], demo=True, clock_offset_s=offset, tz=tzname, request_id=get_ctx().request_id))
    try:
        # Cards as they were on the day she subscribed, then everything that happened since.
        ids: dict[str, str] = {}
        base_cards = persona.cards(d0)
        for c in base_cards:
            body = {**c, "balance_cents": hist["opening"][c["name"]], "opened_on": started.isoformat(), "source": "demo"}
            if c["name"] == hist["rate_cut"]["card"]:
                body["apr_bps"] = hist["rate_cut"]["from_bps"]
            ids[c["name"]] = (await clients.post("cards", "/internal/cards", json=body))["id"]
        for i, p in enumerate(hist["payments"]):
            await clients.post("cards", f"/internal/cards/{ids[p['card']]}/events", json={
                "type": "payment", "amount_cents": p["amount_cents"], "effective_date": p["date"], "source": "demo",
                "ref": f"demo:pay{i}", "note": "Payday payment"})
        rc = hist["rate_cut"]
        await clients.post("cards", f"/internal/cards/{ids[rc['card']]}/rate", json={
            "apr_bps": rc["to_bps"], "effective_date": rc["date"], "source": "ask", "ref": "demo:ask-rate",
            "note": "Rate lowered after a phone call"})

        # Burn-rate history: one point per event, each value computed by the engine.
        def stage(q_balance: int, chase_apr: int) -> list[dict]:
            out = []
            for c in base_cards:
                d = {k: c[k] for k in ("name", "issuer", "balance_cents", "apr_bps", "close_day", "due_day")}
                d["id"] = ids[c["name"]]
                if c["name"] == "Capital One Quicksilver":
                    d["balance_cents"] = q_balance
                if c["name"] == rc["card"]:
                    d["apr_bps"] = chase_apr
                out.append(d)
            return out

        pay = hist["payments"]
        levels = [
            (hist["started_on"], await _burn(stage(568_000, rc["from_bps"]), started), "Start", ""),
            (pay[0]["date"], await _burn(stage(544_000, rc["from_bps"]), started), "Payday, $240 to Capital One Quicksilver", "demo:pay0"),
            (rc["date"], await _burn(stage(544_000, rc["to_bps"]), started), "Chase lowered your rate to 22.49%", "demo:ask-rate"),
            (pay[1]["date"], await _burn(stage(520_000, rc["to_bps"]), started), "Payday, $240 to Capital One Quicksilver", "demo:pay1"),
        ]
        points, prev = [], None
        for date, value, label, ref in levels:
            points.append({"date": date, "value": value, "label": label, "ref": ref, "delta": round(value - prev, 4) if prev is not None else 0.0})
            prev = value
        await clients.post("plan", "/internal/plan/seed", json={
            "schedule": persona.schedule(d0), "started_on": hist["started_on"], "start_burn": levels[0][1], "points": points,
            "instructions": [{"payday": p["date"], "status": "paid", "paid_cents": p["amount_cents"], "saved_cents": 350} for p in pay]})

        await clients.post("billing", "/internal/billing/seed", json={
            "plan_id": persona.PLAN_ID, "started_at": _at(started, 10, 0, tz).isoformat(), "email": persona.EMAIL})
        await clients.put("notifications", "/internal/notifications/prefs", json={
            "phone": persona.PHONE, "sms_opt_in": True, "email": persona.EMAIL, "email_opt_in": True, "timezone": tzname})
        await _seed_statement(ids["Capital One Quicksilver"], d0)
        await _seed_asks(ids, d0, hist)

        async with session(SVC) as s:
            s.add(DemoSession(user_id=user["id"], persona=persona.NAME, d0=d0.isoformat(), created_at=real_now(), jumps=0))
        # Arm her timers and fire the ones already due: the payday SMS lands in the inbox right away.
        await clients.post("plan", "/internal/plan/arm")
        await clients.post("scheduler", "/internal/scheduler/dispatch")
    finally:
        reset_ctx(token)
    return {"user": user, "clock_offset_s": offset, "tz": tzname, "d0": d0.isoformat(), "persona": persona.NAME}


async def _seed_statement(card_id: str, d0: dt.date) -> None:
    """The statement that closed nine days before d0, already checked and reconciled."""
    close = d0 - dt.timedelta(days=9)
    due = next_on_or_after(min((d0 + dt.timedelta(days=19)).day, 28), close + dt.timedelta(days=18))
    fields = {"previous_balance_cents": 532_067, "payments_credits_cents": 24_000, "purchases_cents": 0, "fees_cents": 0,
              "interest_cents": 11_933, "new_balance_cents": 520_000, "apr_bps": 2799, "balance_subject_to_interest_cents": 518_720,
              "days_in_cycle": 30, "min_payment_cents": 17_133, "close_date": close.isoformat(), "due_date": due.isoformat()}
    checks = (await clients.post("engine", "/internal/engine/statement-checks", json={"fields": fields, "issuer": "capital_one"}))["checks"]
    rec = await clients.post("engine", "/internal/engine/reconcile", json={
        "predicted_interest_cents": 11_941, "actual_interest_cents": 11_933, "apr_bps": 2799, "days": 30})
    await clients.post("statements", "/internal/statements/seed", json={"statements": [{
        "card_id": card_id, "card_name": "Capital One Quicksilver", "fields": fields, "checks": checks, "reconciliation": rec}]})


async def _seed_asks(ids: dict[str, str], d0: dt.date, hist: dict) -> None:
    """Her sprint so far: the Chase rate request succeeded; the late-fee refund and two rate requests are waiting."""
    rc = hist["rate_cut"]
    try:
        await clients.post("asks", "/internal/asks/seed", json={
            "sprint_started_on": hist["started_on"],
            "asks": [
                {"key": "chase-rate", "card_id": ids[rc["card"]], "type": "apr_reduction", "status": "done", "order": 0,
                 "created_on": hist["started_on"], "done_on": rc["date"], "result": "Rate lowered from 24.49% to 22.49%"},
                {"key": "chase-fee", "card_id": ids["Chase Freedom Unlimited"], "type": "late_fee_refund", "status": "queued", "order": 1,
                 "created_on": hist["started_on"], "value_cents": 3_900},
                {"key": "cap-rate", "card_id": ids["Capital One Quicksilver"], "type": "apr_reduction", "status": "queued", "order": 2,
                 "created_on": hist["started_on"]},
                {"key": "disc-rate", "card_id": ids["Discover it"], "type": "apr_reduction", "status": "queued", "order": 3,
                 "created_on": hist["started_on"]},
            ],
            "outcomes": [{"ask_key": "chase-rate", "result": "granted", "channel": "phone", "old_apr_bps": rc["from_bps"],
                          "new_apr_bps": rc["to_bps"], "date": rc["date"], "hard_pull": "no"}],
        })
    except DomainError as e:  # the demo still works without the sprint history
        log.warning("asks seed skipped: %s", e.code)


class ClockReq(BaseModel):
    action: str = Field(pattern="^(next_payday|next_statement|plus_day)$")


@router.post("/clock")
async def clock(req: ClockReq):
    """Move the calling guest's clock forward and let the world catch up. Returns the new offset for the cookie."""
    ctx = get_ctx()
    if not ctx.demo:
        raise Forbidden("Only demo sessions have a clock.", code="not_a_demo")
    tz, t, moment = user_tz(), today(), now()
    statement_card: dict | None = None
    if req.action == "plus_day":
        target, note = moment + dt.timedelta(days=1), "One day later."
    elif req.action == "next_payday":
        sched = await clients.get("plan", "/internal/plan/schedule")
        nxt = (await clients.post("engine", "/internal/engine/paydays", json={"schedule": sched, "start": (t + dt.timedelta(days=1)).isoformat(), "count": 1}))["paydays"][0]
        target, note = _at(dt.date.fromisoformat(nxt), 8, 30, tz), "Payday morning."
    else:
        cards = [c for c in (await clients.get("cards", "/internal/cards"))["cards"] if c["balance_cents"] > 0]
        if not cards:
            raise DomainError("No card has a balance, so no statement is coming.", code="no_statement")
        done = {(s["card_id"], s["close_date"]) for s in (await clients.get("statements", "/internal/statements"))["statements"]}

        def next_close(c: dict) -> dt.date:
            d = next_on_or_after(c["close_day"], t)
            while (c["id"], d.isoformat()) in done:
                d = next_on_or_after(c["close_day"], d + dt.timedelta(days=1))
            return d

        statement_card = min(cards, key=next_close)
        close = next_close(statement_card)
        target, note = _at(close + dt.timedelta(days=2), 9, 30, tz), f"{statement_card['name']} statement is out."
    if target <= moment:
        target = moment + dt.timedelta(minutes=1)
    offset = ctx.clock_offset_s + int((target - moment).total_seconds())
    token = set_ctx(Ctx(user_id=ctx.user_id, demo=True, clock_offset_s=offset, tz=ctx.tz, request_id=ctx.request_id))
    try:
        fired = (await clients.post("scheduler", "/internal/scheduler/dispatch"))["fired"]
        statement_id = None
        if statement_card:
            statement_id = (await clients.post("statements", "/internal/statements/demo-generate", json={"card_id": statement_card["id"]}))["id"]
        if settings().is_local:
            await bus.drain()  # one process: let every handler finish before answering
        jumped = today().isoformat()
    finally:
        reset_ctx(token)
    async with session(SVC) as s:
        row = await s.get(DemoSession, ctx.user_id)
        if row:
            row.jumps += 1
    return {"clock_offset_s": offset, "jumped_to": jumped, "note": note, "timers_fired": fired, "statement_id": statement_id}


@router.get("/session")
async def get_session():
    async with session(SVC) as s:
        row = await s.get(DemoSession, get_ctx().user_id)
        return {"persona": row.persona, "d0": row.d0} if row else {"persona": None, "d0": None}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        n = (await s.execute(select(func.count()).select_from(DemoSession))).scalar_one()
        jumps = (await s.execute(select(func.coalesce(func.sum(DemoSession.jumps), 0)))).scalar_one()
    return {"demo_sessions": n, "clock_jumps": int(jumps)}
