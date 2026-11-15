"""Payoff projections. Month-step approximation (interest = balance * APR / 12), used for dates and comparisons.
The daily engine in core.py is what forecasts a statement; this module answers "how long and how much in total"."""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

from burnrate.engine.core import Card, min_payment_cents, round_cents

STRATEGIES = ("avalanche", "snowball", "minimums")


@dataclass(frozen=True)
class Amortization:
    months: int
    interest_cents: int
    never: bool = False  # the payment does not cover the interest


def amortize(balance_cents: int, apr_bps: int, payment_cents: int, max_months: int = 1200) -> Amortization:
    """Fixed payment on one balance, monthly compounding. The last payment is whatever is left."""
    bal = Fraction(balance_cents)
    monthly = Fraction(apr_bps, 120_000)
    total_interest = Fraction(0)
    months = 0
    if balance_cents <= 0:
        return Amortization(0, 0)
    if payment_cents <= bal * monthly:
        return Amortization(max_months, 0, never=True)
    while bal > 0 and months < max_months:
        interest = Fraction(round_cents(bal * monthly))
        total_interest += interest
        bal = bal + interest - min(payment_cents, bal + interest)
        months += 1
    return Amortization(months, round_cents(total_interest), never=bal > 0)


def payment_for_term(balance_cents: int, apr_bps: int, months: int) -> int:
    """Level payment that clears the balance in `months` (the 36-month figure on every statement)."""
    if months <= 0:
        raise ValueError("months must be positive")
    r = Fraction(apr_bps, 120_000)
    if r == 0:
        return round_cents(Fraction(balance_cents, months))
    factor = (1 + r) ** months
    return round_cents(balance_cents * r * factor / (factor - 1))


@dataclass(frozen=True)
class Projection:
    strategy: str
    months: int
    interest_cents: int
    payoff_order: list[tuple[str, int]]  # (card id, month it reached zero)
    never: bool = False
    first_month_minimums_cents: int = 0


def project(cards: list[Card], monthly_budget_cents: int, strategy: str = "avalanche", max_months: int = 720) -> Projection:
    """All cards together. Every month: add interest, pay every minimum, put the rest on one card.
    `minimums` ignores the budget and pays only what each issuer asks for."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    bal = {c.id: Fraction(c.total_cents) for c in cards}
    # Blend the purchase and promo rates by balance; good enough for a projection.
    apr = {c.id: (Fraction(c.balance_cents * c.apr_bps + c.promo_balance_cents * c.promo_apr_bps, c.total_cents)
                  if c.total_cents else Fraction(0)) for c in cards}
    rules = {c.id: c.rule() for c in cards}
    total_interest = Fraction(0)
    order: list[tuple[str, int]] = []
    months = 0
    first_mins = 0
    while any(b > 0 for b in bal.values()) and months < max_months:
        months += 1
        mins: dict[str, int] = {}
        for cid, b in bal.items():
            if b <= 0:
                continue
            interest = Fraction(round_cents(b * apr[cid] / 120_000))
            total_interest += interest
            bal[cid] = b + interest
            mins[cid] = min(round_cents(bal[cid]), min_payment_cents(rules[cid], round_cents(bal[cid]), round_cents(interest)))
        if months == 1:
            first_mins = sum(mins.values())
        budget = sum(mins.values()) if strategy == "minimums" else max(monthly_budget_cents, sum(mins.values()))
        for cid, m in mins.items():
            bal[cid] -= m
            budget -= m
        while budget > 0:
            owing = [cid for cid, b in bal.items() if b > 0]
            if not owing:
                break
            if strategy == "snowball":
                target = min(owing, key=lambda cid: (bal[cid], -apr[cid]))
            else:
                target = max(owing, key=lambda cid: (apr[cid], bal[cid]))
            pay = min(Fraction(budget), bal[target])
            bal[target] -= pay
            budget -= pay
        for cid, b in bal.items():
            if b <= 0 and cid in mins and cid not in {o[0] for o in order}:
                order.append((cid, months))
    never = any(b > 0 for b in bal.values())
    return Projection(strategy, months, round_cents(total_interest), order, never, first_mins)


def compare(cards: list[Card], monthly_budget_cents: int) -> dict[str, Projection]:
    return {s: project(cards, monthly_budget_cents, s) for s in STRATEGIES}


def with_rate_cut(card: Card, points_bps: int) -> Card:
    return replace(card, apr_bps=max(0, card.apr_bps - points_bps))


def yearly_value_of_rate_cut(balance_cents: int, points_bps: int) -> int:
    """Interest avoided in a year by a rate cut, at today's balance. One point on $5,200 is $52 a year."""
    return round_cents(Fraction(balance_cents * points_bps, 10_000))
