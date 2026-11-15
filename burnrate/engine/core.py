"""Card interest the way issuers compute it: daily balance, APR / 365, yesterday's interest added to today's balance.

Money is integer cents, rates are basis points (27.99% = 2799). Intermediate math is exact (Fraction);
rounding to cents happens once, where a bank would post an amount.
The method follows the 2026 Chase and Discover cardmember agreements and the Capital One agreement on file
with the CFPB; other issuers are assumed to do the same until a statement proves otherwise (see `verified`).
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

ENGINE_VERSION = "2026.11.1"
DAYS_IN_YEAR = 365


# ---------- rounding and formatting ----------

def round_cents(x: Fraction | int | float | Decimal) -> int:
    """Half-up to a whole cent."""
    f = Fraction(x)
    return int((Decimal(f.numerator) / Decimal(f.denominator)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def ceil_dollar(cents: Fraction | int) -> int:
    whole, rem = divmod(Fraction(cents), 100)
    return int(whole + (1 if rem else 0)) * 100


def usd(cents: int | Fraction, places: int = 2) -> str:
    c = round_cents(cents) if places == 2 else round_cents(Fraction(cents) / 100) * 100
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}${c // 100:,}" + (f".{c % 100:02d}" if places == 2 else "")


def daily_rate(apr_bps: int) -> Fraction:
    return Fraction(apr_bps, 10_000 * DAYS_IN_YEAR)


# ---------- dates ----------

def clamp_day(year: int, month: int, day: int) -> dt.date:
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_on_or_after(day_of_month: int, start: dt.date) -> dt.date:
    """First date with this day-of-month on or after `start`."""
    d = clamp_day(start.year, start.month, day_of_month)
    if d >= start:
        return d
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return clamp_day(y, m, day_of_month)


def last_on_or_before(day_of_month: int, start: dt.date) -> dt.date:
    d = clamp_day(start.year, start.month, day_of_month)
    if d <= start:
        return d
    y, m = (start.year - 1, 12) if start.month == 1 else (start.year, start.month - 1)
    return clamp_day(y, m, day_of_month)


# ---------- minimum payment rules: data, not code ----------

@dataclass(frozen=True)
class MinRule:
    """`pct`: max(floor, pct * balance + interest + late fees), capped at the balance.
    `discover`: max(floor, alt_pct * balance, alt_base + interest + late fees), rounded up to a dollar."""

    kind: str = "pct"
    floor_cents: int = 3500
    pct_bps: int = 100
    alt_pct_bps: int = 200
    alt_base_cents: int = 2000
    verified: bool = False  # True when read from the issuer's own agreement
    source: str = ""


ISSUER_RULES: dict[str, MinRule] = {
    "chase": MinRule("pct", 3500, 100, verified=True, source="Chase cardmember agreement COL00067, 2026"),
    "discover": MinRule("discover", 3500, verified=True, source="Discover cardmember agreement, 30 Jun 2026"),
    "capital_one": MinRule("pct", 2500, 100, source="secondary source; confirm on a statement"),
    "bank_of_america": MinRule("pct", 3500, 100, source="secondary source; confirm on a statement"),
    "wells_fargo": MinRule("pct", 2500, 100, source="secondary source; confirm on a statement"),
    "citi": MinRule("pct", 3500, 100, source="sources conflict; confirm on a statement"),
    "amex": MinRule("pct", 3500, 100, source="sources conflict; confirm on a statement"),
    "other": MinRule("pct", 3500, 100, source="default; confirm on a statement"),
}


def rule_for(issuer: str) -> MinRule:
    return ISSUER_RULES.get(issuer, ISSUER_RULES["other"])


def min_payment_cents(rule: MinRule, new_balance_cents: int, interest_cents: int,
                      late_fees_cents: int = 0, past_due_cents: int = 0) -> int:
    if new_balance_cents <= 0:
        return 0
    bal = Fraction(new_balance_cents)
    if rule.kind == "discover":
        core = max(Fraction(rule.floor_cents), bal * rule.alt_pct_bps / 10_000,
                   Fraction(rule.alt_base_cents + interest_cents + late_fees_cents))
        core = Fraction(ceil_dollar(core))
    else:
        core = max(Fraction(rule.floor_cents), bal * rule.pct_bps / 10_000 + interest_cents + late_fees_cents)
    return min(new_balance_cents, round_cents(core) + past_due_cents)


# ---------- cards ----------

@dataclass(frozen=True)
class Card:
    id: str
    name: str
    issuer: str = "other"
    balance_cents: int = 0  # purchases balance, interest-bearing
    apr_bps: int = 0
    close_day: int = 1  # statement closes on this day of the month
    due_day: int = 25  # payment due on this day of the month
    statement_min_cents: int | None = None  # the minimum printed on the current statement, when known
    promo_balance_cents: int = 0  # promotional balance (for example a 0% transfer)
    promo_apr_bps: int = 0
    promo_end: dt.date | None = None
    post_promo_apr_bps: int | None = None
    autopay: str = "min"  # min | none | full
    last4: str = ""
    pay_url: str = ""

    @property
    def total_cents(self) -> int:
        return self.balance_cents + self.promo_balance_cents

    def rule(self) -> MinRule:
        return rule_for(self.issuer)


def burn_per_day(card: Card, on: dt.date | None = None) -> Fraction:
    """Interest this card accrues today, in cents (not rounded)."""
    promo_rate = card.promo_apr_bps
    if card.promo_end and on and on > card.promo_end:
        promo_rate = card.post_promo_apr_bps if card.post_promo_apr_bps is not None else card.apr_bps
    return card.balance_cents * daily_rate(card.apr_bps) + card.promo_balance_cents * daily_rate(promo_rate)


def total_burn_per_day(cards: list[Card], on: dt.date | None = None) -> Fraction:
    return sum((burn_per_day(c, on) for c in cards), Fraction(0))


def cycle_interest_estimate(card: Card, days: int = 30) -> int:
    """Interest for one statement cycle at today's balance, with daily compounding."""
    r = daily_rate(card.apr_bps)
    return round_cents(card.balance_cents * ((1 + r) ** days - 1)) + round_cents(card.promo_balance_cents * daily_rate(card.promo_apr_bps) * days)


