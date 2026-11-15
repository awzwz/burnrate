"""Arithmetic checks on numbers read from a statement, and the forecast-versus-statement comparison.
The model that reads a statement is never trusted on its own: these checks decide whether to ask the user."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from fractions import Fraction

from burnrate.engine.core import MinRule, daily_rate, min_payment_cents, round_cents, usd


@dataclass(frozen=True)
class StatementFields:
    """Everything is optional: a photo may miss a line. Amounts in cents, rate in basis points."""

    new_balance_cents: int | None = None
    previous_balance_cents: int | None = None
    payments_credits_cents: int | None = None
    purchases_cents: int | None = None
    fees_cents: int | None = None
    interest_cents: int | None = None
    apr_bps: int | None = None
    balance_subject_to_interest_cents: int | None = None
    days_in_cycle: int | None = None
    min_payment_cents: int | None = None
    close_date: dt.date | None = None
    due_date: dt.date | None = None
    late_fee_cents: int | None = None
    interest_ytd_cents: int | None = None


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: not enough numbers to run the check
    detail: str
    fields: tuple[str, ...] = ()  # which inputs to ask the user about when it fails


MIN_INTEREST_CHARGE = 50  # issuers charge at least $0.50 when any interest is due


def statement_checks(f: StatementFields, *, rule: MinRule | None = None, previous_due_day: int | None = None) -> list[Check]:
    out: list[Check] = []

    parts = (f.previous_balance_cents, f.payments_credits_cents, f.purchases_cents, f.fees_cents, f.interest_cents, f.new_balance_cents)
    if all(p is not None for p in parts):
        expect = f.previous_balance_cents - f.payments_credits_cents + f.purchases_cents + f.fees_cents + f.interest_cents
        ok = abs(expect - f.new_balance_cents) <= 1
        out.append(Check("balance_adds_up", ok,
                         f"Previous balance, payments, purchases, fees and interest give {usd(expect)}; the statement says {usd(f.new_balance_cents)}.",
                         ("new_balance_cents", "interest_cents")))
    else:
        out.append(Check("balance_adds_up", None, "Not enough lines to add the statement up."))

    if None not in (f.balance_subject_to_interest_cents, f.apr_bps, f.days_in_cycle, f.interest_cents):
        expect = round_cents(f.balance_subject_to_interest_cents * daily_rate(f.apr_bps) * f.days_in_cycle)
        tolerance = max(2, f.days_in_cycle // 10)
        ok = abs(expect - f.interest_cents) <= tolerance or (f.interest_cents == MIN_INTEREST_CHARGE and expect < MIN_INTEREST_CHARGE)
        out.append(Check("interest_matches_rate", ok,
                         f"{usd(f.balance_subject_to_interest_cents)} at {f.apr_bps / 100:.2f}% for {f.days_in_cycle} days is {usd(expect)}; the statement charged {usd(f.interest_cents)}.",
                         ("apr_bps", "interest_cents")))
    else:
        out.append(Check("interest_matches_rate", None, "Rate, balance subject to interest or cycle length is missing."))

    if f.min_payment_cents is not None and f.new_balance_cents is not None:
        ok = 0 <= f.min_payment_cents <= f.new_balance_cents
        detail = f"Minimum {usd(f.min_payment_cents)} on a balance of {usd(f.new_balance_cents)}."
        if ok and rule is not None and f.interest_cents is not None:
            expect = min_payment_cents(rule, f.new_balance_cents, f.interest_cents, f.late_fee_cents or 0)
            if abs(expect - f.min_payment_cents) > 100:
                detail += f" Our rule for this issuer gives {usd(expect)}; we will use the statement's number."
        out.append(Check("minimum_is_plausible", ok, detail, ("min_payment_cents",)))
    else:
        out.append(Check("minimum_is_plausible", None, "Minimum payment or balance is missing."))

    if f.close_date and f.due_date:
        gap = (f.due_date - f.close_date).days
        ok = 18 <= gap <= 35
        out.append(Check("dates_are_consistent", ok,
                         f"Due date is {gap} days after the closing date; issuers leave at least 21.", ("close_date", "due_date")))
    else:
        out.append(Check("dates_are_consistent", None, "Closing date or due date is missing."))

    if f.due_date and previous_due_day:
        ok = f.due_date.day == previous_due_day or abs(f.due_date.day - previous_due_day) <= 3
        out.append(Check("due_day_unchanged", ok,
                         f"Due on the {f.due_date.day}; last time it was the {previous_due_day}.", ("due_date",)))
    else:
        out.append(Check("due_day_unchanged", None, "No earlier statement to compare the due day with."))
    return out


def needs_confirmation(checks: list[Check]) -> list[str]:
    """Field names to show on the "please confirm these numbers" screen."""
    out: list[str] = []
    for c in checks:
        if c.ok is False:
            out += [x for x in c.fields if x not in out]
    return out


@dataclass(frozen=True)
class Reconciliation:
    predicted_interest_cents: int
    actual_interest_cents: int
    diff_cents: int  # actual minus predicted
    parts: list[tuple[str, int]]  # explained pieces of the difference, in cents
    unexplained_cents: int
    within_tolerance: bool
    summary: str


def reconcile(predicted_interest_cents: int, actual_interest_cents: int, *, apr_bps: int, days: int,
              unplanned_purchases: list[tuple[int, int]] = (), payment_shifts: list[tuple[int, int]] = (),
              rate_change_bps: int = 0, average_balance_cents: int = 0) -> Reconciliation:
    """Explain actual minus predicted.

    unplanned_purchases: (amount in cents, days it was on the card during the cycle)
    payment_shifts:      (amount in cents, days later than planned; negative means earlier)
    rate_change_bps:     actual rate minus the rate the forecast used
    """
    r = daily_rate(apr_bps)
    parts: list[tuple[str, int]] = []
    purchases = round_cents(sum((Fraction(a) * r * d for a, d in unplanned_purchases), Fraction(0)))
    if purchases:
        parts.append(("New purchases on the card", purchases))
    timing = round_cents(sum((Fraction(a) * r * d for a, d in payment_shifts), Fraction(0)))
    if timing:
        parts.append(("Payments that posted on other days than planned", timing))
    if rate_change_bps and average_balance_cents:
        rate = round_cents(Fraction(average_balance_cents) * daily_rate(rate_change_bps) * days)
        if rate:
            parts.append(("A different interest rate than the forecast used", rate))
    diff = actual_interest_cents - predicted_interest_cents
    unexplained = diff - sum(p[1] for p in parts)
    if unexplained:
        parts.append(("Rounding and posting dates", unexplained))
    tolerance = max(5, days // 6)
    within = abs(unexplained) <= tolerance
    if diff == 0:
        summary = "Our forecast matched your statement to the cent."
    elif abs(diff) <= tolerance and len(parts) == 1:
        summary = f"Our forecast was off by {usd(abs(diff))}. That is rounding."
    else:
        biggest = max(parts, key=lambda p: abs(p[1]))
        summary = f"The statement is {usd(abs(diff))} {'above' if diff > 0 else 'below'} our forecast. Main reason: {biggest[0].lower()}."
    return Reconciliation(predicted_interest_cents, actual_interest_cents, diff, parts, unexplained, within, summary)
