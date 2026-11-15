"""Golden tests for the calculation engine. Expected values are derived independently:
closed-form compounding, the sample statement in Regulation Z, and the worked examples in the product research."""
import datetime as dt
from fractions import Fraction

import pytest

from burnrate.engine.checks import StatementFields, needs_confirmation, reconcile, statement_checks
from burnrate.engine.core import (ISSUER_RULES, Card, Txn, apply_payment, burn_per_day, ceil_dollar, current_min_cents,
                                  last_on_or_before, min_payment_cents, next_on_or_after, round_cents, run_cycle,
                                  saving_from_paying_early, total_burn_per_day, usd)
from burnrate.engine.plan import PaySchedule, payday_instruction, paydays, statement_window
from burnrate.engine.refinance import balance_transfer, consolidation_loan
from burnrate.engine.simulate import amortize, compare, payment_for_term, project, yearly_value_of_rate_cut

D = dt.date


def maya() -> list[Card]:
    return [
        Card("cap", "Capital One Quicksilver", "capital_one", 520_000, 2799, close_day=11, due_day=9, statement_min_cents=17_133),
        Card("chase", "Chase Freedom Unlimited", "chase", 340_000, 2249, close_day=9, due_day=6),
        Card("disc", "Discover it", "discover", 190_000, 2499, close_day=30, due_day=27),
    ]


SCHEDULE = PaySchedule("biweekly", D(2026, 11, 20), 30_000)


# ---------- daily interest ----------

def test_thirty_quiet_days_compound_daily():
    r = run_cycle(500_000, 2400, D(2026, 10, 13), D(2026, 11, 11))
    closed_form = 500_000 * ((1 + Fraction(2400, 3_650_000)) ** 30 - 1)
    assert r.days == 30
    assert r.interest_cents == round_cents(closed_form) == 9_958
    assert r.balance_subject_to_interest_cents == 504_797
    assert r.end_balance_cents == 509_958


def test_without_compounding_it_is_simple_interest():
    assert run_cycle(500_000, 2400, D(2026, 10, 13), D(2026, 11, 11), compound=False).interest_cents == 9_863


@pytest.mark.parametrize("days,expected", [(28, 9_288), (29, 9_623), (30, 9_958), (31, 10_293)])
def test_cycle_length_changes_interest(days, expected):
    assert run_cycle(500_000, 2400, D(2026, 1, 1), D(2026, 1, 1) + dt.timedelta(days=days - 1)).interest_cents == expected


def test_purchase_and_payment_inside_the_cycle():
    r = run_cycle(500_000, 2400, D(2026, 1, 1), D(2026, 1, 30),
                  [Txn(D(2026, 1, 10), "purchase", 20_000), Txn(D(2026, 1, 20), "payment", 40_000)])
    assert (r.interest_cents, r.balance_subject_to_interest_cents) == (9_945, 504_174)
    assert r.end_balance_cents == 500_000 + 20_000 - 40_000 + 9_945


def test_trailing_interest_after_paying_in_full():
    r = run_cycle(500_000, 2400, D(2026, 2, 1), D(2026, 3, 2), [Txn(D(2026, 2, 25), "payment", 500_000)])
    assert 7_900 <= r.interest_cents <= 8_000  # about $79-80 still shows up on the next statement
    assert r.end_balance_cents == r.interest_cents


def test_zero_rate_zero_interest_and_never_negative():
    assert run_cycle(500_000, 0, D(2026, 1, 1), D(2026, 1, 30)).interest_cents == 0
    r = run_cycle(10_000, 2400, D(2026, 1, 1), D(2026, 1, 30), [Txn(D(2026, 1, 2), "credit", 50_000)])
    assert r.interest_cents == round_cents(10_000 * Fraction(2400, 3_650_000))  # one day, then a credit balance earns nothing


def test_paying_earlier_saves_interest():
    late = run_cycle(500_000, 2400, D(2026, 1, 1), D(2026, 1, 30), [Txn(D(2026, 1, 25), "payment", 50_000)]).interest_cents
    early = run_cycle(500_000, 2400, D(2026, 1, 1), D(2026, 1, 30), [Txn(D(2026, 1, 1), "payment", 50_000)]).interest_cents
    assert late - early == 798  # the research figure: $7.98 a cycle for $500 moved 24 days
    assert abs((late - early) - saving_from_paying_early(50_000, 2400, 24)) <= 12


