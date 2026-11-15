"""billing: plans, checkout, subscriptions, cancellation, pause and refund.

Flat subscription only: the price never depends on what an issuer answered.
Providers: `demo` (a test card shown on screen, no money moves) and `stripe` (Checkout in test or live mode).
Rules built in: renewal terms are part of the consent the user ticks; cancelling takes one call; a refund inside
the guarantee window needs no reason.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import time

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus, clients
from burnrate.common.config import settings
from burnrate.common.context import get_ctx, now, real_now, today, user_tz
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import Conflict, DomainError, NotFound, PaymentRequired
from burnrate.engine.core import usd

SVC = "billing"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/billing", tags=["billing"])

GUARANTEE_DAYS = 14
TEST_CARD = "4242 4242 4242 4242"
PLANS = {
    "w1": {"id": "w1", "name": "1-week plan", "price_cents": 693, "days": 7, "renews_price_cents": 693, "renews_days": 7, "tag": None},
    "w4": {"id": "w4", "name": "4-week plan", "price_cents": 1999, "days": 28, "renews_price_cents": 3999, "renews_days": 28, "tag": None},
    "w12": {"id": "w12", "name": "12-week plan", "price_cents": 3999, "days": 84, "renews_price_cents": 7999, "renews_days": 84, "tag": "Full program"},
}


def plan_view(p: dict) -> dict:
    weeks = p["renews_days"] // 7
    every = "every week" if weeks == 1 else f"every {weeks} weeks"
    return {**p, "per_day_cents": round(p["price_cents"] / p["days"], 2), "then": f"then {usd(p['renews_price_cents'])} {every}"}


def consent_text(p: dict) -> str:
    weeks = p["days"] // 7
    first = "week" if weeks == 1 else f"{weeks} weeks"
    return (f"I understand that after the first {first} for {usd(p['price_cents'])} the plan renews automatically, "
            f"{plan_view(p)['then'].replace('then ', '')}, until I cancel.")


class Checkout(Base):
    __tablename__ = "checkouts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    plan_id: Mapped[str] = mapped_column(String(8))
    email: Mapped[str] = mapped_column(String(254))
    amount_cents: Mapped[int]
    provider: Mapped[str] = mapped_column(String(12))
    provider_ref: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | paid | expired
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    paid_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(8))
    email: Mapped[str] = mapped_column(String(254), default="")
    status: Mapped[str] = mapped_column(String(12))  # active | canceled | paused | past_due | refunded
    provider: Mapped[str] = mapped_column(String(12), default="demo")
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    current_period_end: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    cancel_at_period_end: Mapped[bool] = mapped_column(default=False)
    paused_until: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_reason: Mapped[str] = mapped_column(String(200), default="")


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    amount_cents: Mapped[int]  # negative for a refund
    kind: Mapped[str] = mapped_column(String(10))  # charge | renewal | refund
    ref: Mapped[str] = mapped_column(String(120), default="")
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


# ---------- views ----------

async def _paid_total(s, uid: str) -> int:
    return int((await s.execute(select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(Payment.user_id == uid))).scalar_one())


async def _first_charge(s, uid: str) -> Payment | None:
    return (await s.execute(select(Payment).where(Payment.user_id == uid, Payment.kind == "charge").order_by(Payment.at).limit(1))).scalar_one_or_none()


async def _roll(s, sub: Subscription) -> None:
    """Bring a subscription up to the caller's "now": end it if it was cancelled, renew it otherwise.
    The demo provider renews here (no card is charged); with Stripe, renewals arrive as webhooks."""
    moment = now()
    if sub.status == "paused" and sub.paused_until and moment >= sub.paused_until:
        sub.status, sub.paused_until = "active", None
    if sub.status != "active":
        return
    plan = PLANS[sub.plan_id]
    while moment >= sub.current_period_end:
        if sub.cancel_at_period_end:
            sub.status = "canceled"
            return
        if sub.provider != "demo":
            sub.status = "past_due"  # the provider did not confirm a renewal
            return
        s.add(Payment(id=new_id(), user_id=sub.user_id, amount_cents=plan["renews_price_cents"], kind="renewal",
                      ref=sub.current_period_end.isoformat(), at=sub.current_period_end))
        sub.current_period_end = sub.current_period_end + dt.timedelta(days=plan["renews_days"])


async def _view(s, sub: Subscription) -> dict:
    await _roll(s, sub)
    await s.flush()
    first = await _first_charge(s, sub.user_id)
    refundable = (first.at + dt.timedelta(days=GUARANTEE_DAYS)) if first and sub.status in ("active", "paused") else None
    if refundable and now() > refundable:
        refundable = None
    tz = user_tz()
    return {"status": sub.status, "plan": plan_view(PLANS[sub.plan_id]), "started_at": sub.started_at.isoformat(),
            "current_period_end": sub.current_period_end.astimezone(tz).date().isoformat(),
            "cancel_at_period_end": sub.cancel_at_period_end,
            "paused_until": sub.paused_until.astimezone(tz).date().isoformat() if sub.paused_until else None,
            "renews_price_cents": PLANS[sub.plan_id]["renews_price_cents"], "paid_total_cents": await _paid_total(s, sub.user_id),
            "refundable_until": refundable.astimezone(tz).date().isoformat() if refundable else None, "provider": sub.provider}


def _entitled(sub: Subscription | None) -> bool:
    return bool(sub and sub.status == "active" and now() < sub.current_period_end)


# ---------- plans and checkout ----------

@router.get("/plans")
async def plans():
    st = settings()
    test = st.billing_provider == "demo" or st.stripe_secret_key.startswith("sk_test_")
    return {"plans": [plan_view(p) for p in PLANS.values()], "guarantee_days": GUARANTEE_DAYS, "provider": st.billing_provider,
            "test_mode": test, "test_card": TEST_CARD if test else None}


class CheckoutReq(BaseModel):
    plan_id: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    consent_renewal: bool


def _checkout_view(c: Checkout, url: str | None = None) -> dict:
    p = plans_sync()
    return {"id": c.id, "provider": c.provider, "url": url, "plan": plan_view(PLANS[c.plan_id]), "amount_cents": c.amount_cents,
            "status": c.status, "test_mode": p["test_mode"], "test_card": p["test_card"], "email": c.email,
            "consent_text": consent_text(PLANS[c.plan_id])}


def plans_sync() -> dict:
    st = settings()
    test = st.billing_provider == "demo" or st.stripe_secret_key.startswith("sk_test_")
    return {"test_mode": test, "test_card": TEST_CARD if test else None}


@router.post("/checkout")
async def create_checkout(req: CheckoutReq):
    if req.plan_id not in PLANS:
        raise DomainError("Unknown plan.", code="unknown_plan")
    if not req.consent_renewal:
        raise DomainError("Please confirm that you understand the renewal terms.", code="consent_required")
    st = settings()
    plan = PLANS[req.plan_id]
    provider = "stripe" if st.billing_provider == "stripe" and st.stripe_secret_key else "demo"
    async with session(SVC) as s:
        sub = await s.get(Subscription, get_ctx().user_id)
        if sub:
            await _roll(s, sub)
            if _entitled(sub):
                raise Conflict("You already have an active plan.", code="already_subscribed")
        c = Checkout(id=new_id(), user_id=get_ctx().user_id, plan_id=req.plan_id, email=req.email.strip().lower(),
                     amount_cents=plan["price_cents"], provider=provider, provider_ref="", status="open", created_at=real_now(), paid_at=None)
        s.add(c)
        url = None
        if provider == "stripe":
            ref, url = await _stripe_session(c, plan)
            c.provider_ref = ref
        return _checkout_view(c, url)


@router.get("/checkout/{checkout_id}")
async def get_checkout(checkout_id: str):
    async with session(SVC) as s:
        c = await s.get(Checkout, checkout_id)
        if not c or c.user_id != get_ctx().user_id:
            raise NotFound("checkout not found", code="checkout_not_found")
        return _checkout_view(c)


async def _activate(s, c: Checkout, *, started_at: dt.datetime | None = None) -> None:
    """A checkout was paid: record the payment, start the subscription, announce it. Idempotent."""
    if c.status == "paid":
        return
    started = started_at or now()
    plan = PLANS[c.plan_id]
    c.status, c.paid_at = "paid", started
    s.add(Payment(id=new_id(), user_id=c.user_id, amount_cents=c.amount_cents, kind="charge", ref=c.id, at=started))
    period_end = started + dt.timedelta(days=plan["days"])
    sub = await s.get(Subscription, c.user_id)
    if sub:
        sub.plan_id, sub.email, sub.status, sub.provider = c.plan_id, c.email, "active", c.provider
        sub.started_at, sub.current_period_end, sub.cancel_at_period_end, sub.paused_until = started, period_end, False, None
    else:
        s.add(Subscription(user_id=c.user_id, plan_id=c.plan_id, email=c.email, status="active", provider=c.provider, started_at=started,
                           current_period_end=period_end, cancel_at_period_end=False, paused_until=None, cancel_reason=""))
    await bus.emit(s, SVC, "subscription.activated", {
        "user_id": c.user_id, "plan_id": c.plan_id, "email": c.email, "paid_cents": c.amount_cents, "plan_name": plan["name"],
        "renews_price_cents": plan["renews_price_cents"], "renews_weeks": plan["renews_days"] // 7,
        "renews_on": period_end.astimezone(user_tz()).date().isoformat()}, key=c.user_id)


class PayReq(BaseModel):
    card_number: str
    exp: str
    cvc: str
    name: str = ""


@router.post("/checkout/{checkout_id}/pay")
async def pay(checkout_id: str, req: PayReq):
    """Test provider: accepts only the test card shown on the page. No real card is ever stored or charged here."""
    async with session(SVC) as s:
        c = await s.get(Checkout, checkout_id)
        if not c or c.user_id != get_ctx().user_id:
            raise NotFound("checkout not found", code="checkout_not_found")
        if c.provider != "demo":
            raise Conflict("This checkout is paid on the provider's page.", code="wrong_provider")
        if c.status != "paid":
            digits = re.sub(r"\D", "", req.card_number)
            m = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*", req.exp)
            if digits != re.sub(r"\D", "", TEST_CARD):
                raise PaymentRequired(f"Test mode: only the test card {TEST_CARD} works here.", code="card_declined")
            if not m or not 1 <= int(m.group(1)) <= 12:
                raise DomainError("Enter the expiry date as MM/YY.", code="bad_expiry")
            year = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
            t = today()
            if (year, int(m.group(1))) < (t.year, t.month):
                raise PaymentRequired("This card has expired.", code="card_expired")
            if not re.fullmatch(r"\d{3,4}", req.cvc.strip()):
                raise DomainError("Enter the 3-digit security code.", code="bad_cvc")
            await _activate(s, c)
        return {"ok": True, "checkout_id": c.id}


@router.post("/checkout/{checkout_id}/confirm")
async def confirm(checkout_id: str):
    """The buyer came back from the provider's page: ask the provider whether the session was paid.
    The webhook does the same job; whichever arrives first activates the plan, the other is a no-op."""
    async with session(SVC) as s:
        c = await s.get(Checkout, checkout_id)
        if not c or c.user_id != get_ctx().user_id:
            raise NotFound("checkout not found", code="checkout_not_found")
        if c.status != "paid" and c.provider == "stripe" and c.provider_ref:
            async with httpx.AsyncClient(timeout=15, auth=(settings().stripe_secret_key, "")) as h:
                r = await h.get(f"https://api.stripe.com/v1/checkout/sessions/{c.provider_ref}")
            if r.status_code == 200 and r.json().get("payment_status") == "paid":
                await _activate(s, c, started_at=real_now())
        return {"paid": c.status == "paid", "checkout_id": c.id}


# ---------- subscription ----------

async def _sub(s) -> Subscription:
    sub = await s.get(Subscription, get_ctx().user_id)
    if not sub:
        raise NotFound("No plan yet.", code="no_subscription")
    return sub


@router.get("/subscription")
async def subscription():
    async with session(SVC) as s:
        return await _view(s, await _sub(s))


@router.get("/entitlement")
async def entitlement():
    async with session(SVC) as s:
        sub = await s.get(Subscription, get_ctx().user_id)
        if not sub:
            return {"entitled": False, "subscription": None}
        view = await _view(s, sub)
        return {"entitled": _entitled(sub), "subscription": view}


class CancelReq(BaseModel):
    reason: str = Field(default="", max_length=200)


@router.post("/cancel")
async def cancel(req: CancelReq):
    """Cancel now, in one call. Access stays until the period that is already paid for ends."""
    async with session(SVC) as s:
        sub = await _sub(s)
        if sub.status == "paused":
            sub.status, sub.paused_until = "active", None
        sub.cancel_at_period_end, sub.cancel_reason = True, req.reason
        return await _view(s, sub)


@router.post("/resume")
async def resume():
    async with session(SVC) as s:
        sub = await _sub(s)
        if sub.status == "paused":
            sub.status, sub.paused_until = "active", None
        elif sub.status == "canceled":
            raise Conflict("This plan has ended. Start a new one from the plans page.", code="plan_ended")
        sub.cancel_at_period_end = False
        return await _view(s, sub)


class PauseReq(BaseModel):
    weeks: int = Field(ge=1, le=8)


@router.post("/pause")
async def pause(req: PauseReq):
    """Pause instead of cancelling: nothing is charged, the paid time is kept and continues afterwards."""
    async with session(SVC) as s:
        sub = await _sub(s)
        await _roll(s, sub)
        if sub.status != "active":
            raise Conflict("Only an active plan can be paused.", code="not_active")
        delta = dt.timedelta(weeks=req.weeks)
        sub.status, sub.paused_until = "paused", now() + delta
        sub.current_period_end = sub.current_period_end + delta
        return await _view(s, sub)


@router.post("/refund")
async def refund():
    """Unconditional refund of the first payment inside the guarantee window."""
    async with session(SVC) as s:
        sub = await _sub(s)
        first = await _first_charge(s, sub.user_id)
        if sub.status == "refunded":
            return await _view(s, sub)
        if not first or now() > first.at + dt.timedelta(days=GUARANTEE_DAYS):
            raise Conflict(f"Refunds are available for {GUARANTEE_DAYS} days after the first payment.", code="refund_window_closed")
        if sub.provider == "stripe" and settings().stripe_secret_key:
            await _stripe_refund(first.ref)
        s.add(Payment(id=new_id(), user_id=sub.user_id, amount_cents=-first.amount_cents, kind="refund", ref=first.id, at=now()))
        sub.status, sub.cancel_at_period_end = "refunded", True
        await bus.emit(s, SVC, "subscription.ended", {"user_id": sub.user_id, "reason": "refund"}, key=sub.user_id)
        return await _view(s, sub)


@router.get("/payments")
async def payments():
    async with session(SVC) as s:
        rows = (await s.execute(select(Payment).where(Payment.user_id == get_ctx().user_id).order_by(Payment.at))).scalars().all()
        return {"payments": [{"amount_cents": p.amount_cents, "kind": p.kind, "at": p.at.isoformat()} for p in rows],
                "paid_total_cents": sum(p.amount_cents for p in rows)}


# ---------- events ----------

@bus.subscribe("subscription.activated", worker="billing-events", service=SVC)
async def on_activated(ev: bus.Event) -> None:
    """Receipt with the renewal terms, and a reminder three days before the first renewal."""
    p = ev.payload
    if p.get("email"):
        await clients.put("notifications", "/internal/notifications/prefs", json={"email": p["email"]})
    async with session(SVC) as s:
        await bus.emit(s, SVC, "notify.request", {"template": "welcome", "dedupe_key": f"welcome:{p['user_id']}:{p['renews_on']}", "data": {
            "plan_name": p["plan_name"], "paid_cents": p["paid_cents"], "renews_price_cents": p["renews_price_cents"],
            "renews_weeks": p["renews_weeks"], "renews_on": _nice(p["renews_on"])}})
    due = dt.datetime.fromisoformat(p["renews_on"]).replace(hour=14, tzinfo=dt.UTC) - dt.timedelta(days=3)
    await clients.post("scheduler", "/internal/scheduler/timers", json={
        "kind": "renewal_notice", "due_at": due.isoformat(), "dedupe_key": f"renewal-notice:{p['user_id']}:{p['renews_on']}",
        "payload": {"renews_on": p["renews_on"]}})


def _nice(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return d.strftime("%b ") + f"{d.day}, {d.year}"


@bus.subscribe("renewal.notice.due", worker="billing-events", service=SVC)
async def on_renewal_notice(ev: bus.Event) -> None:
    """Before every renewal: the date, the price, and what the product has found against what it has cost."""
    uid = ev.payload["user_id"]
    async with session(SVC) as s:
        sub = await s.get(Subscription, uid)
        if not sub or sub.status != "active" or sub.cancel_at_period_end:
            return
        paid = await _paid_total(s, uid)
        plan = PLANS[sub.plan_id]
    confirmed = estimated = 0
    try:
        confirmed = sum(i["cents"] for i in (await clients.get("asks", "/internal/asks/meter"))["items"])
        estimated = sum(i["cents"] for i in (await clients.get("plan", "/internal/plan/meter"))["items"])
    except DomainError:
        pass  # the notice goes out even if the meter is unavailable
    async with session(SVC) as s:
        await bus.emit(s, SVC, "notify.request", {"template": "renewal_notice", "dedupe_key": f"renewal-mail:{uid}:{ev.payload['renews_on']}", "data": {
            "renews_on": _nice(ev.payload["renews_on"]), "renews_price_cents": plan["renews_price_cents"], "paid_cents": paid,
            "confirmed_cents": confirmed, "estimated_cents": estimated, "found_cents": confirmed + estimated}})


@bus.subscribe("billing.provider.event", worker="billing-events", service=SVC)
async def on_provider_event(ev: bus.Event) -> None:
    """A verified Stripe webhook. Only the events we act on are handled; the rest are ignored."""
    e = ev.payload
    obj = (e.get("data") or {}).get("object") or {}
    if e.get("type") == "checkout.session.completed" and obj.get("payment_status") == "paid":
        async with session(SVC) as s:
            c = await s.get(Checkout, obj.get("client_reference_id") or "")
            if c:
                await _activate(s, c, started_at=real_now())


# ---------- Stripe (REST, no SDK) ----------

async def _stripe_session(c: Checkout, plan: dict) -> tuple[str, str]:
    st = settings()
    base = st.public_url.rstrip("/")
    data = {"mode": "payment", "client_reference_id": c.id, "customer_email": c.email,
            "success_url": f"{base}/api/billing/checkout/{c.id}/return", "cancel_url": f"{base}/plans",
            "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(plan["price_cents"]),
            "line_items[0][price_data][product_data][name]": f"Burn Rate {plan['name']}",
            "line_items[0][price_data][product_data][description]": plan_view(plan)["then"]}
    async with httpx.AsyncClient(timeout=15, auth=(st.stripe_secret_key, "")) as h:
        r = await h.post("https://api.stripe.com/v1/checkout/sessions", data=data, headers={"Idempotency-Key": c.id})
    if r.status_code >= 400:
        raise DomainError("The payment provider is not available right now.", code="provider_unavailable", status=503)
    j = r.json()
    return j["id"], j["url"]


async def _stripe_refund(checkout_id: str) -> None:
    st = settings()
    async with session(SVC) as s:
        c = await s.get(Checkout, checkout_id)
    if not c or not c.provider_ref:
        return
    async with httpx.AsyncClient(timeout=15, auth=(st.stripe_secret_key, "")) as h:
        sess = (await h.get(f"https://api.stripe.com/v1/checkout/sessions/{c.provider_ref}")).json()
        if sess.get("payment_intent"):
            await h.post("https://api.stripe.com/v1/refunds", data={"payment_intent": sess["payment_intent"]},
                         headers={"Idempotency-Key": f"refund-{c.id}"})


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance_s: int = 300) -> bool:
    """Stripe-Signature: t=<unix>,v1=<hex hmac sha256 of "t.payload">."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = parts["t"], parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(time.time() - int(ts)) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ---------- demo seed and stats ----------

