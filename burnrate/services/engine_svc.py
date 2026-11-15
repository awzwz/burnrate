"""engine: stateless calculations over HTTP. No database, no clock of its own: every date comes in the request."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel, Field

from burnrate.engine import checks as chk
from burnrate.engine.core import ENGINE_VERSION, ISSUER_RULES, Txn, burn_per_day, round_cents, rule_for, run_cycle, total_burn_per_day, usd
from burnrate.engine.io import CardIn, ScheduleIn, card_view, instruction_view, projection_view, rate
from burnrate.engine.plan import payday_instruction, paydays, paydays_per_year
from burnrate.engine.refinance import balance_transfer, consolidation_loan
from burnrate.engine.simulate import compare, yearly_value_of_rate_cut

router = APIRouter(prefix="/internal/engine", tags=["engine"])


class CardsReq(BaseModel):
    cards: list[CardIn]
    on: dt.date


@router.post("/burn")
async def burn(req: CardsReq):
    cards = [c.to_engine() for c in req.cards]
    total = total_burn_per_day(cards, req.on)
    return {"engine_version": ENGINE_VERSION, "burn_per_day": rate(total), "per_year_cents": round_cents(total * 365),
            "cards": [card_view(c, req.on) for c in cards]}


class InstructionReq(BaseModel):
    cards: list[CardIn]
    schedule: ScheduleIn
    payday: dt.date
    budget_cents: int | None = Field(default=None, ge=0)


@router.post("/instruction")
async def instruction(req: InstructionReq):
    cards = [c.to_engine() for c in req.cards]
    ins = payday_instruction(cards, req.schedule.to_engine(), req.payday, req.budget_cents)
    return {"engine_version": ENGINE_VERSION, **instruction_view(ins, cards),
            "cards_after": [{"id": c.id, "balance_cents": c.balance_cents, "promo_balance_cents": c.promo_balance_cents} for c in ins.cards_after]}


class PaydaysReq(BaseModel):
    schedule: ScheduleIn
    start: dt.date
    count: int = Field(default=6, ge=1, le=60)


@router.post("/paydays")
async def paydays_ep(req: PaydaysReq):
    return {"paydays": [d.isoformat() for d in paydays(req.schedule.to_engine(), req.start, req.count)]}


class ProjectionReq(BaseModel):
    cards: list[CardIn]
    monthly_budget_cents: int = Field(ge=0)
    start: dt.date


@router.post("/projection")
async def projection(req: ProjectionReq):
    cards = [c.to_engine() for c in req.cards]
    res = compare(cards, req.monthly_budget_cents)
    return {"engine_version": ENGINE_VERSION, "monthly_budget_cents": req.monthly_budget_cents,
            **{k: projection_view(v, req.start) for k, v in res.items()},
            "first_month_minimums_cents": res["avalanche"].first_month_minimums_cents}


class QuizResultReq(BaseModel):
    cards: list[CardIn] = Field(min_length=1, max_length=6)
    schedule: ScheduleIn
    today: dt.date


@router.post("/quiz-result")
async def quiz_result(req: QuizResultReq):
    """Everything the result screen shows, from the user's own numbers. Deterministic: same input, same output."""
    cards = [c.to_engine() for c in req.cards]
    sched = req.schedule.to_engine()
    total = total_burn_per_day(cards, req.today)
    payday = paydays(sched, req.today, 1)[0]
    ins = payday_instruction(cards, sched, payday)
    per_year = paydays_per_year(sched.frequency)
    monthly = round_cents(sched.per_payday_cents * per_year / 12)
    proj = compare(cards, monthly)
    days_between = {"weekly": 7, "biweekly": 14, "semimonthly": 15, "monthly": 30}[sched.frequency]
    ordered = sorted(cards, key=lambda c: -burn_per_day(c, req.today))
    first = None
    if ins.send:
        s = ins.send
        target = next(c for c in cards if c.id == s.card_id)
        first = {"payday": payday.isoformat(), "amount_cents": s.amount_cents, "card_id": target.id, "card_name": target.name,
                 "covers_min": s.covers_min, "min_cents": s.min_cents, "extra_cents": s.extra_cents,
                 "saved_by_date_cents": s.saved_by_date_cents, "days_early": s.days_early,
                 "burn_after": rate(ins.burn_after), "strategy": ins.strategy, "reason": ins.reason,
                 "short_by_cents": ins.short_by_cents}
    top = max(cards, key=lambda c: c.total_cents)
    return {
        "engine_version": ENGINE_VERSION, "today": req.today.isoformat(), "burn_per_day": rate(total),
        "per_paycheck_cents": round_cents(total * days_between), "paycheck_days": days_between,
        "per_month_cents": round_cents(total * 365 / 12), "per_year_cents": round_cents(total * 365),
        "total_balance_cents": sum(c.total_cents for c in cards),
        "cards": [card_view(c, req.today) for c in ordered],
        "first_move": first,
        "payoff": {"monthly_budget_cents": monthly, "plan": projection_view(proj["avalanche"], req.today),
                   "minimums": projection_view(proj["minimums"], req.today)},
        "point_value": {"card_id": top.id, "card_name": top.name, "per_point_per_year_cents": yearly_value_of_rate_cut(top.total_cents, 100)},
        "assumptions": ["Balances and rates are the ones you entered.",
                        "Interest is charged daily at APR ÷ 365 and added to the balance each day.",
                        "No new purchases on these cards.",
                        "Your statement has the final figure; we compare against it every month."],
    }


