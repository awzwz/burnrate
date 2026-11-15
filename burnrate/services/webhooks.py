"""webhooks: the only door for calls from payment and SMS providers.
Verify the signature, write the event to the outbox, answer 200 at once. Processing happens in a worker."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from burnrate.common import bus
from burnrate.common.config import settings
from burnrate.common.db import make_base, session
from burnrate.common.errors import Forbidden, Unavailable
from burnrate.services.billing import verify_stripe_signature

SVC = "webhooks"
Base = make_base(SVC)
public = APIRouter(prefix="/hooks", tags=["webhooks"])


@public.post("/stripe")
async def stripe(request: Request):
    st = settings()
    if not st.stripe_webhook_secret:
        raise Unavailable("Stripe webhooks are not configured.", code="webhook_not_configured")
    payload = await request.body()
    if not verify_stripe_signature(payload, request.headers.get("stripe-signature", ""), st.stripe_webhook_secret):
        raise Forbidden("bad signature", code="bad_signature")
    event = json.loads(payload)
    async with session(SVC) as s:
        await bus.emit(s, SVC, "billing.provider.event", {"id": event.get("id"), "type": event.get("type"), "data": event.get("data")},
                       key=str(event.get("id") or ""))
    return {"received": True}


@public.post("/sms")
async def sms(request: Request):
    """Inbound SMS from the gateway (STOP, HELP). Recorded as an event; the notifications service applies opt-outs."""
    form = dict(await request.form())
    async with session(SVC) as s:
        await bus.emit(s, SVC, "notify.inbound", {"from": str(form.get("From", ""))[:32], "body": str(form.get("Body", ""))[:160]})
    return {"received": True}
