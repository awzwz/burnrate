"""Refinancing math: a 0% balance transfer and a consolidation loan, each against "change nothing".
The engine ranks by total cost on the user's own payment; it never predicts approval."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from burnrate.engine.core import round_cents
from burnrate.engine.simulate import amortize, payment_for_term


@dataclass(frozen=True)
class TransferResult:
    fee_cents: int
    breakeven_months: float  # months of card interest that equal the fee
    stay_months: int
    stay_cost_cents: int  # interest if nothing changes
    transfer_months: int
    transfer_cost_cents: int  # fee plus any interest after the promo ends
    saving_cents: int  # positive: the transfer is cheaper
    payment_to_clear_in_promo_cents: int
    clears_in_promo: bool


def balance_transfer(balance_cents: int, apr_bps: int, monthly_payment_cents: int, *, promo_months: int = 18,
                     fee_bps: int = 300, post_apr_bps: int | None = None) -> TransferResult:
    fee = round_cents(Fraction(balance_cents * fee_bps, 10_000))
    stay = amortize(balance_cents, apr_bps, monthly_payment_cents)
    moved = balance_cents + fee
    months_at_zero = min(promo_months, -(-moved // monthly_payment_cents)) if monthly_payment_cents > 0 else promo_months
    left = max(0, moved - monthly_payment_cents * promo_months)
    tail = amortize(left, post_apr_bps if post_apr_bps is not None else apr_bps, monthly_payment_cents)
    transfer_cost = fee + tail.interest_cents
    monthly_rate = Fraction(apr_bps, 120_000)
    breakeven = float(Fraction(fee_bps, 10_000) / monthly_rate) if monthly_rate else float("inf")
    return TransferResult(
        fee_cents=fee, breakeven_months=round(breakeven, 1), stay_months=stay.months, stay_cost_cents=stay.interest_cents,
        transfer_months=months_at_zero + tail.months, transfer_cost_cents=transfer_cost,
        saving_cents=stay.interest_cents - transfer_cost,
        payment_to_clear_in_promo_cents=round_cents(Fraction(moved, promo_months)) if promo_months else moved,
        clears_in_promo=left == 0,
    )


@dataclass(frozen=True)
class LoanResult:
    borrowed_cents: int  # what must be borrowed to receive the payoff amount after the origination fee
    payment_cents: int
    loan_cost_cents: int  # total paid minus the amount received
    cards_months: int
    cards_cost_cents: int  # card interest when the same payment goes to the cards instead
    saving_cents: int  # positive: the loan is cheaper


def consolidation_loan(amount_cents: int, loan_apr_bps: int, term_months: int, origination_bps: int,
                       card_apr_bps: int) -> LoanResult:
    borrowed = round_cents(Fraction(amount_cents * 10_000, 10_000 - origination_bps))
    payment = payment_for_term(borrowed, loan_apr_bps, term_months)
    loan_cost = payment * term_months - amount_cents
    cards = amortize(amount_cents, card_apr_bps, payment)
    return LoanResult(borrowed, payment, loan_cost, cards.months, cards.interest_cents, cards.interest_cents - loan_cost)