class CycleReq(BaseModel):
    start_balance_cents: int
    apr_bps: int = Field(ge=0)
    first_day: dt.date
    last_day: dt.date
    txns: list[dict] = []


@router.post("/cycle")
async def cycle(req: CycleReq):
    """Forecast one statement: interest, average daily balance and closing balance."""
    r = run_cycle(req.start_balance_cents, req.apr_bps, req.first_day, req.last_day,
                  [Txn(dt.date.fromisoformat(t["date"]), t["kind"], int(t["amount_cents"])) for t in req.txns])
    return {"engine_version": ENGINE_VERSION, "days": r.days, "interest_cents": r.interest_cents,
            "balance_subject_to_interest_cents": r.balance_subject_to_interest_cents, "end_balance_cents": r.end_balance_cents,
            "purchases_cents": r.purchases_cents, "payments_cents": r.payments_cents, "fees_cents": r.fees_cents}


class ChecksReq(BaseModel):
    fields: dict
    issuer: str = "other"
    previous_due_day: int | None = None


def _fields(d: dict) -> chk.StatementFields:
    known = chk.StatementFields.__dataclass_fields__
    out = {}
    for k, v in d.items():
        if k not in known or v in (None, ""):
            continue
        out[k] = dt.date.fromisoformat(v) if k.endswith("_date") and isinstance(v, str) else v
    return chk.StatementFields(**out)


@router.post("/statement-checks")
async def statement_checks(req: ChecksReq):
    res = chk.statement_checks(_fields(req.fields), rule=rule_for(req.issuer), previous_due_day=req.previous_due_day)
    return {"checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in res],
            "confirm_fields": chk.needs_confirmation(res), "all_ok": all(c.ok is not False for c in res)}


class ReconcileReq(BaseModel):
    predicted_interest_cents: int
    actual_interest_cents: int
    apr_bps: int
    days: int = Field(ge=1, le=62)
    unplanned_purchases: list[tuple[int, int]] = []
    payment_shifts: list[tuple[int, int]] = []
    rate_change_bps: int = 0
    average_balance_cents: int = 0


@router.post("/reconcile")
async def reconcile(req: ReconcileReq):
    r = chk.reconcile(req.predicted_interest_cents, req.actual_interest_cents, apr_bps=req.apr_bps, days=req.days,
                      unplanned_purchases=req.unplanned_purchases, payment_shifts=req.payment_shifts,
                      rate_change_bps=req.rate_change_bps, average_balance_cents=req.average_balance_cents)
    return {"predicted_interest_cents": r.predicted_interest_cents, "actual_interest_cents": r.actual_interest_cents,
            "diff_cents": r.diff_cents, "parts": [{"label": label, "cents": c} for label, c in r.parts],
            "unexplained_cents": r.unexplained_cents, "within_tolerance": r.within_tolerance, "summary": r.summary}


class RefinanceReq(BaseModel):
    balance_cents: int = Field(gt=0)
    apr_bps: int = Field(ge=0)
    monthly_payment_cents: int = Field(gt=0)
    promo_months: int = 18
    fee_bps: int = 300


@router.post("/refinance")
async def refinance(req: RefinanceReq):
    t3 = balance_transfer(req.balance_cents, req.apr_bps, req.monthly_payment_cents, promo_months=req.promo_months, fee_bps=req.fee_bps)
    t5 = balance_transfer(req.balance_cents, req.apr_bps, req.monthly_payment_cents, promo_months=req.promo_months, fee_bps=500)
    loans = []
    for loan_apr, fee in ((1200, 0), (1400, 500), (1800, 600), (2000, 800)):
        r = consolidation_loan(req.balance_cents, loan_apr, 36, fee, req.apr_bps)
        loans.append({"loan_apr_bps": loan_apr, "origination_bps": fee, "payment_cents": r.payment_cents,
                      "loan_cost_cents": r.loan_cost_cents, "cards_cost_cents": r.cards_cost_cents, "saving_cents": r.saving_cents})
    def tv(t):
        return {"fee_cents": t.fee_cents, "breakeven_months": t.breakeven_months, "transfer_cost_cents": t.transfer_cost_cents,
                "transfer_months": t.transfer_months, "saving_cents": t.saving_cents, "clears_in_promo": t.clears_in_promo,
                "payment_to_clear_in_promo_cents": t.payment_to_clear_in_promo_cents}
    return {"engine_version": ENGINE_VERSION,
            "stay": {"months": t3.stay_months, "interest_cents": t3.stay_cost_cents},
            "transfer": {"promo_months": req.promo_months, "fee_3": tv(t3), "fee_5": tv(t5)},
            "loans": loans,
            "note": f"Staying costs {usd(t3.stay_cost_cents)} in interest at this payment. We do not predict approval for any offer."}


@router.get("/rules")
async def rules():
    return {"rules": {k: {"kind": r.kind, "floor_cents": r.floor_cents, "pct_bps": r.pct_bps, "verified": r.verified, "source": r.source}
                      for k, r in ISSUER_RULES.items()}}
