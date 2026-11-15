"""asks: the two-week sprint of requests to the card issuer, the outcome log and the issuer playbook.

The person makes every call. This service builds the queue from their own cards, fills the script with their own
numbers, records what the issuer answered, and passes a confirmed result on to the ledger (a new rate, a refunded
fee). Every text here is a template filled by code; no model writes it, and all of it passes common/guardrails.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from fractions import Fraction
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import Date, String, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus, clients
from burnrate.common.context import get_ctx, real_now, reset_ctx, set_ctx, today, with_ctx
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import Conflict, DomainError, NotFound
from burnrate.common.guardrails import check_text
from burnrate.engine.core import round_cents, usd
from burnrate.engine.io import ScheduleIn
from burnrate.engine.simulate import yearly_value_of_rate_cut

SVC = "asks"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/asks", tags=["asks"])
log = logging.getLogger("burnrate.asks")

TYPES = ("late_fee_refund", "apr_reduction", "annual_fee", "due_date_move", "hardship")
POSITIVE = ("granted", "partial", "temporary")
SPRINT_DAYS = 14
PLAYBOOK_MIN = 20  # below this many reports for an issuer and a request type, no shares are shown
PLAYBOOK_WINDOW_DAYS = 365
PLAYBOOK_NOTE_FEW = "Self-reported by Burn Rate users; too few reports to show shares."
ISSUER_NAMES = {"capital_one": "Capital One", "chase": "Chase", "discover": "Discover", "citi": "Citi", "amex": "American Express",
                "bank_of_america": "Bank of America", "wells_fargo": "Wells Fargo"}

Q_HARD_INQUIRY = "Will this request result in a hard inquiry?"
Q_PERMANENT = "Is the new rate permanent or temporary, and until what date?"
Q_BALANCES = "Which balances does it apply to?"
Q_REFUND_SHOWS = "Will the refund show on my next statement?"
Q_OFFER_FORM = "What form does the offer take: a statement credit, points, or a spending requirement?"
Q_NEW_DATE_FROM = "Which billing cycle does the new due date start with?"
Q_HARDSHIP = ("What will the rate be, and for how many months?",
              "Will the card be frozen or closed, and will it reopen afterwards?",
              "How will the account be reported to the credit bureaus?",
              "What happens to the rate and the minimum payment if I miss a payment?")
Q_IN_WRITING = "Can I receive these terms in writing before the arrangement starts?"
MUST_ASK: dict[str, tuple[str, ...]] = {
    "apr_reduction": (Q_HARD_INQUIRY, Q_PERMANENT, Q_BALANCES),
    "late_fee_refund": (Q_HARD_INQUIRY, Q_REFUND_SHOWS),
    "annual_fee": (Q_HARD_INQUIRY, Q_OFFER_FORM),
    "due_date_move": (Q_HARD_INQUIRY, Q_NEW_DATE_FROM),
    "hardship": (Q_HARD_INQUIRY, *Q_HARDSHIP, Q_IN_WRITING),
}

# Other people's statistics: always with the source, the year, the sample and the self-selection caveat (spec 3.16).
_SURVEY = "LendingTree survey, 2026, as reported by Fox 29 and Valley News Live (Sept. 2, 2026)"
_SURVEY_CAVEAT = ("Self-reported by the people who chose to ask, out of about 2,000 surveyed. It describes them, "
                  "not your account, and it is not a forecast. The issuer may say no.")
EVIDENCE: dict[str, dict] = {
    "late_fee_refund": {"text": "In a 2026 LendingTree survey, 92% of cardholders who asked for a late fee to be waived said the issuer agreed.",
                        "source": _SURVEY, "caveat": _SURVEY_CAVEAT, "numbers": ["92%"]},
    "apr_reduction": {"text": "In a 2026 LendingTree survey, 84% of cardholders who asked for a lower rate said they got one. "
                              "The average reduction they reported was 6.3 percentage points.",
                      "source": _SURVEY, "caveat": _SURVEY_CAVEAT, "numbers": ["84%", "6.3%"]},
    "annual_fee": {"text": "In a 2026 LendingTree survey, 94% of cardholders who asked to have an annual fee waived or reduced said the issuer agreed.",
                   "source": _SURVEY, "caveat": _SURVEY_CAVEAT, "numbers": ["94%"]},
    "due_date_move": {"text": "No survey covers this request. The rule behind it: a card's payment due date must fall on the same day every month.",
                      "source": "Regulation Z, 12 CFR 1026.7(b)(11)",
                      "caveat": "There is no data on how often issuers agree to a particular date. The issuer may say no.", "numbers": []},
    "hardship": {"text": "Issuers do not publish hardship terms; they are set case by case. You can ask to receive the terms in writing before an arrangement starts.",
                 "source": "Regulation Z, 12 CFR 1026.55(b)(5)",
                 "caveat": "There is no reliable data on how often issuers offer an arrangement, or on what terms. The issuer may say no.", "numbers": []},
}
_DISCOVER_FIRST_LATE = " Discover's cardmember agreement says there is no late fee the first time you pay late."


# ---------- tables ----------

class AskRow(Base):
    __tablename__ = "asks"
    __table_args__ = (UniqueConstraint("user_id", "ref", name="uq_asks_user_ref"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(24))
    card_id: Mapped[str] = mapped_column(String(32))
    card_name: Mapped[str] = mapped_column(String(60), default="")
    issuer: Mapped[str] = mapped_column(String(32), default="other")
    title: Mapped[str] = mapped_column(String(160))
    subtitle: Mapped[str] = mapped_column(String(300), default="")
    value_cents: Mapped[int | None] = mapped_column(nullable=True)
    value_label: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued | done | skipped
    position: Mapped[int] = mapped_column(default=0)
    result: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ref: Mapped[str] = mapped_column(String(120))  # what produced the ask: makes every way of adding one idempotent
    source: Mapped[str] = mapped_column(String(12), default="build")  # build | statement | suggest | seed
    facts: Mapped[dict] = mapped_column(default=dict)  # the person's numbers at the time: fee, month, balance, rate, due day
    created_on: Mapped[dt.date] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    done_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)


class SprintRow(Base):
    """One per user, written when the queue is built. Its existence is what makes the build idempotent."""

    __tablename__ = "sprints"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_on: Mapped[dt.date] = mapped_column(Date)
    ends_on: Mapped[dt.date] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class OutcomeRow(Base):
    __tablename__ = "outcomes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dedupe: Mapped[str] = mapped_column(String(64), unique=True)  # the ask id; for "no answer", the ask id and the day
    ask_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    card_id: Mapped[str] = mapped_column(String(32))
    card_name: Mapped[str] = mapped_column(String(60), default="")
    issuer: Mapped[str] = mapped_column(String(32), default="other")
    type: Mapped[str] = mapped_column(String(24))
    result: Mapped[str] = mapped_column(String(12))
    channel: Mapped[str] = mapped_column(String(8))
    new_apr_bps: Mapped[int | None] = mapped_column(nullable=True)
    old_apr_bps: Mapped[int | None] = mapped_column(nullable=True)
    until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    refund_cents: Mapped[int | None] = mapped_column(nullable=True)
    balance_cents: Mapped[int | None] = mapped_column(nullable=True)  # the card's balance when the outcome was logged
    rep_name: Mapped[str] = mapped_column(String(60), default="")
    hard_pull: Mapped[str] = mapped_column(String(8), default="unknown")
    ask_again_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    on: Mapped[dt.date] = mapped_column(Date)  # the person's date when it happened
    applied: Mapped[bool] = mapped_column(default=False)  # the ledger effect went through and the event was written
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class PlaybookRow(Base):
    """Counts only: issuer x request x channel x result x month. Nothing here points back to a person."""

    __tablename__ = "playbook"
    issuer: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(24), primary_key=True)
    channel: Mapped[str] = mapped_column(String(8), primary_key=True)
    result: Mapped[str] = mapped_column(String(12), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    n: Mapped[int] = mapped_column(default=0)


class PlaybookSeen(Base):
    """Event ids already counted, written in the same transaction as the count."""

    __tablename__ = "playbook_seen"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


# ---------- small formatting helpers ----------

def money(cents: int) -> str:
    text = usd(cents)
    return text.removesuffix(".00")


def pct(bps: int) -> str:
    return f"{bps / 100:.2f}%"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def short_date(d: dt.date, on: dt.date) -> str:
    text = f"{d:%b} {d.day}"
    return f"{text}, {d.year}" if abs((d - on).days) >= 330 else text


def _int(value, low: int, high: int) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if low <= n <= high else None


def _yes(value) -> bool:
    return value is True or str(value).strip().lower() in ("yes", "true", "1", "y")


@contextmanager
def _as_user(user_id: str) -> Iterator[None]:
    """Event handlers act for the user named in the payload, so calls to other services carry that user."""
    token = set_ctx(with_ctx(user_id=user_id))
    try:
        yield
    finally:
        reset_ctx(token)


# ---------- the cards service ----------

async def _cards() -> list[dict]:
    return (await clients.get("cards", "/internal/cards"))["cards"]


async def _cards_or_empty() -> dict[str, dict]:
    """For reads: the page still renders from stored numbers if the cards service is away."""
    try:
        return {c["id"]: c for c in await _cards()}
    except DomainError:
        log.warning("cards service unavailable; using stored numbers")
        return {}


def _match_card(cards: list[dict], name) -> dict | None:
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    return (next((c for c in cards if c["id"] == name or c["name"].lower() == wanted), None)
            or next((c for c in cards if wanted in c["name"].lower() or c["name"].lower() in wanted), None)
            or next((c for c in cards if c["issuer"] != "other" and c["issuer"].replace("_", " ") in wanted), None))


# ---------- what an ask says on the list ----------

def describe(type_: str, card: dict, facts: dict) -> dict:
    """Title, subtitle and value from the person's numbers. `card` needs name, balance_cents and apr_bps."""
    name = card.get("name") or "your card"
    if type_ == "late_fee_refund":
        fee, month = facts.get("fee_cents"), facts.get("fee_month")
        what = f"{money(fee)} late fee" if fee else "Late fee"
        return {"title": f"Ask for the late fee back on {name}", "subtitle": what + (f" on your {month} statement" if month else ""),
                "value_cents": fee, "value_label": money(fee) if fee else "The amount of the late fee"}
    if type_ == "apr_reduction":
        per_point = yearly_value_of_rate_cut(card.get("balance_cents") or 0, 100)
        return {"title": f"Ask for a lower rate on {name}", "subtitle": f"{money(card.get('balance_cents') or 0)} at {pct(card.get('apr_bps') or 0)}",
                "value_cents": per_point, "value_label": f"Each point off is worth {money(per_point)} a year on this balance"}
    if type_ == "annual_fee":
        fee = facts.get("fee_cents")
        return {"title": f"Ask about the annual fee on {name}", "subtitle": "A waiver, a retention offer, or a no-fee version of the card",
                "value_cents": fee, "value_label": money(fee) if fee else "The annual fee on this card"}
    if type_ == "due_date_move":
        due, target = facts.get("due_day") or card.get("due_day"), facts.get("target_day")
        subtitle = f"Due on the {ordinal(due)}" if due else "The due date does not line up with payday"
        if facts.get("gap_days") is not None:
            subtitle += f", {facts['gap_days']} days after payday"
        if target:
            subtitle += f". Ask for the {ordinal(target)}."
        return {"title": f"Move the due date on {name}", "subtitle": subtitle, "value_cents": None,
                "value_label": "No dollar value: a due date just after payday is easier to meet"}
    return {"title": f"Ask about a hardship arrangement on {name}", "subtitle": "Only when the minimum payments are out of reach",
            "value_cents": None, "value_label": "Terms are set case by case"}


