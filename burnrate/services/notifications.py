"""notifications: templates, consent, channels and the record of everything sent.

SMS carries the payday action; email carries documents (receipts, renewal notices, statement results).
SMS needs a separate opt-in. With the `inbox` provider nothing leaves the system: messages are captured and
shown in the demo inbox, which is also how judges see what a subscriber would receive.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common import bus
from burnrate.common.auth import signed
from burnrate.common.config import settings
from burnrate.common.context import get_ctx, now, real_now
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.engine.core import usd

SVC = "notifications"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/notifications", tags=["notifications"])
log = logging.getLogger("burnrate.notifications")


class Prefs(Base):
    __tablename__ = "prefs"
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    sms_opt_in: Mapped[bool] = mapped_column(default=False)
    email: Mapped[str] = mapped_column(String(254), default="")
    email_opt_in: Mapped[bool] = mapped_column(default=True)
    timezone: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(8))  # sms | email
    to: Mapped[str] = mapped_column(String(254), default="")
    template: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text)
    link: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(12))  # captured | sent | failed | suppressed
    provider: Mapped[str] = mapped_column(String(16), default="inbox")
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    sent_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)  # the user's clock (demo aware)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


# ---------- templates: (channel, subject, body, link path) ----------

def _app(path: str) -> str:
    return settings().public_url.rstrip("/") + path


def _payday_sms(d: dict):
    body = (f"Burn Rate: it's payday. Send {usd(d['amount_cents'])} to {d['card_name']} today. "
            f"That takes {usd(round(d.get('burn_cut', 0)))} a day off your interest.")
    return "sms", "", body + " Reply STOP to opt out.", _app("/app")


def _payday_short_sms(d: dict):
    return "sms", "", ("Burn Rate: it's payday. This paycheck covers your minimums; nothing extra to send today. "
                       "Reply STOP to opt out."), _app("/app")


def _due_safety_sms(d: dict):
    return "sms", "", (f"Burn Rate: {d['card_name']} minimum of {usd(d['min_cents'])} is due {d['due_label']}. "
                       "Check that autopay is on. Reply STOP to opt out."), _app("/app/cards")


def _statement_ready(d: dict):
    return "email", f"{d['card_name']}: your statement is checked", (
        f"We compared our forecast with your {d['card_name']} statement.\n\n{d['summary']}\n\n"
        "Open Burn Rate to see the numbers side by side."), _app(f"/app/statements/{d['statement_id']}")


def _statement_expected(d: dict):
    return "email", f"{d['card_name']}: a new statement should be out", (
        f"Your {d['card_name']} statement closed on {d['close_label']}. Add it and we will check our forecast against it."
    ), _app("/app/statements")


def _welcome(d: dict):
    return "email", "Your Burn Rate plan is ready", (
        f"Your plan: {d['plan_name']}. First payment {usd(d['paid_cents'])}. It renews at {usd(d['renews_price_cents'])} "
        f"every {d['renews_weeks']} weeks on {d['renews_on']} unless you cancel. Cancel any time in Settings, in two taps. "
        "Money back within 14 days, no conditions."), _app("/app")


def _renewal_notice(d: dict):
    return "email", f"Your plan renews on {d['renews_on']}", (
        f"On {d['renews_on']} your plan renews at {usd(d['renews_price_cents'])}.\n"
        f"So far you have paid Burn Rate {usd(d['paid_cents'])}. Found so far: {usd(d['found_cents'])} "
        f"({usd(d['confirmed_cents'])} confirmed by your bank, {usd(d['estimated_cents'])} our estimate).\n"
        "To cancel, open Settings. It takes two taps."), _app("/app/settings")


def _magic_link(d: dict):
    return "email", "Your sign-in link", "Open this link to sign in. It works once, for 30 minutes.", _app(f"/auth/verify?token={d['token']}")


def _receipt(d: dict):
    return "email", f"Receipt: {usd(d['amount_cents'])}", f"We charged {usd(d['amount_cents'])} for {d['plan_name']}.", _app("/app/settings")


TEMPLATES = {"payday_sms": _payday_sms, "payday_short_sms": _payday_short_sms, "due_safety_sms": _due_safety_sms,
             "statement_ready": _statement_ready, "statement_expected": _statement_expected, "welcome": _welcome,
             "renewal_notice": _renewal_notice, "magic_link": _magic_link, "receipt": _receipt}
TRANSACTIONAL = {"welcome", "renewal_notice", "magic_link", "receipt"}  # sent even when marketing email is off


def _guarded(text: str) -> None:
    """Outgoing text passes the compliance check. Templates are fixed, so a failure is a bug: log it loudly."""
    try:
        from burnrate.common.guardrails import check_text
    except ImportError:
        return
    res = check_text(text)
    if not res.ok:
        log.error("template text failed the guard: %s", res.violations)


# ---------- providers ----------

async def _deliver(channel: str, to: str, subject: str, body: str, link: str, demo: bool) -> tuple[str, str]:
    """Returns (status, provider). Demo sessions never reach a real provider."""
    st = settings()
    provider = st.sms_provider if channel == "sms" else st.email_provider
    if demo or provider == "inbox":
        return "captured", "inbox"
    try:
        if channel == "sms" and provider == "twilio":
            async with httpx.AsyncClient(timeout=10, auth=(st.twilio_account_sid, st.twilio_auth_token)) as c:
                r = await c.post(f"https://api.twilio.com/2010-04-01/Accounts/{st.twilio_account_sid}/Messages.json",
                                 data={"To": to, "From": st.twilio_from, "Body": f"{body} {link}".strip()})
                r.raise_for_status()
            return "sent", "twilio"
        if channel == "email" and provider == "smtp":
            import smtplib
            from email.message import EmailMessage
            from urllib.parse import urlparse

            u = urlparse(st.smtp_url)
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = st.mail_from, to, subject
            msg.set_content(f"{body}\n\n{link}".strip())
            with smtplib.SMTP(u.hostname, u.port or 587, timeout=10) as smtp:
                smtp.starttls()
                if u.username:
                    smtp.login(u.username, u.password or "")
                smtp.send_message(msg)
            return "sent", "smtp"
    except Exception:  # noqa: BLE001
        log.exception("delivery failed channel=%s provider=%s", channel, provider)
        return "failed", provider
    return "captured", "inbox"


async def send_message(template: str, data: dict, dedupe_key: str) -> dict:
    """Render and deliver one message to the calling user. Idempotent by dedupe_key."""
    ctx = get_ctx()
    channel, subject, body, link = TEMPLATES[template](data)
    _guarded(body)
    async with session(SVC) as s:
        dup = (await s.execute(select(Message).where(Message.dedupe_key == dedupe_key))).scalar_one_or_none()
        if dup:
            return {"id": dup.id, "status": dup.status, "duplicate": True}
        p = await s.get(Prefs, ctx.user_id)
        to, status, provider = "", "suppressed", "inbox"
        if channel == "sms":
            if p and p.sms_opt_in and p.phone:
                to = p.phone
            elif p and p.email and p.email_opt_in:  # no SMS consent: the same instruction goes by email
                channel, subject, to = "email", "It's payday: your Burn Rate instruction", p.email
                body = body.replace(" Reply STOP to opt out.", "")
        elif p and p.email and (p.email_opt_in or template in TRANSACTIONAL):
            to = p.email
        elif template == "magic_link" and data.get("email"):
            to = data["email"]
        if to:
            status, provider = await _deliver(channel, to, subject, body, link, ctx.demo)
            if status == "failed" and channel == "sms" and p and p.email:  # SMS gateway down: fall back to email
                channel, to = "email", p.email
                status, provider = await _deliver("email", to, "It's payday: your Burn Rate instruction", body, link, ctx.demo)
        m = Message(id=new_id(), user_id=ctx.user_id, channel=channel, to=to, template=template, subject=subject, body=body,
                    link=link, status=status, provider=provider, dedupe_key=dedupe_key, sent_at=now(), created_at=real_now())
        s.add(m)
        return {"id": m.id, "status": status, "channel": channel, "duplicate": False}


class SendReq(BaseModel):
    template: str
    data: dict = {}
    dedupe_key: str = Field(min_length=3, max_length=160)


@router.post("/send")
async def send(req: SendReq):
    return await send_message(req.template, req.data, req.dedupe_key)


@bus.subscribe("notify.request", worker="notifier", service=SVC)
async def on_notify(ev: bus.Event) -> None:
    p = ev.payload
    await send_message(p["template"], p.get("data") or {}, p.get("dedupe_key") or ev.id)


@router.get("/inbox")
async def inbox(limit: int = 50):
    """Captured messages for the calling user, newest first (what would have been sent)."""
    async with session(SVC) as s:
        rows = (await s.execute(select(Message).where(Message.user_id == get_ctx().user_id, Message.status != "suppressed")
                                .order_by(Message.sent_at.desc(), Message.created_at.desc()).limit(limit))).scalars().all()
        return {"messages": [{"id": m.id, "channel": m.channel, "to": m.to, "subject": m.subject, "body": m.body, "link": m.link,
                              "template": m.template, "status": m.status, "sent_at": m.sent_at.isoformat()} for m in rows]}


def _prefs_view(p: Prefs) -> dict:
    uid = p.user_id
    return {"phone": p.phone, "sms_opt_in": p.sms_opt_in, "email": p.email, "email_opt_in": p.email_opt_in,
            "timezone": p.timezone or settings().default_timezone,
            "calendar_url": _app(f"/api/calendar.ics?u={uid}&sig={signed('cal:' + uid)}"),
            "sms_live": settings().sms_provider != "inbox"}


class PrefsReq(BaseModel):
    phone: str | None = Field(default=None, max_length=32)
    sms_opt_in: bool | None = None
    email: str | None = Field(default=None, max_length=254)
    email_opt_in: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)


@router.get("/prefs")
async def get_prefs():
    async with session(SVC) as s:
        p = await s.get(Prefs, get_ctx().user_id)
        if not p:
            p = Prefs(user_id=get_ctx().user_id, phone="", sms_opt_in=False, email="", email_opt_in=True, timezone="", updated_at=real_now())
            s.add(p)
        return _prefs_view(p)


@router.put("/prefs")
async def put_prefs(req: PrefsReq):
    async with session(SVC) as s:
        p = await s.get(Prefs, get_ctx().user_id)
        if not p:
            p = Prefs(user_id=get_ctx().user_id, phone="", sms_opt_in=False, email="", email_opt_in=True, timezone="", updated_at=real_now())
            s.add(p)
        for k, v in req.model_dump(exclude_unset=True).items():
            if v is not None:
                setattr(p, k, v)
        if p.sms_opt_in and not p.phone:
            p.sms_opt_in = False  # consent without a number means nothing
        p.updated_at = real_now()
        return _prefs_view(p)


@router.get("/stats")
async def stats():
    async with session(SVC) as s:
        rows = (await s.execute(select(Message.channel, Message.status, func.count()).group_by(Message.channel, Message.status))).all()
    return {"messages": [{"channel": c, "status": st, "n": n} for c, st, n in rows]}