def current_min_cents(card: Card) -> int:
    """The printed minimum when we have it, otherwise the issuer rule applied to today's balance."""
    if card.statement_min_cents is not None:
        return min(card.statement_min_cents, card.total_cents)
    return min_payment_cents(card.rule(), card.total_cents, cycle_interest_estimate(card))


# ---------- one statement cycle, day by day ----------

@dataclass(frozen=True)
class Txn:
    date: dt.date
    kind: str  # payment | credit | purchase | fee
    amount_cents: int


@dataclass(frozen=True)
class CycleResult:
    days: int
    interest_cents: int  # posted at the close of the cycle
    balance_subject_to_interest_cents: int  # average daily balance, as printed on the statement
    end_balance_cents: int  # principal after transactions plus posted interest
    purchases_cents: int = 0
    payments_cents: int = 0
    fees_cents: int = 0
    interest_exact: Fraction = field(default=Fraction(0), compare=False)


def run_cycle(start_balance_cents: int, apr_bps: int, first_day: dt.date, last_day: dt.date,
              txns: list[Txn] | tuple[Txn, ...] = (), *, compound: bool = True) -> CycleResult:
    """Accrue interest from first_day to last_day inclusive.

    daily balance = yesterday's balance + yesterday's interest + today's debits - today's credits, never below zero;
    interest for the day = daily balance * APR / 365; the cycle's interest is posted on the last day.
    """
    if last_day < first_day:
        raise ValueError("cycle ends before it starts")
    r = daily_rate(apr_bps)
    by_day: dict[dt.date, list[Txn]] = {}
    for t in txns:
        by_day.setdefault(t.date, []).append(t)
    principal = Fraction(start_balance_cents)
    accrued = Fraction(0)
    sum_daily = Fraction(0)
    purchases = payments = fees = 0
    day = first_day
    days = 0
    while day <= last_day:
        for t in by_day.get(day, ()):
            if t.kind in ("payment", "credit"):
                principal -= t.amount_cents
                payments += t.amount_cents
            elif t.kind == "fee":
                principal += t.amount_cents
                fees += t.amount_cents
            else:
                principal += t.amount_cents
                purchases += t.amount_cents
        daily_balance = max(Fraction(0), principal + (accrued if compound else 0))
        accrued += daily_balance * r
        sum_daily += daily_balance
        days += 1
        day += dt.timedelta(days=1)
    interest = round_cents(accrued)
    return CycleResult(days=days, interest_cents=interest,
                       balance_subject_to_interest_cents=round_cents(sum_daily / days),
                       end_balance_cents=round_cents(principal) + interest,
                       purchases_cents=purchases, payments_cents=payments, fees_cents=fees, interest_exact=accrued)


def saving_from_paying_early(payment_cents: int, apr_bps: int, days_earlier: int) -> int:
    """Interest avoided by sending a payment `days_earlier` days sooner."""
    if days_earlier <= 0 or payment_cents <= 0:
        return 0
    return round_cents(payment_cents * daily_rate(apr_bps) * days_earlier)


def apply_payment(card: Card, amount_cents: int) -> Card:
    """Issuers put the amount above the minimum on the highest rate first; with two buckets that is the purchase
    balance unless the promo rate is higher. Good enough for planning; the statement trues it up."""
    first, second = ("balance_cents", "promo_balance_cents")
    if card.promo_balance_cents and card.promo_apr_bps > card.apr_bps:
        first, second = second, first
    a = min(amount_cents, getattr(card, first))
    b = min(amount_cents - a, getattr(card, second))
    return replace(card, **{first: getattr(card, first) - a, second: getattr(card, second) - b})