def _view(a: AskRow, order: int, live: dict | None = None) -> dict:
    out = {"id": a.id, "type": a.type, "card_id": a.card_id, "card_name": (live or {}).get("name") or a.card_name, "issuer": a.issuer,
           "title": a.title, "subtitle": a.subtitle, "value_cents": a.value_cents, "value_label": a.value_label,
           "status": a.status, "order": order, "result": a.result}
    if live and a.status == "queued":  # balances move; a queued ask shows today's numbers
        fresh = describe(a.type, live, a.facts)
        if a.facts.get("auto"):
            out.update(fresh)
        elif a.type == "apr_reduction":  # the wording came from elsewhere, the per-point value is still ours
            out.update(value_cents=fresh["value_cents"], value_label=fresh["value_label"])
    return out


async def _own(s, ask_id: str) -> AskRow:
    a = await s.get(AskRow, ask_id)
    if not a or a.user_id != get_ctx().user_id:
        raise NotFound("ask not found", code="ask_not_found")
    return a


async def _rows(s, user_id: str) -> list[AskRow]:
    return list((await s.execute(select(AskRow).where(AskRow.user_id == user_id).order_by(AskRow.position, AskRow.created_at, AskRow.id))).scalars().all())


def _sprint_view(sprint: SprintRow | None, rows: list[AskRow]) -> dict | None:
    if sprint is None:
        return None
    return {"done": sum(1 for a in rows if a.status == "done"), "total": sum(1 for a in rows if a.status != "skipped"),
            "ends_on": sprint.ends_on.isoformat()}


async def _queue(live: dict[str, dict] | None = None) -> dict:
    uid = get_ctx().user_id
    if live is None:
        live = await _cards_or_empty()
    async with session(SVC) as s:
        rows = await _rows(s, uid)
        sprint = await s.get(SprintRow, uid)
        return {"asks": [_view(a, i + 1, live.get(a.card_id)) for i, a in enumerate(rows)], "sprint": _sprint_view(sprint, rows)}


