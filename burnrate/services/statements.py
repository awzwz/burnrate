"""statements: take in a statement, read it, check the numbers, compare with our forecast, pull the ledger to it.

Pipeline (each arrow is an event, so a slow or failing step never blocks the others):
    upload -> statement.uploaded -> extract (language model) -> arithmetic checks
           -> needs_confirmation (the user fixes the highlighted numbers) or statement.confirmed
           -> reconcile: forecast vs statement, explain the difference, post to the ledger, raise new asks
The uploaded file is deleted as soon as its numbers are read. Only the numbers are kept.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Date, LargeBinary, String, Text, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus, clients
from burnrate.common.context import get_ctx, real_now, today
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import DomainError, NotFound
from burnrate.engine.core import last_on_or_before, min_payment_cents, rule_for, usd

SVC = "statements"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/statements", tags=["statements"])
log = logging.getLogger("burnrate.statements")
MAX_FILE = 10 * 1024 * 1024
FIELDS = ("new_balance_cents", "previous_balance_cents", "payments_credits_cents", "purchases_cents", "fees_cents", "interest_cents",
          "apr_bps", "balance_subject_to_interest_cents", "days_in_cycle", "min_payment_cents", "close_date", "due_date",
          "late_fee_cents", "interest_ytd_cents")


class StatementRow(Base):
    __tablename__ = "statements"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    card_id: Mapped[str] = mapped_column(String(32), index=True)
    card_name: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20))  # extracting | needs_confirmation | reconciled | failed
    source: Mapped[str] = mapped_column(String(8))  # upload | manual | demo
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    confirm_fields: Mapped[list] = mapped_column(JSON, default=list)
    reconciliation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    triggers: Mapped[list] = mapped_column(JSON, default=list)
    confirmed: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str] = mapped_column(String(300), default="")
    filename: Mapped[str] = mapped_column(String(200), default="")
    mime: Mapped[str] = mapped_column(String(80), default="")
    blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    close_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


def _clean(fields: dict) -> dict:
    """Keep known fields only; integers for amounts, ISO strings for dates."""
    out: dict = {}
    for k in FIELDS:
        v = fields.get(k)
        if v in (None, ""):
            continue
        if k.endswith("_date"):
            try:
                out[k] = dt.date.fromisoformat(str(v)[:10]).isoformat()
            except ValueError:
                continue
        else:
            try:
                out[k] = round(float(v))
            except (TypeError, ValueError):
                continue
    return out


def _view(r: StatementRow) -> dict:
    return {"id": r.id, "card_id": r.card_id, "card_name": r.card_name, "status": r.status, "source": r.source, "fields": r.fields or {},
            "checks": r.checks or [], "confirm_fields": r.confirm_fields or [], "reconciliation": r.reconciliation,
            "triggers": r.triggers or [], "error": r.error or None, "created_at": r.created_at.isoformat(),
            "close_date": r.close_date.isoformat() if r.close_date else None, "due_date": r.due_date.isoformat() if r.due_date else None}


async def _card(card_id: str) -> dict:
    cards = (await clients.get("cards", "/internal/cards"))["cards"]
    card = next((c for c in cards if c["id"] == card_id), None)
    if not card:
        raise NotFound("card not found", code="card_not_found")
    return card


async def _own(s, statement_id: str) -> StatementRow:
    r = await s.get(StatementRow, statement_id)
    if not r or r.user_id != get_ctx().user_id:
        raise NotFound("statement not found", code="statement_not_found")
    return r


# ---------- intake ----------

class UploadReq(BaseModel):
    card_id: str
    filename: str = Field(default="statement", max_length=200)
    mime: str = Field(default="application/pdf", max_length=80)
    data_b64: str | None = None
    text: str | None = Field(default=None, max_length=60_000)
    source: str = "upload"


@router.post("/upload")
async def upload(req: UploadReq):
    card = await _card(req.card_id)
    blob = None
    if req.data_b64:
        try:
            blob = base64.b64decode(req.data_b64, validate=True)
        except ValueError as e:
            raise DomainError("The file could not be read.", code="bad_file") from e
        if len(blob) > MAX_FILE:
            raise DomainError("Files up to 10 MB are accepted.", code="file_too_large", status=413)
    if not blob and not (req.text or "").strip():
        raise DomainError("Add a file or paste the statement text.", code="empty_statement")
    async with session(SVC) as s:
        r = StatementRow(id=new_id(), user_id=get_ctx().user_id, card_id=card["id"], card_name=card["name"], status="extracting",
                         source=req.source, fields={}, checks=[], confirm_fields=[], reconciliation=None, triggers=[], confirmed=False,
                         error="", filename=req.filename, mime=req.mime, blob=blob, text=req.text, close_date=None, due_date=None,
                         created_at=real_now())
        s.add(r)
        await bus.emit(s, SVC, "statement.uploaded", {"statement_id": r.id, "user_id": r.user_id})
        return _view(r)


class ManualReq(BaseModel):
    card_id: str
    fields: dict


@router.post("/manual")
async def manual(req: ManualReq):
    card = await _card(req.card_id)
    fields = _clean(req.fields)
    if "new_balance_cents" not in fields:
        raise DomainError("Enter at least the new balance.", code="balance_required")
    async with session(SVC) as s:
        r = StatementRow(id=new_id(), user_id=get_ctx().user_id, card_id=card["id"], card_name=card["name"], status="extracting",
                         source="manual", fields=fields, checks=[], confirm_fields=[], reconciliation=None, triggers=[], confirmed=False,
                         error="", filename="", mime="", blob=None, text=None, close_date=None, due_date=None, created_at=real_now())
        s.add(r)
        await s.flush()
        sid = r.id
    return await _check(sid)


@bus.subscribe("statement.uploaded", worker="statement-extractor", service=SVC)
async def on_uploaded(ev: bus.Event) -> None:
    """Read the numbers with the language model, then delete the file."""
    sid = ev.payload["statement_id"]
    async with session(SVC) as s:
        r = await s.get(StatementRow, sid)
        if not r or r.status != "extracting" or r.fields:
            return
        body = {"filename": r.filename, "mime": r.mime, "text": r.text,
                "data_b64": base64.b64encode(r.blob).decode() if r.blob else None}
    try:
        res = await clients.post("llm-gateway", "/internal/llm/extract-statement", json=body, timeout=120, retries=0)
        fields, error = _clean(res.get("fields") or {}), ""
    except DomainError as e:
        log.warning("extraction failed statement=%s code=%s", sid, e.code)
        fields = {}
        error = ("Reading statements is paused for today. Enter the numbers yourself." if e.code == "llm_budget_exceeded"
                 else "We could not read this file. Enter the numbers yourself.")
    async with session(SVC) as s:
        r = await s.get(StatementRow, sid)
        r.fields, r.blob, r.text = fields, None, None  # the document is not kept
        if not fields:
            r.status, r.error = "failed", error or "We could not find the statement numbers in this file. Enter them yourself."
            return
    await _check(sid)


async def _check(statement_id: str) -> dict:
    """Arithmetic checks. Anything that does not add up goes back to the user instead of into the ledger."""
    async with session(SVC) as s:
        r = await s.get(StatementRow, statement_id)
        fields, card_id, confirmed = dict(r.fields or {}), r.card_id, r.confirmed
    card = await _card(card_id)
    res = await clients.post("engine", "/internal/engine/statement-checks", json={
        "fields": fields, "issuer": card["issuer"], "previous_due_day": card["due_day"]})
    async with session(SVC) as s:
        r = await s.get(StatementRow, statement_id)
        r.checks = res["checks"]
        r.close_date = dt.date.fromisoformat(fields["close_date"]) if fields.get("close_date") else None
        r.due_date = dt.date.fromisoformat(fields["due_date"]) if fields.get("due_date") else None
        missing = [] if "new_balance_cents" in fields else ["new_balance_cents"]
        ask = [f for f in res["confirm_fields"] + missing if f not in (r.confirm_fields if confirmed else [])]
        if ask and not confirmed:
            r.status, r.confirm_fields = "needs_confirmation", res["confirm_fields"] + missing
        else:
            r.status, r.confirm_fields = "extracting", [] if not confirmed else r.confirm_fields
            await bus.emit(s, SVC, "statement.confirmed", {"statement_id": r.id, "user_id": r.user_id})
        return _view(r)


class ConfirmReq(BaseModel):
    fields: dict = {}


@router.post("/{statement_id}/confirm")
async def confirm(statement_id: str, req: ConfirmReq):
    """The user looked at the highlighted numbers and corrected or accepted them. Their numbers are final."""
    async with session(SVC) as s:
        r = await _own(s, statement_id)
        if r.status == "reconciled":
            return _view(r)
        r.fields = {**(r.fields or {}), **_clean(req.fields)}
        if "new_balance_cents" not in r.fields:
            raise DomainError("Enter at least the new balance.", code="balance_required")
        r.confirmed, r.error = True, ""
    return await _check(statement_id)


# ---------- reconcile ----------

KIND = {"payment": "payment", "credit": "credit", "purchase": "purchase", "fee": "fee"}


@bus.subscribe("statement.confirmed", worker="reconciler", service=SVC)
async def on_confirmed(ev: bus.Event) -> None:
    await reconcile_statement(ev.payload["statement_id"])


async def reconcile_statement(statement_id: str) -> None:
    async with session(SVC) as s:
        r = await s.get(StatementRow, statement_id)
        if not r or r.status == "reconciled":
            return
        f, card_id = dict(r.fields or {}), r.card_id
    card = await _card(card_id)
    old_apr = card["apr_bps"]
    close = dt.date.fromisoformat(f["close_date"]) if f.get("close_date") else last_on_or_before(card["close_day"], today())
    days = int(f.get("days_in_cycle") or 30)
    first = close - dt.timedelta(days=days - 1)
    led = await clients.get("cards", f"/internal/cards/{card_id}/ledger", params={"since": first.isoformat(), "until": close.isoformat()})
    txns = [{"date": e["date"], "kind": KIND[e["type"]], "amount_cents": e["amount_cents"]}
            for e in led["events"] if e["type"] in KIND and not e["ref"].startswith(statement_id)]
    pred = await clients.post("engine", "/internal/engine/cycle", json={
        "start_balance_cents": led["balance_before_cents"], "apr_bps": old_apr, "first_day": first.isoformat(),
        "last_day": close.isoformat(), "txns": txns})

    actual = f.get("interest_cents")
    unplanned = max(0, int(f.get("purchases_cents") or 0) - pred["purchases_cents"])
    extra_fees = max(0, int(f.get("fees_cents") or 0) - pred["fees_cents"])
    new_apr = int(f["apr_bps"]) if f.get("apr_bps") else old_apr
    # A payment on the statement that the ledger never saw is usually autopay taking the minimum on the due date.
    unseen_paid = max(0, int(f.get("payments_credits_cents") or 0) - pred["payments_cents"])
    shifts: list[list[int]] = []
    if unseen_paid:
        due_in_cycle = last_on_or_before(card["due_day"], close)
        on_card = (close - due_in_cycle).days + 1 if due_in_cycle >= first else days // 2 + 1
        shifts.append([unseen_paid, -on_card])
    rec = None
    if actual is not None:
        rec = await clients.post("engine", "/internal/engine/reconcile", json={
            "predicted_interest_cents": pred["interest_cents"], "actual_interest_cents": actual, "apr_bps": new_apr, "days": days,
            # posted below at close - days // 2, so it sat on the card for days // 2 + 1 days
            "unplanned_purchases": [[unplanned, days // 2 + 1]] if unplanned else [], "payment_shifts": shifts,
            "rate_change_bps": new_apr - old_apr, "average_balance_cents": pred["balance_subject_to_interest_cents"]})

    # Post what the statement shows that the ledger did not have, then set the balance to the bank's number.
    async def post(kind: str, amount: int, on: dt.date, note: str) -> None:
        if amount > 0:
            await clients.post("cards", f"/internal/cards/{card_id}/events", json={
                "type": kind, "amount_cents": amount, "effective_date": on.isoformat(), "source": "statement",
                "ref": f"{statement_id}:{kind}", "note": note})

    await post("purchase", unplanned, close - dt.timedelta(days=days // 2), "Purchases found on the statement")
    await post("fee", extra_fees, close, "Fees on the statement")
    await post("interest", int(actual or 0), close, "Interest charged on the statement")
    if f.get("new_balance_cents") is not None:
        body = {"balance_cents": f["new_balance_cents"], "on": close.isoformat(), "statement_id": statement_id, "close_day": close.day}
        if f.get("apr_bps"):
            body["apr_bps"] = new_apr
        if f.get("min_payment_cents") is not None:
            body["statement_min_cents"] = f["min_payment_cents"]
        if f.get("due_date"):
            body["due_day"] = min(28, dt.date.fromisoformat(f["due_date"]).day)
        await clients.post("cards", f"/internal/cards/{card_id}/true-up", json=body)

    triggers: list[dict] = []
    late = int(f.get("late_fee_cents") or 0)
    if late > 0:
        triggers.append({"type": "late_fee_refund", "title": "Ask for the late fee back",
                         "detail": f"{usd(late)} late fee on this statement.", "value_cents": late})
    if new_apr > old_apr:
        triggers.append({"type": "apr_reduction", "title": "Your rate went up",
                         "detail": f"{old_apr / 100:.2f}% to {new_apr / 100:.2f}% on this statement. A reason to ask for a lower rate.",
                         "value_cents": None})

    async with session(SVC) as s:
        r = await s.get(StatementRow, statement_id)
        r.status, r.reconciliation, r.triggers, r.close_date = "reconciled", rec, triggers, close
        await bus.emit(s, SVC, "statement.reconciled", {"user_id": r.user_id, "card_id": card_id, "statement_id": r.id, "triggers": triggers,
                                                       "close_date": close.isoformat()})
        summary = rec["summary"] if rec else "We updated your balance from the statement."
        await bus.emit(s, SVC, "notify.request", {"template": "statement_ready", "dedupe_key": f"stmt-ready:{r.id}",
                                                  "data": {"card_name": r.card_name, "summary": summary, "statement_id": r.id}})


# ---------- reads ----------

@router.get("")
async def list_statements(card_id: str | None = None):
    async with session(SVC) as s:
        q = select(StatementRow).where(StatementRow.user_id == get_ctx().user_id)
        if card_id:
            q = q.where(StatementRow.card_id == card_id)
        rows = (await s.execute(q.order_by(StatementRow.created_at.desc()).limit(60))).scalars().all()
        return {"statements": [_view(r) for r in rows]}


@router.get("/{statement_id}")
async def get_statement(statement_id: str):
    async with session(SVC) as s:
        return _view(await _own(s, statement_id))


# ---------- demo ----------

def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class DemoReq(BaseModel):
    card_id: str
    surprise_purchase_cents: int = 8_640  # something the plan did not know about, so the comparison has a story
    autopay_paid_minimum: bool = True


@router.post("/demo-generate")
async def demo_generate(req: DemoReq):
    """Write the statement the persona's bank would have sent for the cycle that just closed, as text,
    and feed it through the normal pipeline (read, check, reconcile). Used by the demo clock."""
    card = await _card(req.card_id)
    close = last_on_or_before(card["close_day"], today())
    days = 30
    first = close - dt.timedelta(days=days - 1)
    led = await clients.get("cards", f"/internal/cards/{req.card_id}/ledger", params={"since": first.isoformat(), "until": close.isoformat()})
    txns = [{"date": e["date"], "kind": KIND[e["type"]], "amount_cents": e["amount_cents"]} for e in led["events"] if e["type"] in KIND]
    if req.autopay_paid_minimum and card.get("autopay") == "min":
        # The bank's autopay took last month's minimum on the due date; the app never saw that payment.
        due_in_cycle = last_on_or_before(card["due_day"], close)
        if due_in_cycle >= first and not any(t["kind"] == "payment" and t["amount_cents"] >= (card.get("statement_min_cents") or 0) > 0 for t in txns):
            minimum = card.get("statement_min_cents") or min_payment_cents(rule_for(card["issuer"]), led["balance_before_cents"], 0)
            txns.append({"date": due_in_cycle.isoformat(), "kind": "payment", "amount_cents": minimum})
    if req.surprise_purchase_cents:
        txns.append({"date": (close - dt.timedelta(days=days // 2)).isoformat(), "kind": "purchase", "amount_cents": req.surprise_purchase_cents})
    cyc = await clients.post("engine", "/internal/engine/cycle", json={
        "start_balance_cents": led["balance_before_cents"], "apr_bps": card["apr_bps"], "first_day": first.isoformat(),
        "last_day": close.isoformat(), "txns": txns})
    new_balance = cyc["end_balance_cents"]
    minimum = min_payment_cents(rule_for(card["issuer"]), new_balance, cyc["interest_cents"])
    from burnrate.engine.core import next_on_or_after

    due = next_on_or_after(card["due_day"], close + dt.timedelta(days=18))
    text = "\n".join([
        f"{card['name'].upper()}  Account ending in {card.get('last4') or '0000'}",
        "ACCOUNT SUMMARY",
        f"Previous Balance: {_money(led['balance_before_cents'])}",
        f"Payments and Credits: {_money(cyc['payments_cents'])}",
        f"Purchases: {_money(cyc['purchases_cents'])}",
        f"Fees Charged: {_money(cyc['fees_cents'])}",
        f"Interest Charged: {_money(cyc['interest_cents'])}",
        f"New Balance: {_money(new_balance)}",
        f"Minimum Payment Due: {_money(minimum)}",
        f"Payment Due Date: {due.isoformat()}",
        f"Statement Closing Date: {close.isoformat()}",
        f"Days in Billing Cycle: {days}",
        "INTEREST CHARGE CALCULATION",
        (f"Purchases  Annual Percentage Rate (APR): {card['apr_bps'] / 100:.2f}% (v)  "
         f"Balance Subject to Interest Rate: {_money(cyc['balance_subject_to_interest_cents'])}  "
         f"Interest Charge: {_money(cyc['interest_cents'])}"),
    ])
    return await upload(UploadReq(card_id=req.card_id, filename=f"{card['name']} {close.isoformat()}.txt", mime="text/plain", text=text, source="demo"))


class SeedReq(BaseModel):
    statements: list[dict]


@router.post("/seed")
async def seed(req: SeedReq):
    """Ready-made reconciled statements for the demo persona. No events."""
    async with session(SVC) as s:
        for x in req.statements:
            s.add(StatementRow(id=new_id(), user_id=get_ctx().user_id, card_id=x["card_id"], card_name=x.get("card_name", ""),
                               status="reconciled", source="demo", fields=_clean(x["fields"]), checks=x.get("checks", []), confirm_fields=[],
                               reconciliation=x.get("reconciliation"), triggers=[], confirmed=True, error="", filename="", mime="",
                               blob=None, text=None, close_date=dt.date.fromisoformat(x["fields"]["close_date"]),
                               due_date=dt.date.fromisoformat(x["fields"]["due_date"]), created_at=real_now() - dt.timedelta(days=1)))
    return {"seeded": len(req.statements)}


async def purge_files(older_than_hours: int = 24) -> int:
    """Retention job: no uploaded document survives a day, even if its processing got stuck."""
    cutoff = real_now() - dt.timedelta(hours=older_than_hours)
    async with session(SVC) as s:
        res = await s.execute(update(StatementRow).where(StatementRow.created_at < cutoff, (StatementRow.blob.is_not(None)) | (StatementRow.text.is_not(None)))
                              .values(blob=None, text=None))
        return res.rowcount or 0


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        rows = (await s.execute(select(StatementRow.status, StatementRow.source, func.count()).group_by(StatementRow.status, StatementRow.source))).all()
    return {"statements": [{"status": st, "source": src, "n": n} for st, src, n in rows]}