class SeedReq(BaseModel):
    plan_id: str = "w12"
    started_at: dt.datetime
    email: str = ""


@router.post("/seed")
async def seed(req: SeedReq):
    """An already-paying subscriber for the demo persona. No events."""
    uid = get_ctx().user_id
    plan = PLANS[req.plan_id]
    async with session(SVC) as s:
        if await s.get(Subscription, uid):
            return {"seeded": False}
        s.add(Payment(id=new_id(), user_id=uid, amount_cents=plan["price_cents"], kind="charge", ref="demo", at=req.started_at))
        s.add(Subscription(user_id=uid, plan_id=req.plan_id, email=req.email, status="active", provider="demo", started_at=req.started_at,
                           current_period_end=req.started_at + dt.timedelta(days=plan["days"]), cancel_at_period_end=False,
                           paused_until=None, cancel_reason=""))
    return {"seeded": True}


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        subs = (await s.execute(select(Subscription.status, func.count()).group_by(Subscription.status))).all()
        revenue = (await s.execute(select(func.coalesce(func.sum(Payment.amount_cents), 0)))).scalar_one()
        checkouts = (await s.execute(select(Checkout.status, func.count()).group_by(Checkout.status))).all()
    return {"subscriptions": {k: n for k, n in subs}, "checkouts": {k: n for k, n in checkouts}, "revenue_cents": int(revenue)}