def _new_ask(user_id: str, type_: str, card: dict, facts: dict, *, ref: str, source: str, position: int, on: dt.date,
             title: str | None = None, subtitle: str | None = None, value_cents: int | None = None) -> AskRow:
    facts = {**facts, "balance_cents": card.get("balance_cents"), "apr_bps": card.get("apr_bps"),
             "auto": title is None and subtitle is None and value_cents is None}
    text = describe(type_, card, facts)
    if value_cents is not None and type_ != "apr_reduction":  # a rate ask is always valued per point of rate
        text.update(value_cents=value_cents, value_label=money(value_cents))
    return AskRow(id=new_id(), user_id=user_id, type=type_, card_id=card["id"], card_name=card.get("name", "")[:60],
                  issuer=card.get("issuer") or "other", title=(title or text["title"])[:160], subtitle=(subtitle or text["subtitle"])[:300],
                  value_cents=text["value_cents"], value_label=text["value_label"][:200], status="queued", position=position,
                  result=None, ref=ref[:120], source=source, facts=facts, created_on=on, created_at=real_now(), done_at=None)


# ---------- building the queue ----------

def _due_date_move(cards: list[dict], schedule: dict | None) -> tuple[dict, dict] | None:
    """At most one card whose due day is not in the week after a payday. Only for monthly and twice-monthly pay:
    with weekly or biweekly pay the payday moves through the month, so no fixed due day stays next to it."""
    try:
        sched = ScheduleIn.model_validate(schedule or {})
    except ValueError:
        return None
    if sched.frequency not in ("monthly", "semimonthly"):
        return None
    paydays = sorted({sched.anchor.day, sched.second_day}) if sched.frequency == "semimonthly" else [sched.anchor.day]
    best: tuple[int, int, dict, dict] | None = None
    for card in cards:
        if not card.get("balance_cents") or not card.get("due_day"):
            continue
        gap, payday = min(((card["due_day"] - p) % 30, p) for p in paydays)  # days from the latest payday to the due day
        if 1 <= gap <= 7:
            continue
        target = (payday + 3 - 1) % 28 + 1
        facts = {"due_day": card["due_day"], "target_day": target, "payday_day": payday, "gap_days": gap}
        rank = (30 if gap == 0 else gap, card["balance_cents"])
        if best is None or rank > best[:2]:
            best = (*rank, card, facts)
    return (best[2], best[3]) if best else None


async def _schedule_from_plan() -> dict | None:
    """Best effort: the plan service owns the pay schedule. Without it there is simply no due-date ask."""
    try:
        got = await clients.get("plan", "/internal/plan/schedule", retries=0)
    except Exception:  # noqa: BLE001 - the plan service may not exist yet, or the person has no schedule
        return None
    if isinstance(got, dict) and isinstance(got.get("schedule"), dict):
        got = got["schedule"]
    return got if isinstance(got, dict) and got.get("frequency") else None


def _specs(cards: list[dict], answers: dict, schedule: dict | None) -> list[tuple[str, dict, dict]]:
    """(type, card, facts) in queue order: the late fee, rates by balance, the annual fee, one due date."""
    by_balance = sorted(cards, key=lambda c: -(c.get("balance_cents") or 0))
    out: list[tuple[str, dict, dict]] = []
    if _yes(answers.get("late_fee_recently")):
        card = _match_card(cards, answers.get("late_fee_card")) or by_balance[0]
        out.append(("late_fee_refund", card, {"fee_cents": _int(answers.get("late_fee_cents"), 1, 20_000),
                                              "fee_month": str(answers.get("late_fee_month") or "")[:20] or None,
                                              "autopay": answers.get("autopay") if answers.get("autopay") in ("min", "full", "none") else None}))
    owing = [c for c in by_balance if (c.get("balance_cents") or 0) > 0 and (c.get("apr_bps") or 0) > 0]
    for i, card in enumerate(owing):
        out.append(("apr_reduction", card, {"largest": i == 0 and len(owing) > 1}))
    fee_card = _match_card(cards, answers.get("annual_fee_card"))
    if fee_card:
        out.append(("annual_fee", fee_card, {"fee_cents": _int(answers.get("annual_fee_cents"), 1, 100_000)}))
    move = _due_date_move(cards, schedule)
    if move:
        out.append(("due_date_move", move[0], move[1]))
    return out  # hardship is never queued automatically


async def _build(answers: dict, schedule: dict | None) -> dict:
    uid = get_ctx().user_id
    if not uid:
        raise DomainError("no user in the request context", code="no_user", status=422)
    async with session(SVC) as s:
        built = await s.get(SprintRow, uid) is not None
    if built:
        return {**await _queue(), "built": False}
    cards = await _cards()
    if not cards:
        return {**await _queue({}), "built": False}  # nothing to ask about yet; a later call builds the queue
    if schedule is None and isinstance(answers.get("schedule"), dict):
        schedule = answers["schedule"]
    if schedule is None:
        schedule = await _schedule_from_plan()
    on = today()
    try:
        async with session(SVC) as s:
            s.add(SprintRow(user_id=uid, started_on=on, ends_on=on + dt.timedelta(days=SPRINT_DAYS), created_at=real_now()))
            taken = {(a.card_id, a.type) for a in await _rows(s, uid) if a.status == "queued"}  # e.g. from a statement that came first
            position = 0
            for type_, card, facts in _specs(cards, answers, schedule):
                if (card["id"], type_) in taken:
                    continue
                position += 1
                s.add(_new_ask(uid, type_, card, facts, ref=f"build:{type_}:{card['id']}", source="build", position=position, on=on))
    except IntegrityError:  # another request built it at the same moment
        return {**await _queue(), "built": False}
    return {**await _queue({c["id"]: c for c in cards}), "built": True}


class BuildReq(BaseModel):
    answers: dict = Field(default_factory=dict)
    schedule: dict | None = None


@router.post("/build")
async def build(req: BuildReq):
    """Build the sprint queue from the person's cards and quiz answers. A second call changes nothing."""
    return await _build(req.answers, req.schedule)


@bus.subscribe("cards.imported", worker="asks-events", service=SVC)
async def on_cards_imported(ev: bus.Event) -> None:
    payload = ev.payload or {}
    uid = payload.get("user_id") or get_ctx().user_id
    if not uid:
        return
    with _as_user(uid):
        await _build(payload.get("answers") or {}, payload.get("schedule"))


async def _add(type_: str, card_id: str, *, ref: str, source: str, title: str | None = None, detail: str | None = None,
               value_cents: int | None = None, facts: dict | None = None) -> tuple[dict, bool]:
    """Add one ask for the current user unless this reference already produced one, or the same request on the same
    card is still waiting. Returns (ask, created)."""
    uid = get_ctx().user_id
    if type_ not in TYPES:
        raise DomainError("unknown ask type", code="bad_ask_type", status=422)
    cards = {c["id"]: c for c in await _cards()}
    card = cards.get(card_id)
    if card is None:
        raise NotFound("card not found", code="card_not_found")
    on = today()
    try:
        async with session(SVC) as s:
            rows = await _rows(s, uid)
            same = next((a for a in rows if a.ref == ref), None) or next(
                (a for a in rows if a.card_id == card_id and a.type == type_ and a.status == "queued"), None)
            if same:
                return _view(same, rows.index(same) + 1, card), False
            queued = [a.position for a in rows if a.status == "queued"]
            # a late fee is the simplest request with the clearest value, so it goes to the front
            position = (min(queued) - 1 if queued else 1) if type_ == "late_fee_refund" else max([a.position for a in rows], default=0) + 1
            ask = _new_ask(uid, type_, card, facts or {}, ref=ref, source=source, position=position, on=on,
                           title=title, subtitle=detail, value_cents=value_cents)
            s.add(ask)
            await s.flush()
            rows = await _rows(s, uid)
            return _view(ask, [a.id for a in rows].index(ask.id) + 1, card), True
    except IntegrityError:  # the same reference, written by a concurrent request
        async with session(SVC) as s:
            rows = await _rows(s, uid)
            same = next(a for a in rows if a.ref == ref[:120])
            return _view(same, rows.index(same) + 1, card), False