def test_rounding_helpers():
    assert round_cents(Fraction(1, 2)) == 1 and round_cents(Fraction(149, 100)) == 1
    assert ceil_dollar(5_903) == 6_000 and ceil_dollar(6_000) == 6_000
    assert usd(123_456) == "$1,234.56" and usd(24_000, 0) == "$240" and usd(-50) == "-$0.50"


# ---------- minimum payment ----------

def test_chase_minimum_is_one_percent_plus_interest_with_a_floor():
    rule = ISSUER_RULES["chase"]
    assert rule.verified
    assert min_payment_cents(rule, 500_000, 9_958) == 5_000 + 9_958
    assert min_payment_cents(rule, 100_000, 1_500) == 3_500  # floor
    assert min_payment_cents(rule, 2_000, 30) == 2_000  # never more than the balance
    assert min_payment_cents(rule, 0, 0) == 0


def test_discover_minimum_takes_the_greatest_branch_and_rounds_up():
    rule = ISSUER_RULES["discover"]
    assert min_payment_cents(rule, 190_000, 3_942) == 6_000  # $20 + interest = $59.42, rounded up
    assert min_payment_cents(rule, 500_000, 9_958) == 12_000  # $20 + $99.58 = $119.58 -> $120
    assert min_payment_cents(rule, 400_000, 1_000) == 8_000  # 2% of the balance wins


def test_printed_minimum_overrides_the_rule():
    cap = maya()[0]
    assert current_min_cents(cap) == 17_133
    assert current_min_cents(maya()[2]) == 6_000


# ---------- dates ----------

def test_day_of_month_helpers_clamp_short_months():
    assert next_on_or_after(31, D(2026, 2, 1)) == D(2026, 2, 28)
    assert next_on_or_after(9, D(2026, 11, 20)) == D(2026, 12, 9)
    assert last_on_or_before(11, D(2026, 12, 8)) == D(2026, 11, 11)
    assert statement_window(maya()[0], D(2026, 12, 9)) == (D(2026, 11, 11), D(2026, 12, 9))


@pytest.mark.parametrize("freq,anchor,start,expected", [
    ("biweekly", D(2026, 11, 20), D(2026, 11, 15), [D(2026, 11, 20), D(2026, 12, 4), D(2026, 12, 18)]),
    ("biweekly", D(2026, 11, 20), D(2026, 10, 1), [D(2026, 10, 9), D(2026, 10, 23), D(2026, 11, 6)]),
    ("weekly", D(2026, 11, 20), D(2026, 11, 21), [D(2026, 11, 27), D(2026, 12, 4), D(2026, 12, 11)]),
    ("semimonthly", D(2026, 11, 1), D(2026, 11, 10), [D(2026, 11, 15), D(2026, 12, 1), D(2026, 12, 15)]),
    ("monthly", D(2026, 1, 31), D(2026, 2, 1), [D(2026, 2, 28), D(2026, 3, 31), D(2026, 4, 30)]),
])
def test_paydays(freq, anchor, start, expected):
    assert paydays(PaySchedule(freq, anchor, 1), start, 3) == expected


def test_biweekly_has_three_paydays_in_some_months():
    ps = paydays(SCHEDULE, D(2027, 1, 1), 26)
    by_month = {}
    for p in ps:
        by_month[(p.year, p.month)] = by_month.get((p.year, p.month), 0) + 1
    assert sorted(set(by_month.values())) == [2, 3]


# ---------- the demo persona ----------

def test_burn_per_day_for_three_cards():
    cards = maya()
    assert [round(float(burn_per_day(c)), 2) for c in cards] == [398.76, 209.50, 130.08]
    assert round_cents(total_burn_per_day(cards)) == 738  # $7.38 a day


def test_payday_instruction_for_the_persona():
    ins = payday_instruction(maya(), SCHEDULE, D(2026, 11, 20))
    s = ins.send
    assert ins.strategy == "avalanche" and ins.next_payday == D(2026, 12, 4)
    assert (s.card_id, s.amount_cents, s.covers_min, s.min_cents, s.extra_cents) == ("cap", 24_000, True, 17_133, 6_867)
    assert (s.days_early, s.saved_by_date_cents) == (19, 350)
    assert round_cents(ins.burn_before) == 738 and round_cents(ins.burn_after) == 720
    assert {(r.card_id, r.source) for r in ins.reserves} == {
        ("disc", "this_paycheck"), ("chase", "next_paycheck"), ("cap", "covered_by_payment")}
    assert ins.reserved_cents == 6_000 and ins.short_by_cents == 0


