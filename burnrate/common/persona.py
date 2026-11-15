"""The sample persona used by "Use sample numbers" in the quiz and by the guest demo.

Aziz, 34, paid every two weeks, three cards, $10,500 of revolving balance. Every date is relative to `d0`,
the payday on which the story starts, so the demo looks the same whenever it is opened.
"""
from __future__ import annotations

import datetime as dt

NAME = "aziz"
PER_PAYDAY_CENTS = 30_000
STARTED_DAYS_AGO = 42  # subscribed three paychecks before d0
PLAN_ID = "w12"
PHONE = "+1 (555) 010-0142"  # 555-01xx numbers are reserved for fiction
EMAIL = "aziz@example.com"


def first_payday_on_or_after(day: dt.date) -> dt.date:
    """Aziz is paid on Fridays."""
    return day + dt.timedelta(days=(4 - day.weekday()) % 7)


def _dom(d: dt.date, cap: int = 28) -> int:
    return min(d.day, cap)


def cards(d0: dt.date) -> list[dict]:
    """Her cards as they stand on d0."""
    def day(offset: int) -> dt.date:
        return d0 + dt.timedelta(days=offset)

    return [
        {"id": "", "name": "Capital One Quicksilver", "issuer": "capital_one", "last4": "4417", "balance_cents": 520_000,
         "apr_bps": 2799, "due_day": _dom(day(19)), "close_day": _dom(day(-9), 31), "statement_min_cents": 17_133, "autopay": "min"},
        {"id": "", "name": "Chase Freedom Unlimited", "issuer": "chase", "last4": "0932", "balance_cents": 340_000,
         "apr_bps": 2249, "due_day": _dom(day(16)), "close_day": _dom(day(-11), 31), "autopay": "min"},
        {"id": "", "name": "Discover it", "issuer": "discover", "last4": "7781", "balance_cents": 190_000,
         "apr_bps": 2499, "due_day": _dom(day(7)), "close_day": _dom(day(-21), 31), "autopay": "min"},
    ]


def schedule(d0: dt.date) -> dict:
    return {"frequency": "biweekly", "anchor": d0.isoformat(), "per_payday_cents": PER_PAYDAY_CENTS, "second_day": 15}


def answers() -> dict:
    return {"card_count": "3", "age_band": "25-34", "carry_months": "over_12", "late_fee_recently": True,
            "late_fee_card": "Chase Freedom Unlimited", "late_fee_cents": 3_900, "autopay": "min"}


def history(d0: dt.date) -> dict:
    """What happened between the day she subscribed and d0: two paydays and one successful rate request."""
    def day(offset: int) -> str:
        return (d0 + dt.timedelta(days=offset)).isoformat()

    return {
        "started_on": day(-STARTED_DAYS_AGO),
        "opening": {"Capital One Quicksilver": 568_000, "Chase Freedom Unlimited": 340_000, "Discover it": 190_000},
        "chase_apr_before_bps": 2449,
        "payments": [{"card": "Capital One Quicksilver", "amount_cents": 24_000, "date": day(-28)},
                     {"card": "Capital One Quicksilver", "amount_cents": 24_000, "date": day(-14)}],
        "rate_cut": {"card": "Chase Freedom Unlimited", "from_bps": 2449, "to_bps": 2249, "date": day(-17)},
    }