@bus.subscribe("statement.reconciled", worker="asks-events", service=SVC)
async def on_statement_reconciled(ev: bus.Event) -> None:
    """A statement can show a new reason to ask (a late fee, a rate that went up). One ask per trigger."""
    payload = ev.payload or {}
    uid = payload.get("user_id") or get_ctx().user_id
    card_id, statement_id = payload.get("card_id"), payload.get("statement_id")
    if not uid or not card_id or not statement_id:
        return
    with _as_user(uid):
        for trigger in payload.get("triggers") or []:
            type_ = trigger.get("type")
            if type_ not in TYPES:
                continue
            facts: dict = {}
            if type_ in ("late_fee_refund", "annual_fee"):
                facts["fee_cents"] = _int(trigger.get("value_cents"), 1, 100_000)
                month = trigger.get("month") or trigger.get("close_date") or payload.get("close_date")
                facts["fee_month"] = _month_name(month)
            try:
                await _add(type_, card_id, ref=f"statement:{statement_id}:{type_}", source="statement", title=trigger.get("title"),
                           detail=trigger.get("detail"), value_cents=_int(trigger.get("value_cents"), 0, 50_000_000), facts=facts)
            except NotFound:
                return  # the card was removed after the statement was uploaded


def _month_name(value) -> str | None:
    """"2026-12-11" or "2026-12" -> "December"; anything else that looks like a month name is kept."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return f"{dt.date.fromisoformat((text + '-01')[:10]):%B}"
    except ValueError:
        return text[:20] if text.isalpha() else None


class SuggestReq(BaseModel):
    card_id: str
    type: str
    title: str | None = Field(default=None, max_length=160)
    detail: str | None = Field(default=None, max_length=300)
    value_cents: int | None = Field(default=None, ge=0, le=50_000_000)
    ref: str = Field(min_length=1, max_length=100)


@router.post("/suggest")
async def suggest(req: SuggestReq):
    """Another service found a reason to ask. The same `ref` twice is the same ask."""
    ask, created = await _add(req.type, req.card_id, ref=f"suggest:{req.ref}", source="suggest", title=req.title, detail=req.detail,
                              value_cents=req.value_cents,
                              facts={"fee_cents": req.value_cents} if req.type in ("late_fee_refund", "annual_fee") else {})
    return {"ask": ask, "created": created}


# ---------- reading ----------

@router.get("")
async def list_asks():
    return await _queue()


async def _playbook(s, issuer: str | None, type_: str | None) -> dict:
    since = (real_now().date() - dt.timedelta(days=PLAYBOOK_WINDOW_DAYS)).isoformat()[:7]
    q = select(PlaybookRow).where(PlaybookRow.month >= since)
    if issuer:
        q = q.where(PlaybookRow.issuer == issuer)
    if type_:
        q = q.where(PlaybookRow.type == type_)
    cells: dict[tuple[str, str], dict] = {}
    for row in (await s.execute(q)).scalars().all():
        cell = cells.setdefault((row.channel, row.result), {"channel": row.channel, "result": row.result, "n": 0, "month": row.month})
        cell["n"] += row.n
        cell["month"] = max(cell["month"], row.month)  # the latest month with a report
    count = sum(c["n"] for c in cells.values())
    entries = sorted(cells.values(), key=lambda c: (-c["n"], c["channel"], c["result"]))
    if count >= PLAYBOOK_MIN:
        for cell in entries:
            cell["share"] = round(cell["n"] / count, 3)
        note = f"Self-reported by Burn Rate users; {count} reports in the last 12 months, refusals and unanswered calls included."
    else:
        note = PLAYBOOK_NOTE_FEW
    return {"count": count, "entries": entries, "note": note}


@router.get("/playbook")
async def playbook(issuer: str | None = None, type: str | None = None):  # the query parameter is called `type`
    async with session(SVC) as s:
        return await _playbook(s, issuer, type)


@router.get("/meter")
async def meter():
    """Confirmed savings for the "paid for itself" meter. A refund counts once. A lower rate counts every day since:
    today's balance x (old rate - new rate) / 365."""
    uid, on = get_ctx().user_id, today()
    cards = await _cards_or_empty()
    async with session(SVC) as s:
        rows = (await s.execute(select(OutcomeRow).where(OutcomeRow.user_id == uid, OutcomeRow.applied.is_(True),
                                                         OutcomeRow.result.in_(POSITIVE)).order_by(OutcomeRow.on, OutcomeRow.created_at))).scalars().all()
    items: list[dict] = []
    for o in rows:
        name = (cards.get(o.card_id) or {}).get("name") or o.card_name or "your card"
        if o.refund_cents:
            what = {"late_fee_refund": "Late fee refunded", "annual_fee": "Annual fee credit"}.get(o.type, "Refund")
            items.append({"label": f"{what} on {name}", "cents": o.refund_cents, "kind": "confirmed", "date": o.on.isoformat()})
        if o.new_apr_bps is not None and o.old_apr_bps is not None and o.old_apr_bps > o.new_apr_bps:
            days = max(0, (min(on, o.until) if o.until else on).toordinal() - o.on.toordinal())
            balance = cards[o.card_id]["balance_cents"] if o.card_id in cards else (o.balance_cents or 0)
            cents = round_cents(Fraction(balance * (o.old_apr_bps - o.new_apr_bps) * days, 10_000 * 365))
            items.append({"label": f"Lower rate on {name}: {pct(o.old_apr_bps)} to {pct(o.new_apr_bps)}, {days} {'day' if days == 1 else 'days'} so far",
                          "cents": cents, "kind": "confirmed", "date": o.on.isoformat()})
    return {"items": items, "confirmed_cents": sum(i["cents"] for i in items)}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        by_status = dict((await s.execute(select(AskRow.status, func.count()).group_by(AskRow.status))).all())
        by_type = dict((await s.execute(select(AskRow.type, func.count()).group_by(AskRow.type))).all())
        by_result = dict((await s.execute(select(OutcomeRow.result, func.count()).where(OutcomeRow.applied.is_(True)).group_by(OutcomeRow.result))).all())
        sprints = (await s.execute(select(func.count()).select_from(SprintRow))).scalar_one()
        reports = (await s.execute(select(func.coalesce(func.sum(PlaybookRow.n), 0)))).scalar_one()
    return {"asks": sum(by_status.values()), "by_status": by_status, "by_type": by_type, "outcomes": sum(by_result.values()),
            "by_result": by_result, "sprints": sprints, "playbook_reports": int(reports)}


# ---------- the script, the written version and the call card ----------