def test_next_payday_reserves_the_other_minimum():
    first = payday_instruction(maya(), SCHEDULE, D(2026, 11, 20))
    second = payday_instruction(first.cards_after, SCHEDULE, D(2026, 12, 4))
    assert second.send.card_id == "cap" and second.send.amount_cents == 30_000 - 9_741
    assert ("chase", "this_paycheck") in {(r.card_id, r.source) for r in second.reserves}


def test_every_paycheck_is_fully_assigned_and_never_overspent():
    cards = maya()
    day = D(2026, 11, 20)
    for _ in range(12):
        ins = payday_instruction(cards, SCHEDULE, day)
        assert ins.total_send_cents + ins.reserved_cents <= SCHEDULE.per_payday_cents
        assert all(c.total_cents >= 0 for c in ins.cards_after)
        cards, day = ins.cards_after, ins.next_payday


def test_short_paycheck_keeps_minimums_first():
    ins = payday_instruction(maya(), SCHEDULE, D(2026, 11, 20), budget_cents=4_000)
    assert ins.short_by_cents == 2_000 and ins.sends == [] and ins.strategy == "minimums_only"


def test_a_small_card_is_finished_first_when_one_paycheck_can_do_it():
    cards = maya()
    cards[2] = Card("disc", "Discover it", "discover", 15_000, 2499, close_day=30, due_day=27)
    ins = payday_instruction(cards, SCHEDULE, D(2026, 11, 20))
    assert ins.strategy == "finish_small"
    assert [(s.card_id, s.amount_cents) for s in ins.sends] == [("disc", 15_000), ("cap", 15_000)]
    assert next(c for c in ins.cards_after if c.id == "disc").total_cents == 0


def test_payment_before_the_statement_closes_does_not_count_as_the_minimum():
    card = Card("x", "Card", "chase", 300_000, 2400, close_day=25, due_day=20)
    ins = payday_instruction([card], PaySchedule("monthly", D(2026, 11, 22), 40_000), D(2026, 11, 22))
    assert ins.send.covers_min is False  # the statement due Dec 20 closes on Nov 25
    assert ("x", "this_paycheck") in {(r.card_id, r.source) for r in ins.reserves}
    assert ins.send.amount_cents == 40_000 - ins.reserved_cents


def test_debt_free():
    ins = payday_instruction([Card("x", "Card", "chase", 0, 2400)], SCHEDULE, D(2026, 11, 20))
    assert ins.strategy == "debt_free" and ins.sends == []


def test_apply_payment_takes_the_higher_rate_first():
    c = Card("x", "Card", "chase", 100_000, 2400, promo_balance_cents=200_000, promo_apr_bps=0)
    after = apply_payment(c, 150_000)
    assert (after.balance_cents, after.promo_balance_cents) == (0, 150_000)


# ---------- projections ----------

@pytest.mark.parametrize("balance,apr,payment,months,interest", [
    (100_000, 1200, 10_000, 11, 5_898),
    (500_000, 1800, 20_000, 32, 131_397),
    (1_000_000, 2400, 30_000, 56, 664_421),
])
def test_amortization(balance, apr, payment, months, interest):
    a = amortize(balance, apr, payment)
    assert (a.months, a.interest_cents, a.never) == (months, interest, False)


def test_payment_below_interest_never_pays_off():
    assert amortize(1_000_000, 2400, 15_000).never


def test_regulation_z_sample_statement_thirty_six_month_payment():
    assert round(payment_for_term(178_453, 1499, 36) / 100) == 62  # form G-18: $62 a month


def test_strategy_ordering_holds():
    r = compare(maya(), 60_000)
    assert (r["avalanche"].months, r["snowball"].months) == (22, 23)
    assert r["avalanche"].interest_cents <= r["snowball"].interest_cents <= r["minimums"].interest_cents
    assert r["avalanche"].interest_cents == 267_083
    assert [c for c, _ in r["avalanche"].payoff_order][0] == "cap"
    assert [c for c, _ in r["snowball"].payoff_order][0] == "disc"


