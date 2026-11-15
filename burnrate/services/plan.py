"""plan: the pay schedule, the instruction for each payday, and the burn-rate history.

The numbers come from the engine service; this service owns the state: which paydays were handled,
what was paid, and how the daily burn moved over time.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Date, Float, String, UniqueConstraint, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus, clients
from burnrate.common.context import get_ctx, now, real_now, today, user_tz
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import Conflict, NotFound
from burnrate.engine.core import usd
from burnrate.engine.io import ScheduleIn
from burnrate.engine.plan import paydays_per_year

SVC = "plan"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/plan", tags=["plan"])
CARD_KEYS = ("id", "name", "issuer", "balance_cents", "apr_bps", "close_day", "due_day", "statement_min_cents", "autopay", "last4", "pay_url")


class PlanRow(Base):
    __tablename__ = "plans"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_on: Mapped[dt.date] = mapped_column(Date)
    start_burn: Mapped[float] = mapped_column(Float)  # cents per day on the first day
    activated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class ScheduleRow(Base):
    __tablename__ = "schedules"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    frequency: Mapped[str] = mapped_column(String(16))
    anchor: Mapped[dt.date] = mapped_column(Date)
    per_payday_cents: Mapped[int]
    second_day: Mapped[int] = mapped_column(default=15)


class InstructionRow(Base):
    __tablename__ = "instructions"
    __table_args__ = (UniqueConstraint("user_id", "payday"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    payday: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(8), default="due")  # due | paid
    payload: Mapped[dict] = mapped_column(JSON)
    budget_cents: Mapped[int | None] = mapped_column(nullable=True)  # "short this payday"
    paid_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    paid_cents: Mapped[int | None] = mapped_column(nullable=True)
    saved_cents: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class BurnPoint(Base):
    __tablename__ = "burn_points"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)  # cents per day after the event
    delta: Mapped[float] = mapped_column(Float, default=0.0)
    label: Mapped[str] = mapped_column(String(120))
    ref: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


# ---------- helpers ----------

async def _cards(on: dt.date | None = None) -> list[dict]:
    r = await clients.get("cards", "/internal/cards", params={"on": (on or today()).isoformat()})
    return r["cards"]


def _for_engine(cards: list[dict]) -> list[dict]:
    return [{k: c.get(k) for k in CARD_KEYS if c.get(k) is not None} for c in cards]


def _sched_json(r: ScheduleRow) -> dict:
    return {"frequency": r.frequency, "anchor": r.anchor.isoformat(), "per_payday_cents": r.per_payday_cents, "second_day": r.second_day}


async def _schedule(s, required: bool = True) -> ScheduleRow | None:
    r = await s.get(ScheduleRow, get_ctx().user_id)
    if not r and required:
        raise Conflict("Tell us when you get paid first.", code="no_schedule")
    return r


async def _burn(cards: list[dict]) -> float:
    if not cards:
        return 0.0
    r = await clients.post("engine", "/internal/engine/burn", json={"cards": _for_engine(cards), "on": today().isoformat()})
    return r["burn_per_day"]


async def _paydays(sched: dict, start: dt.date, count: int) -> list[dt.date]:
    r = await clients.post("engine", "/internal/engine/paydays", json={"schedule": sched, "start": start.isoformat(), "count": count})
    return [dt.date.fromisoformat(d) for d in r["paydays"]]


async def _engine_instruction(cards: list[dict], sched: dict, payday: dt.date, budget: int | None) -> dict:
    body = {"cards": _for_engine(cards), "schedule": sched, "payday": payday.isoformat()}
    if budget is not None:
        body["budget_cents"] = budget
    return await clients.post("engine", "/internal/engine/instruction", json=body)


def _local(day: dt.date, hour: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, 0), tzinfo=user_tz()).astimezone(dt.UTC)


async def _timer(kind: str, due_at: dt.datetime, key: str, payload: dict) -> None:
    await clients.post("scheduler", "/internal/scheduler/timers", json={"kind": kind, "due_at": due_at.isoformat(), "dedupe_key": key, "payload": payload})


def _next_dom(day_of_month: int, after: dt.date) -> dt.date:
    from burnrate.engine.core import next_on_or_after

    return next_on_or_after(day_of_month, after)


async def _arm_payday(sched: dict, after: dt.date) -> None:
    uid = get_ctx().user_id
    nxt = (await _paydays(sched, after, 1))[0]
    await _timer("payday", _local(nxt, 8), f"payday:{uid}:{nxt.isoformat()}", {"payday": nxt.isoformat()})


async def _arm_card_timers(cards: list[dict], after: dt.date) -> None:
    uid = get_ctx().user_id
    for c in cards:
        if c["balance_cents"] <= 0:
            continue
        close = _next_dom(c["close_day"], after)
        await _timer("statement_expected", _local(close + dt.timedelta(days=2), 9), f"stmt:{uid}:{c['id']}:{close.isoformat()}",
                     {"card_id": c["id"], "card_name": c["name"], "close": close.isoformat()})
        due = _next_dom(c["due_day"], after + dt.timedelta(days=3))
        await _timer("due_safety", _local(due - dt.timedelta(days=3), 9), f"due:{uid}:{c['id']}:{due.isoformat()}",
                     {"card_id": c["id"], "card_name": c["name"], "due": due.isoformat()})


def _ins_view(row: InstructionRow | None, payload: dict, status: str) -> dict:
    return {"id": row.id if row else "", "status": status, **{k: v for k, v in payload.items() if k not in ("cards_after", "engine_version")},
            "paid_at": row.paid_at.isoformat() if row and row.paid_at else None, "paid_cents": row.paid_cents if row else None}


async def _add_point(s, value: float, label: str, on: dt.date, ref: str = "") -> None:
    uid = get_ctx().user_id
    last = (await s.execute(select(BurnPoint).where(BurnPoint.user_id == uid).order_by(BurnPoint.created_at.desc()).limit(1))).scalar_one_or_none()
    if last is not None and abs(last.value - value) < 0.005:
        return
    s.add(BurnPoint(id=new_id(), user_id=uid, date=on, value=value, delta=round(value - last.value, 4) if last else 0.0,
                    label=label[:120], ref=ref, created_at=real_now()))


# ---------- activation ----------

class ActivateReq(BaseModel):
    schedule: ScheduleIn
    started_on: dt.date | None = None


@router.post("/activate")
async def activate(req: ActivateReq):
    """Start the plan for the calling user: store the schedule, record the starting burn, arm the timers. Idempotent."""
    uid = get_ctx().user_id
    cards = await _cards()
    burn = await _burn(cards)
    sched = req.schedule.model_dump(mode="json")
    async with session(SVC) as s:
        row = await s.get(ScheduleRow, uid)
        if not row:
            s.add(ScheduleRow(user_id=uid, **{**req.schedule.model_dump(), "anchor": req.schedule.anchor}))
        first = not await s.get(PlanRow, uid)
        if first:
            started = req.started_on or today()
            s.add(PlanRow(user_id=uid, started_on=started, start_burn=burn, activated_at=real_now()))
            s.add(BurnPoint(id=new_id(), user_id=uid, date=started, value=burn, delta=0.0, label="Start", ref="", created_at=real_now()))
            await bus.emit(s, SVC, "plan.activated", {"user_id": uid})
    await _arm_payday(sched, today())
    await _arm_card_timers(cards, today())
    return {"activated": first, "start_burn": burn}


@bus.subscribe("cards.imported", worker="onboarding", service=SVC)
async def on_cards_imported(ev: bus.Event) -> None:
    sched = ev.payload.get("schedule")
    if sched:
        await activate(ActivateReq(schedule=ScheduleIn(**sched)))


@router.get("/schedule")
async def get_schedule():
    async with session(SVC) as s:
        return _sched_json(await _schedule(s))


@router.put("/schedule")
async def put_schedule(req: ScheduleIn):
    uid = get_ctx().user_id
    async with session(SVC) as s:
        row = await s.get(ScheduleRow, uid)
        if row:
            row.frequency, row.anchor, row.per_payday_cents, row.second_day = req.frequency, req.anchor, req.per_payday_cents, req.second_day
        else:
            s.add(ScheduleRow(user_id=uid, **req.model_dump()))
    await clients.post("scheduler", "/internal/scheduler/cancel", json={"prefix": f"payday:{uid}:"})
    await _arm_payday(req.model_dump(mode="json"), today())
    return req.model_dump(mode="json")


# ---------- instructions ----------

async def _get_or_create(s, payday: dt.date, cards: list[dict], sched: dict) -> InstructionRow:
    uid = get_ctx().user_id
    row = (await s.execute(select(InstructionRow).where(InstructionRow.user_id == uid, InstructionRow.payday == payday))).scalar_one_or_none()
    if row and row.status == "paid":
        return row
    payload = await _engine_instruction(cards, sched, payday, row.budget_cents if row else None)
    if row:
        row.payload = payload  # an unpaid instruction always reflects the cards as they are now
    else:
        row = InstructionRow(id=new_id(), user_id=uid, payday=payday, status="due", payload=payload, budget_cents=None,
                             paid_at=None, paid_cents=None, saved_cents=0, created_at=real_now())
        s.add(row)
    return row


async def _own_instruction(s, ins_id: str) -> InstructionRow:
    row = await s.get(InstructionRow, ins_id)
    if not row or row.user_id != get_ctx().user_id:
        raise NotFound("instruction not found", code="instruction_not_found")
    return row


def _headline(t: dt.date, status: str, payday: dt.date | None, nxt: dt.date, sends: list) -> tuple[str, str]:
    nice = nxt.strftime("%A, %b ") + str(nxt.day)
    if status == "paid":
        return "Done for this payday.", f"Next payday is {nice}."
    if status == "due" and payday == t:
        if not sends:
            return "It's payday.", "This paycheck covers your minimums. Nothing extra to send today."
        return "It's payday.", "One payment today. Autopay handles the rest."
    if status == "due":
        return "Your payday payment is still open.", f"It was planned for {payday.strftime('%b ')}{payday.day}. Sending it today still helps."
    return f"Next payday is {nice}.", "Here is what you will send then."


NOTES = {"this_paycheck": "reserved from this paycheck", "next_paycheck": "from your next paycheck",
         "covered_by_payment": "covered by this payday's payment", "nothing_due": "nothing due"}


@router.get("/today")
async def today_view():
    uid = get_ctx().user_id
    t = today()
    cards = await _cards()
    async with session(SVC) as s:
        plan = await s.get(PlanRow, uid)
        if not plan:
            raise Conflict("Your plan is not set up yet.", code="plan_not_active")
        sched_row = await _schedule(s)
        sched = _sched_json(sched_row)
    pds = await _paydays(sched, t - dt.timedelta(days=32), 8)
    last = max((p for p in pds if p <= t), default=None)
    nxt = min(p for p in pds if p > t)
    async with session(SVC) as s:
        row = None
        if last and last >= plan.started_on:
            row = await _get_or_create(s, last, cards, sched)
            await s.flush()
            ins = _ins_view(row, row.payload, row.status)
        else:
            ins = _ins_view(None, await _engine_instruction(cards, sched, nxt, None), "upcoming")
        points = (await s.execute(select(BurnPoint).where(BurnPoint.user_id == uid).order_by(BurnPoint.created_at))).scalars().all()
    burn_now = await _burn(cards)
    headline, lead = _headline(t, ins["status"], last, nxt, ins["sends"])
    upcoming = [{"date": nxt.isoformat(), "kind": "payday", "title": "Payday"}]
    for c in cards:
        if c["balance_cents"] > 0:
            upcoming.append({"date": _next_dom(c["close_day"], t).isoformat(), "kind": "statement_close", "card_id": c["id"], "title": f"{c['name']} statement closes"})
            upcoming.append({"date": _next_dom(c["due_day"], t).isoformat(), "kind": "due", "card_id": c["id"], "title": f"{c['name']} payment due"})
    upcoming.sort(key=lambda e: e["date"])
    return {
        "date": t.isoformat(), "headline": headline, "lead": lead, "instruction": ins,
        "burn": {"now": burn_now, "start": plan.start_burn, "down_by": round(plan.start_burn - burn_now, 4),
                 "started_on": plan.started_on.isoformat(),
                 "history": [{"date": p.date.isoformat(), "value": p.value, "label": p.label, "delta": p.delta} for p in points]},
        "autopay": [{"card_id": r["card_id"], "card_name": r["card_name"], "issuer": r["issuer"], "min_cents": r["min_cents"], "due": r["due"],
                     "source": r["source"], "note": NOTES.get(r["source"], "")} for r in ins.get("reserves", [])],
        "upcoming": upcoming[:8], "next_payday": nxt.isoformat(),
    }


class PaidReq(BaseModel):
    amount_cents: int | None = Field(default=None, ge=1, le=5_000_000)


@router.post("/instructions/{ins_id}/paid")
async def paid(ins_id: str, req: PaidReq):
    """The user says they made the payment at their bank. We record it; we never move money."""
    t = today()
    async with session(SVC) as s:
        row = await _own_instruction(s, ins_id)
        if row.status == "paid":
            return {"ok": True, "already": True}
        sends = [dict(x) for x in row.payload.get("sends", [])]
        if not sends:
            raise Conflict("Nothing to send for this payday.", code="nothing_to_pay")
        if req.amount_cents is not None and req.amount_cents != sum(x["amount_cents"] for x in sends):
            sends = [{**sends[0], "amount_cents": req.amount_cents}]  # a different amount goes to the target card
    total = saved = 0
    names = []
    for x in sends:
        r = await clients.post("cards", f"/internal/cards/{x['card_id']}/events", json={
            "type": "payment", "amount_cents": x["amount_cents"], "effective_date": t.isoformat(), "source": "plan",
            "ref": f"ins:{ins_id}", "note": "Payday payment"})
        total += x["amount_cents"]
        saved += x.get("saved_by_date_cents", 0) if req.amount_cents is None else 0
        names.append(r["card"]["name"])
    burn = await _burn(await _cards())
    async with session(SVC) as s:
        row = await _own_instruction(s, ins_id)
        row.status, row.paid_at, row.paid_cents, row.saved_cents = "paid", now(), total, saved
        row.payload = {**row.payload, "sends": sends if req.amount_cents is not None else row.payload["sends"]}
        await _add_point(s, burn, f"Payday, {usd(total, 0)} to {names[0]}", t, ref=f"ins:{ins_id}")
    return {"ok": True, "paid_cents": total}


@router.post("/instructions/{ins_id}/undo")
async def undo(ins_id: str):
    async with session(SVC) as s:
        row = await _own_instruction(s, ins_id)
        if row.status != "paid":
            return {"ok": True, "already": True}
        card_ids = [x["card_id"] for x in row.payload.get("sends", [])]
    for cid in card_ids:
        await clients.call("cards", "DELETE", f"/internal/cards/{cid}/events/by-ref/ins:{ins_id}")
    async with session(SVC) as s:
        row = await _own_instruction(s, ins_id)
        row.status, row.paid_at, row.paid_cents, row.saved_cents = "due", None, None, 0
        await s.execute(delete(BurnPoint).where(BurnPoint.user_id == row.user_id, BurnPoint.ref == f"ins:{ins_id}"))
    return {"ok": True}


class ShortReq(BaseModel):
    available_cents: int = Field(ge=0, le=5_000_000)


@router.post("/instructions/{ins_id}/short")
async def short(ins_id: str, req: ShortReq):
    """Less money this payday. Minimums stay first; nothing is marked as a failure."""
    async with session(SVC) as s:
        row = await _own_instruction(s, ins_id)
        if row.status == "paid":
            raise Conflict("This payday is already marked as paid.", code="already_paid")
        row.budget_cents = req.available_cents
    return {"ok": True}


@router.get("/projection")
async def projection():
    cards = await _cards()
    async with session(SVC) as s:
        sched = await _schedule(s)
        monthly = round(sched.per_payday_cents * paydays_per_year(sched.frequency) / 12)
    return await clients.post("engine", "/internal/engine/projection", json={
        "cards": _for_engine(cards), "monthly_budget_cents": monthly, "start": today().isoformat()})


@router.get("/timeline")
async def timeline(days: int = 60):
    t = today()
    cards = await _cards()
    async with session(SVC) as s:
        sched = _sched_json(await _schedule(s))
    events = [{"date": p.isoformat(), "kind": "payday", "title": "Payday"} for p in await _paydays(sched, t, 12) if (p - t).days <= days]
    for c in cards:
        for kind, key, title in (("statement_close", "close_day", "statement closes"), ("due", "due_day", "payment due")):
            d = _next_dom(c[key], t)
            while (d - t).days <= days:
                events.append({"date": d.isoformat(), "kind": kind, "card_id": c["id"], "title": f"{c['name']} {title}"})
                d = _next_dom(c[key], d + dt.timedelta(days=1))
    return {"events": sorted(events, key=lambda e: e["date"])}


@router.get("/meter")
async def meter():
    """Estimated part of "paid for itself": interest avoided by paying on payday rather than on the due date."""
    async with session(SVC) as s:
        rows = (await s.execute(select(InstructionRow).where(InstructionRow.user_id == get_ctx().user_id, InstructionRow.status == "paid",
                                                             InstructionRow.saved_cents > 0).order_by(InstructionRow.payday))).scalars().all()
        return {"items": [{"label": "Paid on payday instead of the due date", "cents": r.saved_cents, "kind": "estimated",
                           "date": r.payday.isoformat()} for r in rows]}


# ---------- event handlers ----------

@bus.subscribe("payday.due", worker="plan-events", service=SVC)
async def on_payday(ev: bus.Event) -> None:
    """Payday morning: make sure the instruction exists and ask for the SMS. Then arm the next payday."""
    payday = dt.date.fromisoformat(ev.payload["payday"])
    cards = await _cards()
    async with session(SVC) as s:
        sched_row = await _schedule(s, required=False)
        if not sched_row:
            return
        sched = _sched_json(sched_row)
        row = await _get_or_create(s, payday, cards, sched)
        send = (row.payload.get("sends") or [None])[0]
        data = {"amount_cents": send["amount_cents"], "card_name": send["card_name"], "burn_cut": send["burn_cut"]} if send else {}
        await bus.emit(s, SVC, "notify.request", {"template": "payday_sms" if send else "payday_short_sms", "data": data,
                                                  "dedupe_key": f"payday-sms:{row.user_id}:{payday.isoformat()}"})
    await _arm_payday(sched, payday + dt.timedelta(days=1))


@bus.subscribe("statement.expected", worker="plan-events", service=SVC)
async def on_statement_expected(ev: bus.Event) -> None:
    p = ev.payload
    close = dt.date.fromisoformat(p["close"])
    async with session(SVC) as s:
        await bus.emit(s, SVC, "notify.request", {"template": "statement_expected", "dedupe_key": f"stmt-mail:{p['user_id']}:{p['card_id']}:{p['close']}",
                                                  "data": {"card_name": p["card_name"], "close_label": close.strftime("%b ") + str(close.day)}})
    cards = [c for c in await _cards() if c["id"] == p["card_id"]]
    await _arm_card_timers(cards, close + dt.timedelta(days=1))


@bus.subscribe("due.safety", worker="plan-events", service=SVC)
async def on_due_safety(ev: bus.Event) -> None:
    """Three days before a due date: remind only when autopay is off, because then nothing protects the minimum."""
    p = ev.payload
    card = next((c for c in await _cards() if c["id"] == p["card_id"]), None)
    if not card or card["balance_cents"] <= 0 or card.get("autopay") != "none":
        return
    due = dt.date.fromisoformat(p["due"])
    async with session(SVC) as s:
        await bus.emit(s, SVC, "notify.request", {"template": "due_safety_sms", "dedupe_key": f"due-sms:{p['user_id']}:{p['card_id']}:{p['due']}",
                                                  "data": {"card_name": card["name"], "min_cents": card.get("statement_min_cents") or 0,
                                                           "due_label": due.strftime("%b ") + str(due.day)}})


REASONS = {"payment": "Payment to {card}", "purchase": "New purchase on {card}", "fee": "Fee on {card}", "interest": "{card} statement",
           "statement": "{card} statement", "credit": "Refund on {card}", "card_added": "{card} added", "card_removed": "{card} removed"}


@bus.subscribe("card.ledger.changed", worker="plan-recalc", service=SVC)
async def on_ledger_changed(ev: bus.Event) -> None:
    """Any change to a card moves the daily burn. Record the new level once."""
    uid = get_ctx().user_id
    async with session(SVC) as s:
        if not await s.get(PlanRow, uid):
            return
    cards = await _cards()
    burn = await _burn(cards)
    card = next((c for c in cards if c["id"] == ev.payload.get("card_id")), None)
    reason = ev.payload.get("reason", "")
    name = card["name"] if card else "a card"
    async with session(SVC) as s:
        last = (await s.execute(select(BurnPoint).where(BurnPoint.user_id == uid).order_by(BurnPoint.created_at.desc()).limit(1))).scalar_one_or_none()
        if reason == "rate_change" and card:
            verb = "lowered" if last and burn < last.value else "raised"
            label = f"{name.split()[0]} {verb} your rate to {card['apr_bps'] / 100:.2f}%"
        else:
            label = REASONS.get(reason, "Change on {card}").format(card=name)
        await _add_point(s, burn, label, today())


# ---------- demo seed and stats ----------

class SeedReq(BaseModel):
    schedule: ScheduleIn
    started_on: dt.date
    start_burn: float
    points: list[dict] = []
    instructions: list[dict] = []


@router.post("/seed")
async def seed(req: SeedReq):
    """Create a plan with history for the demo persona. No timers, no events."""
    uid = get_ctx().user_id
    async with session(SVC) as s:
        if await s.get(PlanRow, uid):
            return {"seeded": False}
        s.add(ScheduleRow(user_id=uid, **req.schedule.model_dump()))
        s.add(PlanRow(user_id=uid, started_on=req.started_on, start_burn=req.start_burn, activated_at=real_now()))
        base = real_now() - dt.timedelta(days=1)
        for i, p in enumerate(req.points):
            s.add(BurnPoint(id=new_id(), user_id=uid, date=dt.date.fromisoformat(p["date"]), value=p["value"], delta=p.get("delta", 0.0),
                            label=p["label"], ref=p.get("ref", ""), created_at=base + dt.timedelta(seconds=i)))
        for x in req.instructions:
            s.add(InstructionRow(id=new_id(), user_id=uid, payday=dt.date.fromisoformat(x["payday"]), status=x.get("status", "paid"),
                                 payload=x.get("payload", {"sends": [], "reserves": []}), budget_cents=None,
                                 paid_at=base, paid_cents=x.get("paid_cents"), saved_cents=x.get("saved_cents", 0), created_at=base))
    return {"seeded": True}


@router.post("/arm")
async def arm():
    """(Re)arm the calling user's timers from today. Used by the demo after seeding and after a restart."""
    cards = await _cards()
    async with session(SVC) as s:
        sched = _sched_json(await _schedule(s))
    await _arm_payday(sched, today())
    await _arm_card_timers(cards, today())
    return {"ok": True}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        plans = (await s.execute(select(func.count()).select_from(PlanRow))).scalar_one()
        rows = (await s.execute(select(InstructionRow.status, func.count()).group_by(InstructionRow.status))).all()
    return {"plans": plans, "instructions": {st: n for st, n in rows}}