def _numbers(a: AskRow, live: dict | None) -> dict:
    """The person's own numbers for this ask: today's from the cards service, else the ones stored with the ask."""
    card = live or {}
    balance = card["balance_cents"] if "balance_cents" in card else (a.facts.get("balance_cents") or 0)
    apr = card["apr_bps"] if "apr_bps" in card else (a.facts.get("apr_bps") or 0)
    return {"card_name": card.get("name") or a.card_name or "your card", "last4": card.get("last4") or "", "balance_cents": balance, "apr_bps": apr,
            "per_point_cents": yearly_value_of_rate_cut(balance, 100), "yearly_interest_cents": yearly_value_of_rate_cut(balance, apr),
            "fee_cents": a.facts.get("fee_cents"), "fee_month": a.facts.get("fee_month"), "autopay": a.facts.get("autopay"),
            "due_day": a.facts.get("due_day") or card.get("due_day"), "target_day": a.facts.get("target_day"),
            "payday_day": a.facts.get("payday_day"), "gap_days": a.facts.get("gap_days"), "largest": bool(a.facts.get("largest"))}


def _request_lines(type_: str, n: dict) -> tuple[str, str, str]:
    """(the request, the reason, the refusal branch) in the cardholder's own voice. Reasons are only ones we know
    to be true for this person; where we cannot know, the line is a bracket for them to fill in."""
    own_words = "[Say in one sentence what happened, in your own words.]"
    if type_ == "apr_reduction":
        return (f"I'm carrying a balance of {money(n['balance_cents'])} at {pct(n['apr_bps'])}. I'd like to ask you to lower my purchase APR.",
                f"At this rate the balance costs me about {money(n['yearly_interest_cents'])} a year in interest, and I'm working on paying it down.",
                "I understand. What would make this account eligible for a lower rate, and when should I ask again?")
    if type_ == "late_fee_refund":
        fee = f"a {money(n['fee_cents'])} late fee" if n["fee_cents"] else "a late fee"
        reason = "I've set up automatic payments, so I don't expect to be late again." if n["autopay"] in ("min", "full") else own_words
        return (f"I was charged {fee} on my {n['fee_month'] or '[month]'} statement. I'd like to ask you to refund it.", reason,
                "I understand. Is a partial refund possible, and when can I ask again?")
    if type_ == "annual_fee":
        fee = f" of {money(n['fee_cents'])}" if n["fee_cents"] else ""
        return ((f"This card has an annual fee{fee}. I'd like to ask whether you can waive or reduce it, whether there is a retention "
                 "offer, or whether I can switch to a version of the card with no annual fee."),
                "I'm deciding whether the card is still worth the fee for me.",
                "I understand. Is there a version of this card with no annual fee that I can switch to, and when can I ask about the fee again?")
    if type_ == "due_date_move":
        due = f"on the {ordinal(n['due_day'])} of each month" if n["due_day"] else "on [day] of each month"
        target = f"the {ordinal(n['target_day'])}" if n["target_day"] else "[day]"
        payday = f"I get paid on the {ordinal(n['payday_day'])}, so" if n["payday_day"] else "For me,"
        return (f"My payment is due {due}. I'd like to move the due date to {target}.",
                f"{payday} a due date a few days after payday is easier to meet.",
                "I understand. Which due dates are available on this account, and when can I ask again?")
    return ("I'm not able to cover my minimum payments right now. I'd like to ask what hardship arrangements are available on this account.",
            own_words.replace("happened", "changed"),
            "I understand. Is there someone else I can speak with about hardship options, and when should I ask again?")


def _why(type_: str, n: dict) -> str:
    if type_ == "apr_reduction":
        lead = ("This is your largest balance, so each point off the rate is worth the most here:" if n["largest"]
                else "Each point off the rate on this card is worth")
        return f"{lead} {money(n['per_point_cents'])} a year at today's balance. Asking takes one call or one message."
    if type_ == "late_fee_refund":
        worth = f"It is worth {money(n['fee_cents'])} if the issuer agrees." if n["fee_cents"] else "It is worth the amount of the fee if the issuer agrees."
        return f"A late fee is one specific charge, so it is the simplest request to make and it goes first. {worth}"
    if type_ == "annual_fee":
        return "An annual fee is charged whether or not you use the card. Make this request within about 30 days of the fee posting."
    if type_ == "due_date_move":
        if n["due_day"] and n["payday_day"] and n["gap_days"] is not None:
            return (f"Your payment on this card is due on the {ordinal(n['due_day'])}, {n['gap_days']} days after your payday on the "
                    f"{ordinal(n['payday_day'])}. A due date a few days after payday is easier to meet.")
        return "A due date a few days after payday is easier to meet."
    return ("This request is only for a time when the minimum payments are out of reach. It changes the terms of the account, "
            "so the questions matter more than the offer.")


def _call_card(type_: str, opening: str, request: str, must_ask: tuple[str, ...], if_refused: str) -> dict:
    """A tap-through tree for the call itself. Nothing is recorded; each end node says which outcome to log."""
    notes = {"apr_reduction": "the new rate, whether it is permanent or until what date, and which balances it covers",
             "late_fee_refund": "the amount refunded and when it will show on a statement",
             "annual_fee": "the offer and the form it takes",
             "due_date_move": "the new due date and the billing cycle it starts with",
             "hardship": "the rate, the number of months, what happens to the card, and how you will get the terms in writing"}[type_]
    smaller = type_ in ("late_fee_refund", "annual_fee")
    end = {"say": "Thank you. Could I have your name for my notes?", "options": []}
    nodes: dict[str, dict] = {
        "hello": {"say": opening,
                  "hint": "Use the phone number on the back of your card. They will ask a few questions to identify you first.",
                  "options": [{"label": "I'm talking to a representative", "next": "ask"},
                              {"label": "Nobody answered, or the call dropped", "next": "no_answer"}]},
        "ask": {"say": request, "hint": "Say it once, then wait for the answer.",
                "options": [{"label": "They said yes", "next": "yes"},
                            {"label": "They offered part of it, or something temporary" if smaller else "They made a temporary offer", "next": "temporary"},
                            {"label": "They said no", "next": "no"},
                            {"label": "They are transferring me to another department", "next": "transfer"}]},
        "yes": {"say": " ".join(must_ask), "hint": "Ask before you accept, and write the answers down.",
                "options": [{"label": "The answers are fine: I accept", "next": "granted"},
                            {"label": "It turns out to be temporary", "next": "temporary"},
                            {"label": "It needs a hard inquiry: I decline", "next": "declined"}]},
        "temporary": {"say": f"What exactly is the offer, how long does it last, and what happens after that? {Q_HARD_INQUIRY}",
                      "hint": "A smaller or temporary offer still counts. You can accept it and ask again later.",
                      "options": [{"label": "I accept the offer", "next": "accepted_offer"}, {"label": "I'd rather not take it", "next": "no"}]},
        "no": {"say": if_refused, "hint": "A no today is an answer for today. Write down the reason and the date they give you.",
               "options": [{"label": "They told me what would qualify or when to ask again", "next": "refused"},
                           {"label": "They are transferring me to another department", "next": "transfer"}]},
        "transfer": {"say": "Thank you. Before you transfer me, could I have your name?",
                     "hint": "A transfer is normal: the first person often cannot change the account. When someone new answers, make the request again from the start.",
                     "options": [{"label": "Someone new answered", "next": "ask"}, {"label": "The call dropped", "next": "no_answer"}]},
        "granted": {**end, "hint": f"Log it now: the name and today's date; {notes}; the answer about a hard inquiry.", "outcome": "granted"},
        "accepted_offer": {**end, "hint": f"Log it now: the name and today's date; {notes}; the date it ends.",
                           "outcome": "partial" if smaller else "temporary"},
        "refused": {**end, "hint": "Log the refusal with the name, the reason, and the date they said to ask again.", "outcome": "refused"},
        "declined": {"say": "Then I'd like to leave the account as it is. Thank you for checking.",
                     "hint": "Log it as refused and note that a hard inquiry was required.", "options": [], "outcome": "refused"},
        "no_answer": {"say": "", "hint": "Log it as no answer and try at another time of day. The request stays in your queue.",
                      "options": [], "outcome": "no_answer"},
    }
    if type_ == "hardship":  # the four questions come before any yes
        steps = ["terms_rate", "terms_card", "terms_reporting", "terms_missed"]
        for i, (node, question) in enumerate(zip(steps, Q_HARDSHIP)):
            nodes[node] = {"say": question, "hint": f"Question {i + 1} of 4 before you agree to anything. Write the answer down.",
                           "options": [{"label": "They answered", "next": steps[i + 1] if i + 1 < len(steps) else "terms_writing"}]}
        nodes["terms_writing"] = {"say": f"{Q_IN_WRITING} {Q_HARD_INQUIRY}",
                                  "hint": "You can ask for the terms in writing before the arrangement starts. Take your time.",
                                  "options": [{"label": "I accept the arrangement", "next": "accepted_offer"},
                                              {"label": "I'm not accepting it today", "next": "declined"}]}
        nodes["ask"]["options"][0] = {"label": "They said yes and described an arrangement", "next": "terms_rate"}
        nodes["ask"]["options"][1] = {"label": "They made a temporary offer", "next": "terms_rate"}
        nodes["declined"]["hint"] = "Log it as refused and note why you did not accept."
        del nodes["yes"], nodes["temporary"], nodes["granted"]  # every path to a yes goes through the questions
    return {"start": "hello", "nodes": nodes}


