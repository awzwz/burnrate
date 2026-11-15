"""JSON shapes used between services, and their conversion to and from engine types."""
from __future__ import annotations

import datetime as dt
from fractions import Fraction

from pydantic import BaseModel, Field

from burnrate.engine.core import Card, burn_per_day, current_min_cents, round_cents
from burnrate.engine.plan import Instruction, PaySchedule, due_date
from burnrate.engine.simulate import Projection


def rate(x: Fraction) -> float:
    """Cents per day as a float with four decimals (display only; never fed back into the engine)."""
    return round(float(x), 4)


class CardIn(BaseModel):
    id: str = ""
    name: str = Field(min_length=1, max_length=60)
    issuer: str = "other"
    balance_cents: int = Field(ge=0, le=50_000_000)
    apr_bps: int = Field(ge=0, le=7_999)
    close_day: int = Field(default=1, ge=1, le=31)
    due_day: int = Field(default=25, ge=1, le=28)
    statement_min_cents: int | None = Field(default=None, ge=0)
    promo_balance_cents: int = Field(default=0, ge=0)
    promo_apr_bps: int = Field(default=0, ge=0)
    promo_end: dt.date | None = None
    post_promo_apr_bps: int | None = None
    autopay: str = "min"
    last4: str = ""
    pay_url: str = ""

    def to_engine(self) -> Card:
        return Card(**self.model_dump())


class ScheduleIn(BaseModel):
    frequency: str = Field(pattern="^(weekly|biweekly|semimonthly|monthly)$")
    anchor: dt.date
    per_payday_cents: int = Field(ge=0, le=5_000_000)
    second_day: int = Field(default=15, ge=1, le=28)

    def to_engine(self) -> PaySchedule:
        return PaySchedule(**self.model_dump())


def card_view(c: Card, on: dt.date) -> dict:
    return {"id": c.id, "name": c.name, "issuer": c.issuer, "last4": c.last4, "balance_cents": c.total_cents,
            "apr_bps": c.apr_bps, "burn_per_day": rate(burn_per_day(c, on)), "min_payment_cents": current_min_cents(c),
            "due_day": c.due_day, "close_day": c.close_day, "next_due": due_date(c, on).isoformat(),
            "autopay": c.autopay, "pay_url": c.pay_url, "min_rule_verified": c.rule().verified,
            "promo": ({"balance_cents": c.promo_balance_cents, "apr_bps": c.promo_apr_bps,
                       "ends": c.promo_end.isoformat() if c.promo_end else None} if c.promo_balance_cents else None)}


def instruction_view(ins: Instruction, cards: list[Card]) -> dict:
    by_id = {c.id: c for c in cards}
    sends = []
    for s in ins.sends:
        c = by_id[s.card_id]
        sends.append({"card_id": c.id, "card_name": c.name, "issuer": c.issuer, "last4": c.last4, "apr_bps": c.apr_bps,
                      "amount_cents": s.amount_cents, "covers_min": s.covers_min, "min_cents": s.min_cents,
                      "extra_cents": s.extra_cents, "due": s.due.isoformat(), "days_early": s.days_early,
                      "saved_by_date_cents": s.saved_by_date_cents, "burn_cut": rate(s.burn_cut), "pay_url": c.pay_url})
    return {"payday": ins.payday.isoformat(), "next_payday": ins.next_payday.isoformat(), "sends": sends,
            "reserves": [{"card_id": r.card_id, "card_name": by_id[r.card_id].name, "issuer": by_id[r.card_id].issuer,
                          "min_cents": r.min_cents, "due": r.due.isoformat(), "source": r.source} for r in ins.reserves],
            "reserved_cents": ins.reserved_cents, "short_by_cents": ins.short_by_cents, "strategy": ins.strategy,
            "reason": ins.reason, "burn_before": rate(ins.burn_before), "burn_after": rate(ins.burn_after),
            "total_send_cents": ins.total_send_cents}


def projection_view(p: Projection, start: dt.date) -> dict:
    y, m = divmod(start.month - 1 + p.months, 12)
    return {"strategy": p.strategy, "months": p.months, "interest_cents": p.interest_cents, "never": p.never,
            "debt_free_month": None if p.never else f"{start.year + y:04d}-{m + 1:02d}",
            "payoff_order": [{"card_id": cid, "month": mo} for cid, mo in p.payoff_order]}


def cents(x: Fraction) -> int:
    return round_cents(x)
