"""Payday plan: one amount, one card, one day.

Rules (see the product spec, section 3.6):
1. Issuer autopay stays at the minimum on every card as a safety net.
2. Each card's minimum is reserved from the last paycheck that arrives on or before its due date.
3. Everything above the reserved minimums goes to one target card.
4. The target is the highest rate, unless a small card can be finished with this paycheck alone.
5. The extra is sent on payday, not on the due date.
A payment made after the statement closed and before the due date also satisfies that statement's minimum.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from fractions import Fraction

from burnrate.engine.core import (Card, apply_payment, clamp_day, current_min_cents, daily_rate, last_on_or_before,
                                  next_on_or_after, saving_from_paying_early, total_burn_per_day)

FREQUENCIES = ("weekly", "biweekly", "semimonthly", "monthly")


@dataclass(frozen=True)
class PaySchedule:
    frequency: str  # weekly | biweekly | semimonthly | monthly
    anchor: dt.date  # any known payday
    per_payday_cents: int  # money available for cards from each paycheck
    second_day: int = 15  # semimonthly: the other day of the month

    def __post_init__(self):
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"unknown pay frequency: {self.frequency}")


def paydays(schedule: PaySchedule, start: dt.date, count: int) -> list[dt.date]:
    """The next `count` paydays on or after `start`."""
    out: list[dt.date] = []
    if schedule.frequency in ("weekly", "biweekly"):
        step = 7 if schedule.frequency == "weekly" else 14
        delta = (start - schedule.anchor).days
        k = -(-delta // step) if delta > 0 else -((-delta) // step)
        d = schedule.anchor + dt.timedelta(days=k * step)
        while len(out) < count:
            out.append(d)
            d += dt.timedelta(days=step)
        return out
    days = sorted({schedule.anchor.day, schedule.second_day}) if schedule.frequency == "semimonthly" else [schedule.anchor.day]
    y, m = start.year, start.month
    while len(out) < count:
        for day in days:
            d = clamp_day(y, m, day)
            if d >= start and len(out) < count:
                out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def paydays_per_year(frequency: str) -> int:
    return {"weekly": 52, "biweekly": 26, "semimonthly": 24, "monthly": 12}[frequency]


@dataclass(frozen=True)
class Reserve:
    card_id: str
    min_cents: int
    due: dt.date
    source: str  # this_paycheck | next_paycheck | covered_by_payment | nothing_due


@dataclass(frozen=True)
class Send:
    card_id: str
    amount_cents: int
    covers_min: bool
    min_cents: int
    extra_cents: int
    due: dt.date
    days_early: int
    saved_by_date_cents: int
    burn_cut: Fraction  # cents per day taken off the burn


@dataclass(frozen=True)
class Instruction:
    payday: dt.date
    next_payday: dt.date
    sends: list[Send]
    reserves: list[Reserve]
    burn_before: Fraction
    burn_after: Fraction
    reserved_cents: int
    short_by_cents: int
    strategy: str  # avalanche | finish_small | minimums_only | debt_free
    reason: str
    cards_after: list[Card] = field(default_factory=list, compare=False)

    @property
    def send(self) -> Send | None:
        return self.sends[0] if self.sends else None

    @property
    def total_send_cents(self) -> int:
        return sum(s.amount_cents for s in self.sends)


def due_date(card: Card, on: dt.date) -> dt.date:
    return next_on_or_after(card.due_day, on)


def statement_window(card: Card, due: dt.date) -> tuple[dt.date, dt.date]:
    """(close date of the statement that is due on `due`, due)."""
    close = last_on_or_before(card.close_day, due - dt.timedelta(days=1))
    if (due - close).days < 18:  # a statement is mailed at least 21 days before the due date
        close = last_on_or_before(card.close_day, close - dt.timedelta(days=1))
    return close, due


def effective_apr_bps(card: Card, on: dt.date) -> int:
    """Rate used to rank cards: the purchase rate, or the rate a promo balance is about to jump to."""
    apr = card.apr_bps if card.balance_cents > 0 else 0
    if card.promo_balance_cents > 0:
        post = card.post_promo_apr_bps if card.post_promo_apr_bps is not None else card.apr_bps
        ends_soon = card.promo_end is not None and (card.promo_end - on).days <= 90
        apr = max(apr, post if ends_soon else card.promo_apr_bps)
    return apr


def _rate_for_payment(card: Card) -> int:
    return card.apr_bps if card.balance_cents > 0 else card.promo_apr_bps


def choose_target(cards: list[Card], pool_cents: int, on: dt.date) -> tuple[Card | None, str, str]:
    owing = [c for c in cards if c.total_cents > 0]
    if not owing:
        return None, "debt_free", "No balance left on any card."
    highest = max(owing, key=lambda c: (effective_apr_bps(c, on), c.total_cents))
    smallest = min(owing, key=lambda c: c.total_cents)
    if smallest.id != highest.id and 0 < smallest.total_cents <= pool_cents:
        return smallest, "finish_small", (f"This paycheck finishes {smallest.name}. That removes one minimum and one due date; "
                                          f"the rest goes to your highest rate.")
    return highest, "avalanche", f"{highest.name} has your highest rate, so every extra dollar works hardest there."


def payday_instruction(cards: list[Card], schedule: PaySchedule, payday: dt.date,
                       budget_cents: int | None = None) -> Instruction:
    """What to do with this paycheck. `budget_cents` overrides the usual amount ("short this payday")."""
    nxt = paydays(schedule, payday + dt.timedelta(days=1), 1)[0]
    budget = schedule.per_payday_cents if budget_cents is None else budget_cents
    burn_before = total_burn_per_day(cards, payday)
    owing = [c for c in cards if c.total_cents > 0]

    def must_cover_now(c: Card) -> bool:
        return due_date(c, payday) <= nxt

    mins = {c.id: current_min_cents(c) for c in owing}
    # First pass: reserve every minimum that falls due before the next paycheck, then pick a target from what is left.
    base_reserved = sum(mins[c.id] for c in owing if must_cover_now(c))
    target, strategy, reason = choose_target(owing, max(0, budget - base_reserved), payday)

    reserves: list[Reserve] = []
    sends: list[Send] = []
    reserved = 0
    short_by = 0
    state = {c.id: c for c in cards}

    if target is None:
        return Instruction(payday, nxt, [], [], burn_before, burn_before, 0, 0, strategy, reason, list(cards))

    for c in owing:
        due = due_date(c, payday)
        if c.id == target.id:
            continue
        if must_cover_now(c):
            reserves.append(Reserve(c.id, mins[c.id], due, "this_paycheck"))
            reserved += mins[c.id]
        else:
            reserves.append(Reserve(c.id, mins[c.id], due, "next_paycheck"))

    t_due = due_date(target, payday)
    close, _ = statement_window(target, t_due)
    in_window = close <= payday <= t_due
    t_min = mins[target.id]
    if not in_window and must_cover_now(target):
        # The statement has not closed yet: today's payment will not count as its minimum, so keep the minimum aside too.
        reserves.append(Reserve(target.id, t_min, t_due, "this_paycheck"))
        reserved += t_min

    pool = budget - reserved
    if pool < 0:
        short_by = -pool
        pool = 0
    if in_window and must_cover_now(target) and pool < t_min:
        short_by += t_min - pool

    remaining = pool
    order = [target] + sorted((c for c in owing if c.id != target.id),
                              key=lambda c: (-effective_apr_bps(c, payday), c.total_cents))
    for i, c in enumerate(order):
        if remaining <= 0:
            break
        amount = min(remaining, c.total_cents)
        if amount <= 0:
            continue
        due = due_date(c, payday)
        c_close, _ = statement_window(c, due)
        counts = c_close <= payday <= due
        c_min = mins[c.id]
        covers = counts and amount >= c_min
        if i > 0 and not covers and must_cover_now(c):
            # A second card only gets overflow; its reserved minimum stays reserved.
            pass
        rate = _rate_for_payment(c)
        days_early = max(0, (due - payday).days)
        sends.append(Send(card_id=c.id, amount_cents=amount, covers_min=covers, min_cents=c_min,
                          extra_cents=max(0, amount - (c_min if covers else 0)), due=due, days_early=days_early,
                          saved_by_date_cents=saving_from_paying_early(amount, rate, days_early),
                          burn_cut=amount * daily_rate(rate)))
        state[c.id] = apply_payment(state[c.id], amount)
        remaining -= amount
        if i == 0 and covers:
            reserves.append(Reserve(c.id, c_min, due, "covered_by_payment"))
        if i == 0 and strategy != "finish_small":
            # One card per payday unless the target is paid off and money is left over.
            if state[c.id].total_cents > 0:
                break

    if not sends and short_by:
        strategy, reason = "minimums_only", "This paycheck covers minimums only. Nothing is late; the extra waits for the next one."
    cards_after = [state[c.id] for c in cards]
    reserves.sort(key=lambda r: r.due)
    return Instruction(payday=payday, next_payday=nxt, sends=sends, reserves=reserves, burn_before=burn_before,
                       burn_after=total_burn_per_day(cards_after, payday), reserved_cents=reserved,
                       short_by_cents=short_by, strategy=strategy, reason=reason, cards_after=cards_after)