def build_detail(view: dict, type_: str, issuer: str, n: dict, playbook_view: dict) -> dict:
    """`AskDetail` of docs/API.md from the list view and the person's numbers. Pure."""
    ending = f" ending in {n['last4']}" if n["last4"] else ""
    opening = f"Hi, my name is [your name]. I'm calling about my {n['card_name']} card{ending}."
    request, reason, if_refused = _request_lines(type_, n)
    must_ask = MUST_ASK[type_]
    script = {"opening": opening,
              "lines": [{"label": "Your history", "text": "I've had this card since [year you opened it]."},
                        {"label": "The request", "text": request},
                        {"label": "Your reason", "text": reason},
                        {"label": "Before you agree", "text": " ".join(must_ask)},
                        {"label": "For your notes", "text": "Could I have your name, please? I'm writing down today's date and what was agreed."}],
              "must_ask": list(must_ask), "if_refused": if_refused}
    written = (f"Hello. I'm writing about my {n['card_name']} card{ending}. {request} "
               f"Before anything changes on the account, please answer these questions: {' '.join(must_ask)} Thank you.")
    evidence = {k: v for k, v in EVIDENCE[type_].items() if k != "numbers"}
    if type_ == "late_fee_refund" and issuer == "discover":
        evidence["text"] += _DISCOVER_FIRST_LATE
        evidence["source"] += "; Discover cardmember agreement, June 30, 2026"
    return {**view, "why": _why(type_, n), "evidence": evidence, "script": script, "written": written,
            "call_card": _call_card(type_, opening, request, must_ask, if_refused), "playbook": playbook_view,
            "facts": {k: n[k] for k in ("balance_cents", "apr_bps", "per_point_cents", "yearly_interest_cents", "fee_cents", "fee_month",
                                        "due_day", "target_day", "payday_day", "last4")}}


def detail_texts(detail: dict) -> list[str]:
    """Every string of an ask detail that a person will read or say; what the wording rules are checked against."""
    script, card = detail["script"], detail["call_card"]
    texts = [detail["title"], detail["subtitle"], detail["value_label"], detail.get("result") or "", detail["why"],
             *detail["evidence"].values(), script["opening"], script["if_refused"], *script["must_ask"], detail["written"]]
    for line in script["lines"]:
        texts += [line["label"], line["text"]]
    for node in card["nodes"].values():
        texts += [node["say"], node.get("hint", ""), *[o["label"] for o in node["options"]]]
    return [t for t in texts if t]


@router.get("/{ask_id}")
async def get_ask(ask_id: str):
    live = await _cards_or_empty()
    uid = get_ctx().user_id
    async with session(SVC) as s:
        ask = await _own(s, ask_id)
        rows = await _rows(s, uid)
        view = _view(ask, [a.id for a in rows].index(ask.id) + 1, live.get(ask.card_id))
        detail = build_detail(view, ask.type, ask.issuer, _numbers(ask, live.get(ask.card_id)), await _playbook(s, ask.issuer, ask.type))
    broken = [v.model_dump() for text in detail_texts(detail) for v in check_text(text).violations]
    if broken:  # only a card name or a trigger text typed elsewhere can cause this; the templates are tested
        log.warning("ask %s has wording that breaks the rules: %s", ask_id, broken)
    return detail


# ---------- outcomes ----------

class OutcomeReq(BaseModel):
    result: Literal["granted", "partial", "temporary", "refused", "no_answer"]
    channel: Literal["phone", "chat", "message"] = "phone"
    new_apr_bps: int | None = Field(default=None, ge=0, le=7_999)
    until: dt.date | None = None
    refund_cents: int | None = Field(default=None, ge=0, le=1_000_000)
    rep_name: str | None = Field(default=None, max_length=60)
    hard_pull: Literal["yes", "no", "unknown"] | None = None
    ask_again_on: dt.date | None = None
    note: str | None = Field(default=None, max_length=500)


def result_line(type_: str, result: str, *, old_apr_bps: int | None, new_apr_bps: int | None, refund_cents: int | None,
                until: dt.date | None, ask_again_on: dt.date | None, on: dt.date) -> str:
    if result == "no_answer":
        return "No answer. Try again at another time of day."
    if result == "refused":
        return "Refused." + (f" Ask again on {short_date(ask_again_on, on)}." if ask_again_on else "")
    parts: list[str] = []
    if new_apr_bps is not None and old_apr_bps is not None:
        parts.append(f"Rate lowered from {pct(old_apr_bps)} to {pct(new_apr_bps)}" + (f" until {short_date(until, on)}" if until else ""))
    if refund_cents:
        what = {"late_fee_refund": "Late fee", "annual_fee": "Annual fee"}.get(type_, "Fee")
        parts.append(f"{what} refunded: {money(refund_cents)}" if result == "granted" else f"{money(refund_cents)} refunded")
    if not parts:
        plain = {"due_date_move": "Due date moved", "annual_fee": "Annual fee offer accepted", "hardship": "Hardship arrangement agreed",
                 "late_fee_refund": "Late fee refunded", "apr_reduction": "Rate lowered"}[type_]
        parts.append({"granted": plain, "partial": "Partly granted", "temporary": "Temporary offer accepted"}[result]
                     + (f" until {short_date(until, on)}" if until and result != "granted" else ""))
    return ". ".join(parts)


