"""cards: the user's cards and the dated ledger behind every balance.

A balance is never stored. It is the sum of ledger events up to a date: opening balance, purchases, fees,
interest, payments, credits and adjustments. A statement always wins: reconciliation adds an adjustment
so the ledger equals the bank's number.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import Date, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus
from burnrate.common.context import get_ctx, real_now, today
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import Conflict, DomainError, NotFound
from burnrate.engine.io import CardIn

SVC = "cards"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/cards", tags=["cards"])

DEBITS = ("opening", "purchase", "fee", "interest")
CREDITS = ("payment", "credit")
TYPES = DEBITS + CREDITS + ("adjustment", "rate_change")
PAY_URLS = {"capital_one": "https://www.capitalone.com", "chase": "https://www.chase.com", "discover": "https://www.discover.com",
            "citi": "https://www.citi.com", "amex": "https://www.americanexpress.com",
            "bank_of_america": "https://www.bankofamerica.com", "wells_fargo": "https://www.wellsfargo.com"}
ISSUER_WORDS = {"capital one": "capital_one", "quicksilver": "capital_one", "venture": "capital_one", "savor": "capital_one",
                "chase": "chase", "freedom": "chase", "sapphire": "chase", "discover": "discover", "citi": "citi",
                "amex": "amex", "american express": "amex", "bank of america": "bank_of_america", "bofa": "bank_of_america",
                "wells fargo": "wells_fargo"}


def guess_issuer(name: str) -> str:
    low = name.lower()
    return next((v for k, v in ISSUER_WORDS.items() if k in low), "other")


class CardRow(Base):
    __tablename__ = "cards"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(60))
    issuer: Mapped[str] = mapped_column(String(32), default="other")
    last4: Mapped[str] = mapped_column(String(4), default="")
    apr_bps: Mapped[int]
    close_day: Mapped[int] = mapped_column(default=1)
    due_day: Mapped[int] = mapped_column(default=25)
    autopay: Mapped[str] = mapped_column(String(8), default="min")
    pay_url: Mapped[str] = mapped_column(String(200), default="")
    statement_min_cents: Mapped[int | None] = mapped_column(nullable=True)
    start_balance_cents: Mapped[int] = mapped_column(default=0)
    opened_on: Mapped[dt.date] = mapped_column(Date)
    position: Mapped[int] = mapped_column(default=0)
    archived_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class LedgerEvent(Base):
    __tablename__ = "ledger"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    card_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(16))
    amount_cents: Mapped[int] = mapped_column(default=0)  # always positive; the type gives the direction
    apr_bps: Mapped[int | None] = mapped_column(nullable=True)  # rate_change: the new rate
    effective_date: Mapped[dt.date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(16), default="user")  # user | statement | reconcile | demo | ask | plan
    ref: Mapped[str] = mapped_column(String(64), default="")  # instruction id, statement id, ask id: makes writes idempotent
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


def signed(e: LedgerEvent) -> int:
    if e.type in DEBITS:
        return e.amount_cents
    if e.type in CREDITS:
        return -e.amount_cents
    if e.type == "adjustment":
        return e.amount_cents  # may be negative
    return 0


async def _balance(s, card_id: str, on: dt.date) -> int:
    rows = (await s.execute(select(LedgerEvent).where(LedgerEvent.card_id == card_id, LedgerEvent.effective_date <= on))).scalars().all()
    return max(0, sum(signed(e) for e in rows))


async def _own(s, card_id: str) -> CardRow:
    c = await s.get(CardRow, card_id)
    if not c or c.user_id != get_ctx().user_id or c.archived_at:
        raise NotFound("card not found", code="card_not_found")
    return c


async def _view(s, c: CardRow, on: dt.date) -> dict:
    bal = await _balance(s, c.id, on)
    start = c.start_balance_cents or bal
    return {"id": c.id, "name": c.name, "issuer": c.issuer, "last4": c.last4, "balance_cents": bal, "apr_bps": c.apr_bps,
            "close_day": c.close_day, "due_day": c.due_day, "autopay": c.autopay, "pay_url": c.pay_url or PAY_URLS.get(c.issuer, ""),
            "statement_min_cents": c.statement_min_cents, "start_balance_cents": start,
            "progress": round(max(0.0, min(1.0, 1 - bal / start)), 4) if start else 1.0, "paid_off": bal == 0,
            "opened_on": c.opened_on.isoformat(), "position": c.position}


async def _changed(s, card_id: str, reason: str) -> None:
    await bus.emit(s, SVC, "card.ledger.changed", {"user_id": get_ctx().user_id, "card_id": card_id, "reason": reason})


@router.get("")
async def list_cards(on: dt.date | None = None):
    on = on or today()
    async with session(SVC) as s:
        rows = (await s.execute(select(CardRow).where(CardRow.user_id == get_ctx().user_id, CardRow.archived_at.is_(None))
                                .order_by(CardRow.position, CardRow.created_at))).scalars().all()
        return {"on": on.isoformat(), "cards": [await _view(s, c, on) for c in rows]}


class NewCard(CardIn):
    opened_on: dt.date | None = None
    source: str = "user"


async def _create(s, req: NewCard, position: int) -> CardRow:
    uid = get_ctx().user_id
    opened = req.opened_on or today()
    issuer = req.issuer if req.issuer != "other" else guess_issuer(req.name)
    c = CardRow(id=req.id or new_id(), user_id=uid, name=req.name.strip(), issuer=issuer, last4=req.last4[-4:], apr_bps=req.apr_bps,
                close_day=req.close_day, due_day=req.due_day, autopay=req.autopay, pay_url=req.pay_url,
                statement_min_cents=req.statement_min_cents, start_balance_cents=req.balance_cents, opened_on=opened,
                position=position, created_at=real_now())
    s.add(c)
    s.add(LedgerEvent(card_id=c.id, user_id=uid, type="opening", amount_cents=req.balance_cents, effective_date=opened,
                      source=req.source, note="Balance when the card was added", created_at=real_now()))
    return c


@router.post("")
async def create_card(req: NewCard):
    async with session(SVC) as s:
        n = (await s.execute(select(func.count()).select_from(CardRow).where(CardRow.user_id == get_ctx().user_id, CardRow.archived_at.is_(None)))).scalar_one()
        if n >= 8:
            raise Conflict("You can track up to 8 cards.", code="too_many_cards")
        c = await _create(s, req, n)
        await _changed(s, c.id, "card_added")
        await s.flush()
        return await _view(s, c, today())


class ImportReq(BaseModel):
    cards: list[NewCard] = Field(min_length=1, max_length=8)


@router.post("/import")
async def import_cards(req: ImportReq):
    """Copy the cards entered in the quiz. Idempotent: a user who already has cards keeps them."""
    async with session(SVC) as s:
        n = (await s.execute(select(func.count()).select_from(CardRow).where(CardRow.user_id == get_ctx().user_id))).scalar_one()
        if n == 0:
            for i, card in enumerate(req.cards):
                c = await _create(s, card, i)
                await _changed(s, c.id, "imported")
        await s.flush()
        rows = (await s.execute(select(CardRow).where(CardRow.user_id == get_ctx().user_id, CardRow.archived_at.is_(None)).order_by(CardRow.position))).scalars().all()
        return {"imported": n == 0, "cards": [await _view(s, c, today()) for c in rows]}


class PatchCard(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    issuer: str | None = None
    last4: str | None = None
    close_day: int | None = Field(default=None, ge=1, le=31)
    due_day: int | None = Field(default=None, ge=1, le=28)
    autopay: str | None = Field(default=None, pattern="^(min|none|full)$")
    pay_url: str | None = None
    statement_min_cents: int | None = Field(default=None, ge=0)


@router.patch("/{card_id}")
async def patch_card(card_id: str, req: PatchCard):
    async with session(SVC) as s:
        c = await _own(s, card_id)
        for k, v in req.model_dump(exclude_unset=True).items():
            setattr(c, k, v)
        await _changed(s, c.id, "card_edited")
        return await _view(s, c, today())


@router.delete("/{card_id}")
async def archive_card(card_id: str):
    async with session(SVC) as s:
        c = await _own(s, card_id)
        c.archived_at = real_now()
        await _changed(s, c.id, "card_removed")
    return {"ok": True}


class EventReq(BaseModel):
    type: str
    amount_cents: int = Field(ge=0, le=50_000_000)
    effective_date: dt.date | None = None
    source: str = "user"
    ref: str = ""
    note: str = Field(default="", max_length=200)


@router.post("/{card_id}/events")
async def add_event(card_id: str, req: EventReq):
    if req.type not in DEBITS + CREDITS or req.type == "opening":
        raise DomainError("unknown ledger event type", code="bad_event_type")
    async with session(SVC) as s:
        c = await _own(s, card_id)
        if req.ref:  # same reference twice is the same event
            dup = (await s.execute(select(LedgerEvent).where(LedgerEvent.card_id == card_id, LedgerEvent.ref == req.ref, LedgerEvent.type == req.type))).scalars().first()
            if dup:
                return {"event_id": dup.id, "card": await _view(s, c, today()), "duplicate": True}
        on = req.effective_date or today()
        amount = req.amount_cents
        if req.type in CREDITS:
            amount = min(amount, await _balance(s, card_id, on))  # a card never goes below zero here
        e = LedgerEvent(id=new_id(), card_id=card_id, user_id=c.user_id, type=req.type, amount_cents=amount, effective_date=on,
                        source=req.source, ref=req.ref, note=req.note, created_at=real_now())
        s.add(e)
        await _changed(s, card_id, req.type)
        await s.flush()
        return {"event_id": e.id, "card": await _view(s, c, today()), "duplicate": False}


@router.delete("/{card_id}/events/by-ref/{ref}")
async def remove_event(card_id: str, ref: str):
    """Undo: remove the events written under a reference (for example one payday instruction)."""
    async with session(SVC) as s:
        c = await _own(s, card_id)
        rows = (await s.execute(select(LedgerEvent).where(LedgerEvent.card_id == card_id, LedgerEvent.ref == ref))).scalars().all()
        for e in rows:
            await s.delete(e)
        if rows:
            await _changed(s, card_id, "undo")
        await s.flush()
        return {"removed": len(rows), "card": await _view(s, c, today())}


class RateReq(BaseModel):
    apr_bps: int = Field(ge=0, le=7_999)
    effective_date: dt.date | None = None
    source: str = "user"
    ref: str = ""
    note: str = Field(default="", max_length=200)


@router.post("/{card_id}/rate")
async def change_rate(card_id: str, req: RateReq):
    async with session(SVC) as s:
        c = await _own(s, card_id)
        old = c.apr_bps
        c.apr_bps = req.apr_bps
        s.add(LedgerEvent(card_id=card_id, user_id=c.user_id, type="rate_change", apr_bps=req.apr_bps, amount_cents=0,
                          effective_date=req.effective_date or today(), source=req.source, ref=req.ref,
                          note=req.note or f"Rate {old / 100:.2f}% to {req.apr_bps / 100:.2f}%", created_at=real_now()))
        await _changed(s, card_id, "rate_change")
        return {"old_apr_bps": old, "card": await _view(s, c, today())}


class TrueUpReq(BaseModel):
    balance_cents: int = Field(ge=0)
    on: dt.date
    statement_id: str
    apr_bps: int | None = None
    statement_min_cents: int | None = None
    close_day: int | None = Field(default=None, ge=1, le=31)
    due_day: int | None = Field(default=None, ge=1, le=28)


@router.post("/{card_id}/true-up")
async def true_up(card_id: str, req: TrueUpReq):
    """The statement is right: adjust the ledger to its closing balance and take its rate, minimum and dates."""
    async with session(SVC) as s:
        c = await _own(s, card_id)
        dup = (await s.execute(select(LedgerEvent).where(LedgerEvent.card_id == card_id, LedgerEvent.ref == req.statement_id, LedgerEvent.type == "adjustment"))).scalars().first()
        before = await _balance(s, card_id, req.on)
        diff = req.balance_cents - before
        if diff and not dup:
            s.add(LedgerEvent(card_id=card_id, user_id=c.user_id, type="adjustment", amount_cents=diff, effective_date=req.on,
                              source="reconcile", ref=req.statement_id, note="Matched to the statement balance", created_at=real_now()))
        rate_changed = req.apr_bps is not None and req.apr_bps != c.apr_bps
        old_apr = c.apr_bps
        if rate_changed:
            c.apr_bps = req.apr_bps
            s.add(LedgerEvent(card_id=card_id, user_id=c.user_id, type="rate_change", apr_bps=req.apr_bps, effective_date=req.on,
                              source="statement", ref=req.statement_id, note=f"Rate on the statement: {req.apr_bps / 100:.2f}%", created_at=real_now()))
        for k in ("statement_min_cents", "close_day", "due_day"):
            if getattr(req, k) is not None:
                setattr(c, k, getattr(req, k))
        await _changed(s, card_id, "statement")
        await s.flush()
        return {"adjusted_by_cents": 0 if dup else diff, "ledger_balance_before_cents": before, "old_apr_bps": old_apr,
                "rate_changed": rate_changed, "card": await _view(s, c, today())}


@router.get("/{card_id}/ledger")
async def ledger(card_id: str, since: dt.date | None = None, until: dt.date | None = None, limit: int = Query(default=200, le=1000)):
    async with session(SVC) as s:
        c = await _own(s, card_id)
        q = select(LedgerEvent).where(LedgerEvent.card_id == card_id)
        if since:
            q = q.where(LedgerEvent.effective_date >= since)
        if until:
            q = q.where(LedgerEvent.effective_date <= until)
        rows = (await s.execute(q.order_by(LedgerEvent.effective_date, LedgerEvent.created_at).limit(limit))).scalars().all()
        opening = await _balance(s, card_id, since - dt.timedelta(days=1)) if since else 0
        return {"card_id": c.id, "balance_before_cents": opening,
                "events": [{"id": e.id, "type": e.type, "amount_cents": e.amount_cents, "signed_cents": signed(e), "apr_bps": e.apr_bps,
                            "date": e.effective_date.isoformat(), "source": e.source, "ref": e.ref, "note": e.note} for e in rows]}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        n = (await s.execute(select(func.count()).select_from(CardRow).where(CardRow.archived_at.is_(None)))).scalar_one()
    return {"cards": n}
