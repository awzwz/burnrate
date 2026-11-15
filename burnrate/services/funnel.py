"""funnel: quiz answers and the stored result. The result is computed once and never changes after payment:
the number a person saw before the paywall is the number they get afterwards."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus, clients, persona
from burnrate.common.context import get_ctx, real_now, today
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import DomainError, NotFound
from burnrate.engine.core import usd
from burnrate.engine.io import CardIn, ScheduleIn
from burnrate.services.cards import guess_issuer

SVC = "funnel"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/funnel", tags=["funnel"])


class QuizRow(Base):
    __tablename__ = "quiz"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    cards: Mapped[list] = mapped_column(JSON, default=list)
    schedule: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class ResultRow(Base):
    __tablename__ = "results"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


async def _row(s) -> QuizRow:
    uid = get_ctx().user_id
    q = await s.get(QuizRow, uid)
    if not q:
        q = QuizRow(user_id=uid, answers={}, cards=[], schedule=None, updated_at=real_now())
        s.add(q)
    return q


async def _has_result(s) -> bool:
    return bool((await s.execute(select(func.count()).select_from(ResultRow).where(ResultRow.user_id == get_ctx().user_id))).scalar_one())


async def _view(s, q: QuizRow) -> dict:
    return {"answers": q.answers or {}, "cards": q.cards or [], "schedule": q.schedule, "has_result": await _has_result(s)}


@router.get("/quiz")
async def get_quiz():
    async with session(SVC) as s:
        return await _view(s, await _row(s))


class AnswersReq(BaseModel):
    answers: dict


@router.put("/quiz/answers")
async def put_answers(req: AnswersReq):
    if len(str(req.answers)) > 4_000:
        raise DomainError("too many answers", code="answers_too_large")
    async with session(SVC) as s:
        q = await _row(s)
        q.answers = {**(q.answers or {}), **req.answers}
        q.updated_at = real_now()
        return await _view(s, q)


class CardsReq(BaseModel):
    cards: list[CardIn] = Field(min_length=1, max_length=6)


@router.put("/quiz/cards")
async def put_cards(req: CardsReq):
    out = []
    for i, c in enumerate(req.cards):
        d = c.model_dump(mode="json")
        d["id"] = f"c{i + 1}"
        if d["issuer"] == "other":
            d["issuer"] = guess_issuer(d["name"])
        out.append(d)
    async with session(SVC) as s:
        q = await _row(s)
        q.cards, q.updated_at = out, real_now()
        return await _view(s, q)


@router.put("/quiz/schedule")
async def put_schedule(req: ScheduleIn):
    async with session(SVC) as s:
        q = await _row(s)
        q.schedule, q.updated_at = req.model_dump(mode="json"), real_now()
        return await _view(s, q)


@router.post("/quiz/sample")
async def sample():
    """Fill the quiz with the sample persona's numbers, dated from the visitor's own today."""
    d0 = persona.first_payday_on_or_after(today())
    cards = persona.cards(d0)
    for i, c in enumerate(cards):
        c["id"] = f"c{i + 1}"
    async with session(SVC) as s:
        q = await _row(s)
        q.answers = {**(q.answers or {}), **persona.answers(), "sample": True}
        q.cards, q.schedule, q.updated_at = cards, persona.schedule(d0), real_now()
        return await _view(s, q)


class BurnReq(BaseModel):
    balance_cents: int = Field(ge=0, le=50_000_000)
    apr_bps: int = Field(ge=0, le=7_999)


@router.post("/quiz/card-burn")
async def card_burn(req: BurnReq):
    r = await clients.post("engine", "/internal/engine/burn", json={"on": today().isoformat(), "cards": [
        {"id": "x", "name": "card", "balance_cents": req.balance_cents, "apr_bps": req.apr_bps}]})
    return {"burn_per_day": r["burn_per_day"], "per_year_cents": r["per_year_cents"]}


def _ask_preview(answers: dict, cards: list[dict], point_value: dict) -> list[dict]:
    """What the first two weeks will contain, from the user's own answers. Wording promises nothing."""
    out: list[dict] = []
    if answers.get("late_fee_recently"):
        fee = int(answers.get("late_fee_cents") or 0)
        card = answers.get("late_fee_card") or "your card"
        out.append({"type": "late_fee_refund", "title": f"Ask for a late fee back: {card}",
                    "detail": "Issuers often waive a first late fee when asked.", "value_label": usd(fee) if fee else "the fee"})
    by_balance = sorted(cards, key=lambda c: -c["balance_cents"])
    for c in by_balance[:2]:
        per_point = round(c["balance_cents"] / 100)
        out.append({"type": "apr_reduction", "title": f"Ask for a lower rate: {c['name']}",
                    "detail": f"Each point off is worth {usd(per_point, 0)} a year on this balance.",
                    "value_label": f"{c['apr_bps'] / 100:.2f}%"})
    return out