def _outcome_view(o: OutcomeRow) -> dict:
    return {"id": o.id, "ask_id": o.ask_id, "result": o.result, "channel": o.channel, "new_apr_bps": o.new_apr_bps, "old_apr_bps": o.old_apr_bps,
            "until": o.until.isoformat() if o.until else None, "refund_cents": o.refund_cents, "rep_name": o.rep_name or None,
            "hard_pull": o.hard_pull, "ask_again_on": o.ask_again_on.isoformat() if o.ask_again_on else None, "note": o.note or None,
            "date": o.on.isoformat()}


async def _answer(ask_id: str, outcome_id: str, duplicate: bool) -> dict:
    live = await _cards_or_empty()
    uid = get_ctx().user_id
    async with session(SVC) as s:
        ask = await _own(s, ask_id)
        rows = await _rows(s, uid)
        outcome = await s.get(OutcomeRow, outcome_id)
        return {"ask": _view(ask, [a.id for a in rows].index(ask.id) + 1, live.get(ask.card_id)), "outcome": _outcome_view(outcome),
                "duplicate": duplicate}


@router.post("/{ask_id}/outcome")
async def log_outcome(ask_id: str, req: OutcomeReq):
    """Record what the issuer answered. A lower rate goes to the cards service as a rate change, a refund as a credit,
    both under the ask id, and both exactly once however many times this is called."""
    uid, on = get_ctx().user_id, today()
    final = req.result != "no_answer"
    dedupe = ask_id if final else f"{ask_id}:{on.isoformat()}"
    async with session(SVC) as s:
        ask = await _own(s, ask_id)
        stored = (await s.execute(select(OutcomeRow).where(OutcomeRow.dedupe.in_([ask_id, dedupe])).order_by(OutcomeRow.created_at))).scalars().all()
        closed = next((o for o in stored if o.dedupe == ask_id), None)  # a final answer already logged for this ask
        current = closed or next((o for o in stored if o.dedupe == dedupe), None)
        type_, card_id, card_name, issuer = ask.type, ask.card_id, ask.card_name, ask.issuer
    if current is not None and current.applied:
        return await _answer(ask_id, current.id, duplicate=True)

    if current is None:
        # If the cards service is away this fails and nothing is stored: better than logging a rate that never
        # reached the ledger. A card the person has since removed is simply absent, and then there is no effect.
        card = {c["id"]: c for c in await _cards()}.get(card_id)
        positive = req.result in POSITIVE
        new_apr = req.new_apr_bps if positive else None
        refund = (req.refund_cents or None) if positive else None
        old_apr = card["apr_bps"] if card and new_apr is not None else None
        if new_apr is not None and old_apr is not None and new_apr >= old_apr:
            raise DomainError(f"The new rate must be lower than the current rate of {pct(old_apr)}.", code="not_a_reduction", status=422)
        current = OutcomeRow(id=new_id(), dedupe=dedupe, ask_id=ask_id, user_id=uid, card_id=card_id, card_name=(card or {}).get("name") or card_name,
                             issuer=issuer, type=type_, result=req.result, channel=req.channel, new_apr_bps=new_apr, old_apr_bps=old_apr,
                             until=req.until if positive else None, refund_cents=refund, balance_cents=card["balance_cents"] if card else None,
                             rep_name=(req.rep_name or "").strip(), hard_pull=req.hard_pull or "unknown",
                             ask_again_on=req.ask_again_on, note=(req.note or "").strip(), on=on, applied=False, created_at=real_now())
        try:
            async with session(SVC) as s:
                s.add(current)
        except IntegrityError:  # the same outcome, logged by a concurrent request: that request applies it
            async with session(SVC) as s:
                other = (await s.execute(select(OutcomeRow).where(OutcomeRow.dedupe == dedupe))).scalar_one()
            return await _answer(ask_id, other.id, duplicate=True)

    # Effects on the ledger. Both are safe to repeat: setting a rate to the same value, and a credit under the same ref.
    try:
        if current.new_apr_bps is not None and current.old_apr_bps is not None:
            await clients.post("cards", f"/internal/cards/{card_id}/rate", {
                "apr_bps": current.new_apr_bps, "source": "ask", "ref": ask_id,
                "note": f"Issuer agreed to {pct(current.new_apr_bps)}, was {pct(current.old_apr_bps)}"})
        if current.refund_cents:
            await clients.post("cards", f"/internal/cards/{card_id}/events", {
                "type": "credit", "amount_cents": current.refund_cents, "source": "ask", "ref": ask_id,
                "note": {"late_fee_refund": "Late fee refunded by the issuer", "annual_fee": "Annual fee credited by the issuer"}.get(type_, "Refund from the issuer")})
    except DomainError as e:
        if e.code != "card_not_found":  # a removed card has no ledger to update; anything else is retried by calling again
            raise

    async with session(SVC) as s:
        outcome = await s.get(OutcomeRow, current.id)
        ask = await s.get(AskRow, ask_id)
        if not outcome.applied:
            outcome.applied = True
            ask.result = result_line(type_, outcome.result, old_apr_bps=outcome.old_apr_bps, new_apr_bps=outcome.new_apr_bps,
                                     refund_cents=outcome.refund_cents, until=outcome.until, ask_again_on=outcome.ask_again_on, on=outcome.on)
            if outcome.result != "no_answer":  # nobody answered: the request stays in the queue
                ask.status, ask.done_at = "done", real_now()
            await bus.emit(s, SVC, "ask.outcome.logged", {
                "user_id": uid, "ask_id": ask_id, "outcome_id": outcome.id, "card_id": card_id, "issuer": issuer, "type": type_,
                "channel": outcome.channel, "result": outcome.result, "new_apr_bps": outcome.new_apr_bps, "old_apr_bps": outcome.old_apr_bps,
                "until": outcome.until.isoformat() if outcome.until else None, "refund_cents": outcome.refund_cents,
                "ask_again_on": outcome.ask_again_on.isoformat() if outcome.ask_again_on else None, "hard_pull": outcome.hard_pull,
                "on": outcome.on.isoformat(), "demo": get_ctx().demo})
    return await _answer(ask_id, current.id, duplicate=False)


@router.post("/{ask_id}/skip")
async def skip(ask_id: str):
    live = await _cards_or_empty()
    uid = get_ctx().user_id
    async with session(SVC) as s:
        ask = await _own(s, ask_id)
        if ask.status == "done":
            raise Conflict("This request already has an outcome.", code="ask_already_done")
        ask.status = "skipped"
        rows = await _rows(s, uid)
        return _view(ask, [a.id for a in rows].index(ask.id) + 1, live.get(ask.card_id))