def test_a_bigger_budget_never_takes_longer_or_costs_more():
    a, b = project(maya(), 50_000), project(maya(), 70_000)
    assert b.months <= a.months and b.interest_cents < a.interest_cents


def test_one_point_of_rate_on_the_target_card_is_fifty_two_dollars_a_year():
    assert yearly_value_of_rate_cut(520_000, 100) == 5_200
    assert yearly_value_of_rate_cut(1_000_000, 630) == 63_000


# ---------- refinancing ----------

def test_balance_transfer_against_staying():
    t = balance_transfer(500_000, 2400, 30_000, promo_months=18, fee_bps=300)
    assert (t.fee_cents, t.breakeven_months, t.stay_months, t.stay_cost_cents) == (15_000, 1.5, 21, 114_333)
    assert (t.transfer_cost_cents, t.saving_cents, t.clears_in_promo) == (15_000, 99_333, True)
    assert balance_transfer(500_000, 2400, 30_000, promo_months=18, fee_bps=500).saving_cents == 89_333


def test_transfer_that_does_not_clear_in_the_promo_pays_interest_afterwards():
    t = balance_transfer(1_000_000, 2400, 30_000, promo_months=12, fee_bps=300)
    assert not t.clears_in_promo and t.transfer_cost_cents > t.fee_cents


@pytest.mark.parametrize("loan_apr,fee,payment,saving_sign", [(1200, 0, 33_214, 1), (1800, 600, 38_460, 1), (2000, 800, 40_395, -1)])
def test_consolidation_loan(loan_apr, fee, payment, saving_sign):
    r = consolidation_loan(1_000_000, loan_apr, 36, fee, 2400)
    assert r.payment_cents == payment
    assert (r.saving_cents > 0) == (saving_sign > 0)


def test_expensive_loan_loses_about_six_hundred_dollars():
    assert -61_000 < consolidation_loan(1_000_000, 2000, 36, 800, 2400).saving_cents < -59_000


# ---------- statement checks ----------

def good_statement() -> StatementFields:
    return StatementFields(new_balance_cents=520_000, previous_balance_cents=532_067, payments_credits_cents=24_000,
                           purchases_cents=0, fees_cents=0, interest_cents=11_933, apr_bps=2799,
                           balance_subject_to_interest_cents=518_720, days_in_cycle=30, min_payment_cents=17_133,
                           close_date=D(2026, 11, 11), due_date=D(2026, 12, 9))


def test_a_consistent_statement_passes_every_check():
    checks = statement_checks(good_statement(), rule=ISSUER_RULES["capital_one"], previous_due_day=9)
    assert [c.ok for c in checks] == [True] * 5
    assert needs_confirmation(checks) == []


def test_a_misread_rate_is_caught_and_names_the_fields_to_confirm():
    bad = StatementFields(**{**good_statement().__dict__, "apr_bps": 2199})
    checks = statement_checks(bad)
    assert next(c for c in checks if c.name == "interest_matches_rate").ok is False
    assert needs_confirmation(checks) == ["apr_bps", "interest_cents"]


def test_a_misread_balance_is_caught():
    bad = StatementFields(**{**good_statement().__dict__, "new_balance_cents": 502_000})
    assert next(c for c in statement_checks(bad) if c.name == "balance_adds_up").ok is False


def test_missing_lines_skip_checks_instead_of_failing():
    checks = statement_checks(StatementFields(new_balance_cents=100_000, apr_bps=2400))
    assert all(c.ok is None for c in checks)


def test_reconcile_explains_the_difference():
    r = reconcile(11_941, 11_933, apr_bps=2799, days=30)
    assert r.diff_cents == -8 and r.within_tolerance and "rounding" in r.summary.lower() or "Rounding" in str(r.parts)
    r = reconcile(11_933, 12_300, apr_bps=2799, days=30, unplanned_purchases=[(25_000, 19)])
    assert r.parts[0][0] == "New purchases on the card" and r.parts[0][1] == 364
    assert r.diff_cents == 367 and r.unexplained_cents == 3 and r.within_tolerance
    assert reconcile(10_000, 10_000, apr_bps=2400, days=30).summary.startswith("Our forecast matched")