@router.post("/quiz/result")
async def make_result():
    async with session(SVC) as s:
        q = await _row(s)
        cards, sched, answers = q.cards or [], q.schedule, q.answers or {}
    if not cards:
        raise DomainError("Add at least one card first.", code="no_cards")
    if not sched:
        raise DomainError("Tell us when you get paid first.", code="no_schedule")
    r = await clients.post("engine", "/internal/engine/quiz-result", json={"cards": cards, "schedule": sched, "today": today().isoformat()})
    r["asks"] = _ask_preview(answers, cards, r["point_value"])
    async with session(SVC) as s:
        row = ResultRow(id=new_id(), user_id=get_ctx().user_id, payload=r, created_at=real_now())
        s.add(row)
        return {"id": row.id, "created_at": row.created_at.isoformat(), **r}


@router.get("/quiz/result")
async def get_result():
    async with session(SVC) as s:
        row = (await s.execute(select(ResultRow).where(ResultRow.user_id == get_ctx().user_id)
                               .order_by(ResultRow.created_at.desc()).limit(1))).scalar_one_or_none()
        if not row:
            raise NotFound("No result yet.", code="no_result")
        return {"id": row.id, "created_at": row.created_at.isoformat(), **row.payload}


@router.get("/export")
async def export():
    """Everything the product needs after payment: cards, pay schedule and answers from the quiz."""
    async with session(SVC) as s:
        q = await _row(s)
        return {"answers": q.answers or {}, "cards": q.cards or [], "schedule": q.schedule}


@bus.subscribe("subscription.activated", worker="onboarding", service=SVC)
async def on_subscribed(ev: bus.Event) -> None:
    """Payment went through: copy the quiz cards into the cards service, then announce it so the plan can start.
    Chain: subscription.activated -> cards.imported -> plan.activated."""
    async with session(SVC) as s:
        q = await _row(s)
        cards, sched, answers = q.cards or [], q.schedule, q.answers or {}
    if not cards:
        return
    await clients.post("cards", "/internal/cards/import", json={"cards": [{**c, "id": ""} for c in cards]})
    async with session(SVC) as s:
        await bus.emit(s, SVC, "cards.imported", {"user_id": get_ctx().user_id, "schedule": sched, "answers": answers})


class AdoptReq(BaseModel):
    from_user_id: str


@router.post("/adopt")
async def adopt(req: AdoptReq):
    """A visitor signed in to an existing account: carry the quiz they just filled over to it."""
    uid = get_ctx().user_id
    async with session(SVC) as s:
        src = await s.get(QuizRow, req.from_user_id)
        if src and not await s.get(QuizRow, uid):
            s.add(QuizRow(user_id=uid, answers=src.answers, cards=src.cards, schedule=src.schedule, updated_at=real_now()))
    return {"ok": True}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        started = (await s.execute(select(func.count()).select_from(QuizRow))).scalar_one()
        results = (await s.execute(select(func.count(func.distinct(ResultRow.user_id))))).scalar_one()
    return {"quiz_started": started, "results": results}