@bus.subscribe("ask.outcome.logged", worker="playbook-aggregator", service=SVC)
async def on_outcome_logged(ev: bus.Event) -> None:
    """Add one report to the playbook. Demo sessions are practice, not reports about a real issuer."""
    p = ev.payload or {}
    if p.get("demo") or get_ctx().demo or not p.get("issuer") or p.get("type") not in TYPES:
        return
    key = (p["issuer"], p["type"], p.get("channel") or "phone", p.get("result") or "", str(p.get("on") or real_now().date().isoformat())[:7])
    async with session(SVC) as s:
        if await s.get(PlaybookSeen, ev.id):
            return  # delivered twice
        s.add(PlaybookSeen(event_id=ev.id, at=real_now()))
        row = await s.get(PlaybookRow, key)
        if row is None:
            s.add(PlaybookRow(issuer=key[0], type=key[1], channel=key[2], result=key[3], month=key[4], n=1))
        else:
            row.n += 1


# ---------- demo data ----------

class SeedAsk(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=32)  # used as given; must not belong to another user
    key: str | None = Field(default=None, min_length=1, max_length=60)  # or a name of the caller's choosing: the id is derived per user
    type: Literal["late_fee_refund", "apr_reduction", "annual_fee", "due_date_move", "hardship"]
    card_id: str
    card_name: str = ""
    issuer: str = "other"
    title: str | None = None
    subtitle: str | None = None
    value_cents: int | None = None
    value_label: str | None = None
    status: Literal["queued", "done", "skipped"] = "queued"
    order: int | None = None
    result: str | None = None
    created_on: dt.date | None = None
    done_on: dt.date | None = None
    facts: dict = Field(default_factory=dict)  # fee_cents, fee_month, balance_cents, apr_bps, due_day, target_day, payday_day


class SeedOutcome(BaseModel):
    id: str | None = None
    ask_id: str | None = None
    ask_key: str | None = None  # the `key` of an ask seeded for this user, now or earlier
    result: Literal["granted", "partial", "temporary", "refused", "no_answer"]
    channel: Literal["phone", "chat", "message"] = "phone"
    new_apr_bps: int | None = None
    old_apr_bps: int | None = None
    until: dt.date | None = None
    refund_cents: int | None = None
    balance_cents: int | None = None
    rep_name: str = ""
    hard_pull: Literal["yes", "no", "unknown"] = "unknown"
    ask_again_on: dt.date | None = None
    note: str = ""
    date: dt.date | None = None


class SeedReq(BaseModel):
    asks: list[SeedAsk] = Field(default_factory=list)
    outcomes: list[SeedOutcome] = Field(default_factory=list)
    sprint_started_on: dt.date | None = None


def _seed_id(user_id: str, key: str) -> str:
    """Every demo guest gets the same persona, so a seeded row's id is derived from the user and the caller's key."""
    return hashlib.sha256(f"{user_id}:{key}".encode()).hexdigest()[:32]


@router.post("/seed")
async def seed(req: SeedReq):
    """Ready-made history for the demo persona: rows exactly as given. Nothing is sent to the cards service and no
    event is written, so the ledger and the playbook are untouched. Rows that already exist are left alone."""
    uid, on = get_ctx().user_id, today()
    live = await _cards_or_empty()
    added = {"asks": 0, "outcomes": 0}
    async with session(SVC) as s:
        position = max([a.position for a in await _rows(s, uid)], default=0)
        by_id: dict[str, AskRow] = {}
        for item in req.asks:
            if not (item.id or item.key):
                raise DomainError("a seeded ask needs an id or a key", code="invalid_input", status=422)
            ask_id = item.id or _seed_id(uid, item.key)
            existing = await s.get(AskRow, ask_id)
            if existing:
                if existing.user_id != uid:
                    raise Conflict(f"ask id {ask_id} belongs to another user; seed with `key` instead", code="ask_id_taken")
                by_id[ask_id] = existing
                continue
            card = {"id": item.card_id, "name": item.card_name, "issuer": item.issuer, **(live.get(item.card_id) or {})}
            card.setdefault("balance_cents", item.facts.get("balance_cents") or 0)
            card.setdefault("apr_bps", item.facts.get("apr_bps") or 0)
            facts = dict(item.facts)
            if item.type in ("late_fee_refund", "annual_fee") and item.value_cents:
                facts.setdefault("fee_cents", item.value_cents)
            position += 1
            row = _new_ask(uid, item.type, card, facts, ref=f"seed:{ask_id}", source="seed",
                           position=item.order if item.order is not None else position,
                           on=item.created_on or on, title=item.title, subtitle=item.subtitle, value_cents=item.value_cents)
            row.id, row.status, row.result = ask_id, item.status, item.result
            row.issuer = item.issuer if item.issuer != "other" else row.issuer
            if item.value_label:
                row.value_label = item.value_label
            if item.status == "done":
                done_on = item.done_on or item.created_on or on
                row.done_at = dt.datetime(done_on.year, done_on.month, done_on.day, 12, tzinfo=dt.UTC)
            s.add(row)
            by_id[ask_id] = row
            added["asks"] += 1
        await s.flush()
        for item in req.outcomes:
            ask_id = item.ask_id or (_seed_id(uid, item.ask_key) if item.ask_key else "")
            ask = by_id.get(ask_id) or (await s.get(AskRow, ask_id) if ask_id else None)
            if ask is None or ask.user_id != uid:
                raise NotFound(f"outcome refers to an unknown ask: {item.ask_id or item.ask_key}", code="ask_not_found")
            when = item.date or on
            dedupe = ask.id if item.result != "no_answer" else f"{ask.id}:{when.isoformat()}"
            if (item.id and await s.get(OutcomeRow, item.id)) or (await s.execute(select(OutcomeRow).where(OutcomeRow.dedupe == dedupe))).first():
                continue
            s.add(OutcomeRow(id=item.id or new_id(), dedupe=dedupe, ask_id=ask.id, user_id=uid, card_id=ask.card_id, card_name=ask.card_name,
                             issuer=ask.issuer, type=ask.type, result=item.result, channel=item.channel, new_apr_bps=item.new_apr_bps,
                             old_apr_bps=item.old_apr_bps, until=item.until, refund_cents=item.refund_cents, balance_cents=item.balance_cents,
                             rep_name=item.rep_name, hard_pull=item.hard_pull, ask_again_on=item.ask_again_on, note=item.note, on=when,
                             applied=True, created_at=real_now()))
            if ask.result is None:
                ask.result = result_line(ask.type, item.result, old_apr_bps=item.old_apr_bps, new_apr_bps=item.new_apr_bps,
                                         refund_cents=item.refund_cents, until=item.until, ask_again_on=item.ask_again_on, on=when)
            added["outcomes"] += 1
        if req.asks and await s.get(SprintRow, uid) is None:
            started = req.sprint_started_on or min((a.created_on for a in req.asks if a.created_on), default=on)
            s.add(SprintRow(user_id=uid, started_on=started, ends_on=started + dt.timedelta(days=SPRINT_DAYS), created_at=real_now()))
    return {**await _queue(live), "added": added}
