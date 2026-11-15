"""edge-api: the only public API. Reads the session cookie, applies rate limits and entitlement,
asks the internal services and composes the answer a screen needs. No business rules live here.
The contract is docs/API.md."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import time

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from burnrate.common import bus, clients, ratelimit
from burnrate.common.auth import COOKIE, cookie_params, make_session_token, read_session_token, signed
from burnrate.common.config import settings
from burnrate.common.context import Ctx, get_ctx, real_now, set_ctx, today
from burnrate.common.errors import DomainError, Forbidden, NotFound, PaymentRequired, Unauthorized
from burnrate.engine.core import ENGINE_VERSION
from burnrate.engine.io import CardIn, ScheduleIn

public = APIRouter(prefix="/api", tags=["public"])
_cfg_cache: dict = {"at": 0.0, "value": None}


# ---------- session ----------

def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _set_cookie(response: Response, ctx: Ctx) -> None:
    response.set_cookie(COOKIE, make_session_token(ctx.user_id, demo=ctx.demo, off=ctx.clock_offset_s, tz=ctx.tz), **cookie_params())


def _ctx_from_cookie(request: Request) -> Ctx | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        c = read_session_token(token)
    except Unauthorized:
        return None
    return Ctx(user_id=c["sub"], demo=bool(c.get("demo")), clock_offset_s=int(c.get("off") or 0), tz=c.get("tz") or "",
               request_id=get_ctx().request_id)


async def current(request: Request) -> Ctx:
    """Every public endpoint except the session bootstrap needs a valid cookie."""
    ctx = _ctx_from_cookie(request)
    if not ctx:
        raise Unauthorized("Start a session first.", code="no_session")
    set_ctx(ctx)
    await ratelimit.check(f"u:{ctx.user_id}", 600, 60)
    return ctx


async def entitled(ctx: Ctx = Depends(current)) -> Ctx:
    """Product endpoints: an active plan, or a demo session."""
    if ctx.demo:
        return ctx
    ent = await clients.get("billing", "/internal/billing/entitlement")
    if not ent["entitled"]:
        raise PaymentRequired("An active plan is needed for this.")
    return ctx


async def _settle() -> None:
    """Local mode runs every handler in this process: finish them before answering, so the next read is current.
    In the cluster the workers do this within a second or two and the client polls."""
    if settings().is_local:
        await bus.drain()


async def _config() -> dict:
    if time.time() - _cfg_cache["at"] > 30 or _cfg_cache["value"] is None:
        st = settings()
        plans = await clients.get("billing", "/internal/billing/plans")
        llm = {"provider": "unknown", "live": False}
        try:
            llm = await clients.get("llm-gateway", "/internal/llm/status")
        except DomainError:
            pass
        _cfg_cache.update(at=time.time(), value={
            "billing_provider": plans["provider"], "test_mode": plans["test_mode"], "test_card": plans["test_card"],
            "llm_provider": llm.get("provider"), "llm_live": bool(llm.get("live")), "sms_live": st.sms_provider != "inbox",
            "demo_enabled": st.demo_enabled})
    return _cfg_cache["value"]


async def _me() -> dict:
    ctx = get_ctx()
    user = await clients.get("identity", f"/internal/identity/users/{ctx.user_id}")
    ent = await clients.get("billing", "/internal/billing/entitlement")
    demo = None
    if ctx.demo:
        d = await clients.get("demo", "/internal/demo/session")
        tz_today = real_now().astimezone(__import__("zoneinfo").ZoneInfo(ctx.tz or settings().default_timezone)).date()
        demo = {"persona": d["persona"], "started_on": d["d0"], "today": today().isoformat(), "real_today": tz_today.isoformat()}
    return {"user": {"id": user["id"], "anonymous": user["anonymous"], "email": user["email"], "demo": user["demo"]},
            "subscription": ent["subscription"], "entitled": bool(ent["entitled"] or ctx.demo), "today": today().isoformat(),
            "demo": demo, "config": await _config()}


@public.post("/session")
async def start_session(request: Request, response: Response):
    ctx = _ctx_from_cookie(request)
    if ctx:
        set_ctx(ctx)
        try:
            return await _me()
        except NotFound:
            pass  # the account behind the cookie is gone: start fresh
    await ratelimit.check(f"session:{_ip(request)}", 60, 60)
    tz = request.headers.get("x-timezone", "")[:64]
    user = await clients.post("identity", "/internal/identity/users", json={"demo": False, "tz": tz})
    ctx = Ctx(user_id=user["id"], tz=tz, request_id=get_ctx().request_id)
    set_ctx(ctx)
    _set_cookie(response, ctx)
    return await _me()


@public.get("/me")
async def me(ctx: Ctx = Depends(current)):
    return await _me()


class EmailReq(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)


@public.post("/auth/link")
async def auth_link(req: EmailReq, request: Request, ctx: Ctx = Depends(current)):
    """Always answers the same way, whether or not the email has an account."""
    await ratelimit.check(f"link:{_ip(request)}", 10, 600, "Too many sign-in links. Try again in ten minutes.")
    r = await clients.post("identity", "/internal/identity/link", json={"email": req.email})
    out: dict = {"sent": True}
    if r["token"]:
        set_ctx(Ctx(user_id=r["user_id"], tz=ctx.tz, request_id=ctx.request_id))
        await clients.post("notifications", "/internal/notifications/send", json={
            "template": "magic_link", "data": {"token": r["token"], "email": req.email}, "dedupe_key": f"link:{r['token'][:24]}"})
        set_ctx(ctx)
        if settings().env != "prod":
            out["dev_link"] = f"/auth/verify?token={r['token']}"  # no mail provider in development
    return out


class VerifyReq(BaseModel):
    token: str


@public.post("/auth/verify")
async def auth_verify(req: VerifyReq, request: Request, response: Response):
    old = _ctx_from_cookie(request)
    user = await clients.post("identity", "/internal/identity/verify", json={"token": req.token})
    ctx = Ctx(user_id=user["id"], tz=user.get("tz") or "", request_id=get_ctx().request_id)
    set_ctx(ctx)
    if old and old.user_id != user["id"] and not old.demo:
        await clients.post("funnel", "/internal/funnel/adopt", json={"from_user_id": old.user_id})
    _set_cookie(response, ctx)
    return await _me()


@public.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {}


# ---------- demo ----------

class DemoStart(BaseModel):
    persona: str = "aziz"


@public.post("/demo/start")
async def demo_start(req: DemoStart, request: Request, response: Response):
    await ratelimit.check(f"demo:{_ip(request)}", 20, 3600, "Too many demo sessions from this address. Try again later.")
    d = await clients.post("demo", "/internal/demo/start", json={"persona": req.persona, "tz": request.headers.get("x-timezone", "")[:64]})
    ctx = Ctx(user_id=d["user"]["id"], demo=True, clock_offset_s=d["clock_offset_s"], tz=d["tz"], request_id=get_ctx().request_id)
    set_ctx(ctx)
    await _settle()
    _set_cookie(response, ctx)
    return await _me()


class DemoClock(BaseModel):
    action: str = Field(pattern="^(next_payday|next_statement|plus_day|reset)$")


@public.post("/demo/clock")
async def demo_clock(req: DemoClock, request: Request, response: Response, ctx: Ctx = Depends(current)):
    if not ctx.demo:
        raise Forbidden("Only demo sessions have a clock.", code="not_a_demo")
    if req.action == "reset":
        return {"me": await demo_start(DemoStart(), request, response), "jumped_to": today().isoformat(), "note": "Started over."}
    d = await clients.post("demo", "/internal/demo/clock", json={"action": req.action}, timeout=60)
    ctx = Ctx(user_id=ctx.user_id, demo=True, clock_offset_s=d["clock_offset_s"], tz=ctx.tz, request_id=ctx.request_id)
    set_ctx(ctx)
    _set_cookie(response, ctx)
    return {"me": await _me(), "jumped_to": d["jumped_to"], "note": d["note"], "statement_id": d.get("statement_id")}


@public.get("/inbox")
async def inbox(ctx: Ctx = Depends(current)):
    """What the product would have sent. Demo sessions and non-production environments only."""
    if not ctx.demo and settings().env == "prod" and settings().sms_provider != "inbox":
        raise Forbidden("The inbox exists only in demo sessions.", code="demo_only")
    return await clients.get("notifications", "/internal/notifications/inbox")


# ---------- quiz ----------

@public.get("/quiz")
async def quiz(ctx: Ctx = Depends(current)):
    return await clients.get("funnel", "/internal/funnel/quiz")


class AnswersReq(BaseModel):
    answers: dict


@public.put("/quiz/answers")
async def quiz_answers(req: AnswersReq, ctx: Ctx = Depends(current)):
    return await clients.put("funnel", "/internal/funnel/quiz/answers", json=req.model_dump())


class QuizCards(BaseModel):
    cards: list[CardIn] = Field(min_length=1, max_length=6)


@public.put("/quiz/cards")
async def quiz_cards(req: QuizCards, ctx: Ctx = Depends(current)):
    return await clients.put("funnel", "/internal/funnel/quiz/cards", json=req.model_dump(mode="json"))


@public.put("/quiz/schedule")
async def quiz_schedule(req: ScheduleIn, ctx: Ctx = Depends(current)):
    return await clients.put("funnel", "/internal/funnel/quiz/schedule", json=req.model_dump(mode="json"))


@public.post("/quiz/sample")
async def quiz_sample(ctx: Ctx = Depends(current)):
    return await clients.post("funnel", "/internal/funnel/quiz/sample")


class BurnReq(BaseModel):
    balance_cents: int = Field(ge=0, le=50_000_000)
    apr_bps: int = Field(ge=0, le=7_999)


@public.post("/quiz/card-burn")
async def quiz_card_burn(req: BurnReq, ctx: Ctx = Depends(current)):
    return await clients.post("funnel", "/internal/funnel/quiz/card-burn", json=req.model_dump())


@public.post("/quiz/result")
async def quiz_result(ctx: Ctx = Depends(current)):
    await ratelimit.check(f"result:{ctx.user_id}", 60, 3600)
    return await clients.post("funnel", "/internal/funnel/quiz/result")


@public.get("/quiz/result")
async def quiz_result_get(ctx: Ctx = Depends(current)):
    return await clients.get("funnel", "/internal/funnel/quiz/result")


# ---------- billing ----------

@public.get("/billing/plans")
async def plans():
    return await clients.get("billing", "/internal/billing/plans")


class CheckoutReq(BaseModel):
    plan_id: str
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    consent_renewal: bool


@public.post("/billing/checkout")
async def checkout(req: CheckoutReq, ctx: Ctx = Depends(current)):
    if ctx.demo:
        raise Forbidden("The demo account already has a plan. Leave the demo to buy one.", code="demo_account")
    c = await clients.post("billing", "/internal/billing/checkout", json=req.model_dump())
    # Keep the exact words the person agreed to.
    await clients.post("identity", f"/internal/identity/users/{ctx.user_id}/consents", json={"kind": "auto_renewal", "text": c["consent_text"]})
    await clients.post("identity", f"/internal/identity/users/{ctx.user_id}/email", json={"email": req.email})
    return c


async def _wait_for_plan(seconds: float = 8.0) -> None:
    """After a payment the plan is set up by events. Wait briefly so the first screen is ready."""
    await _settle()
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            await clients.get("plan", "/internal/plan/schedule")
            return
        except DomainError:
            await asyncio.sleep(0.4)


class PayReq(BaseModel):
    card_number: str
    exp: str
    cvc: str
    name: str = ""


@public.post("/billing/checkout/{checkout_id}/pay")
async def pay(checkout_id: str, req: PayReq, ctx: Ctx = Depends(current)):
    await ratelimit.check(f"pay:{ctx.user_id}", 20, 600)
    await clients.post("billing", f"/internal/billing/checkout/{checkout_id}/pay", json=req.model_dump())
    await _wait_for_plan()
    return await _me()


@public.get("/billing/checkout/{checkout_id}")
async def checkout_get(checkout_id: str, ctx: Ctx = Depends(current)):
    return await clients.get("billing", f"/internal/billing/checkout/{checkout_id}")


@public.get("/billing/checkout/{checkout_id}/return")
async def checkout_return(checkout_id: str, ctx: Ctx = Depends(current)):
    """The payment provider sends the buyer back here. Confirm with the provider, then open the product."""
    r = await clients.post("billing", f"/internal/billing/checkout/{checkout_id}/confirm")
    if r["paid"]:
        await _wait_for_plan()
        return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/plans?payment=pending", status_code=303)


@public.get("/billing/subscription")
async def subscription(ctx: Ctx = Depends(current)):
    return await clients.get("billing", "/internal/billing/subscription")


class CancelReq(BaseModel):
    reason: str = Field(default="", max_length=200)


@public.post("/billing/cancel")
async def cancel(req: CancelReq, ctx: Ctx = Depends(current)):
    return await clients.post("billing", "/internal/billing/cancel", json=req.model_dump())


@public.post("/billing/resume")
async def resume(ctx: Ctx = Depends(current)):
    return await clients.post("billing", "/internal/billing/resume")


class PauseReq(BaseModel):
    weeks: int = Field(ge=1, le=8)


@public.post("/billing/pause")
async def pause(req: PauseReq, ctx: Ctx = Depends(current)):
    return await clients.post("billing", "/internal/billing/pause", json=req.model_dump())


@public.post("/billing/refund")
async def refund(ctx: Ctx = Depends(current)):
    return await clients.post("billing", "/internal/billing/refund")


# ---------- product: today ----------

async def _card_views() -> list[dict]:
    cards = (await clients.get("cards", "/internal/cards"))["cards"]
    if not cards:
        return []
    keys = ("id", "name", "issuer", "balance_cents", "apr_bps", "close_day", "due_day", "statement_min_cents", "autopay", "last4", "pay_url")
    eng = await clients.post("engine", "/internal/engine/burn", json={"on": today().isoformat(), "cards": [
        {k: c[k] for k in keys if c.get(k) is not None} for c in cards]})
    by_id = {c["id"]: c for c in eng["cards"]}
    return [{**by_id[c["id"]], "start_balance_cents": c["start_balance_cents"], "progress": c["progress"], "paid_off": c["paid_off"]} for c in cards]


async def _meter() -> dict:
    paid = (await clients.get("billing", "/internal/billing/payments"))["paid_total_cents"]
    items: list[dict] = []
    for svc, path in (("asks", "/internal/asks/meter"), ("plan", "/internal/plan/meter")):
        try:
            items += (await clients.get(svc, path))["items"]
        except DomainError:
            pass
    confirmed = sum(i["cents"] for i in items if i["kind"] == "confirmed")
    estimated = sum(i["cents"] for i in items if i["kind"] == "estimated")
    return {"paid_cents": paid, "confirmed_cents": confirmed, "estimated_cents": estimated, "found_cents": confirmed + estimated,
            "items": sorted(items, key=lambda i: i.get("date") or "")}


async def _asks() -> dict:
    try:
        return await clients.get("asks", "/internal/asks")
    except DomainError:
        return {"asks": [], "sprint": None}


async def _today() -> dict:
    t, asks, meter, cards = await asyncio.gather(clients.get("plan", "/internal/plan/today"), _asks(), _meter(), _card_views())
    nxt = next((a for a in asks["asks"] if a["status"] == "queued"), None)
    return {**t, "next_ask": nxt, "sprint": asks.get("sprint"), "meter": meter, "cards": cards}


@public.get("/today")
async def today_ep(ctx: Ctx = Depends(entitled)):
    return await _today()


class PaidReq(BaseModel):
    amount_cents: int | None = Field(default=None, ge=1, le=5_000_000)


@public.post("/plan/instructions/{ins_id}/paid")
async def paid(ins_id: str, req: PaidReq, ctx: Ctx = Depends(entitled)):
    await clients.post("plan", f"/internal/plan/instructions/{ins_id}/paid", json=req.model_dump(exclude_none=True))
    await _settle()
    return await _today()


@public.post("/plan/instructions/{ins_id}/undo")
async def undo(ins_id: str, ctx: Ctx = Depends(entitled)):
    await clients.post("plan", f"/internal/plan/instructions/{ins_id}/undo")
    await _settle()
    return await _today()


class ShortReq(BaseModel):
    available_cents: int = Field(ge=0, le=5_000_000)


@public.post("/plan/instructions/{ins_id}/short")
async def short(ins_id: str, req: ShortReq, ctx: Ctx = Depends(entitled)):
    await clients.post("plan", f"/internal/plan/instructions/{ins_id}/short", json=req.model_dump())
    return await _today()


@public.get("/plan/schedule")
async def schedule(ctx: Ctx = Depends(entitled)):
    return await clients.get("plan", "/internal/plan/schedule")


@public.put("/plan/schedule")
async def schedule_put(req: ScheduleIn, ctx: Ctx = Depends(entitled)):
    return await clients.put("plan", "/internal/plan/schedule", json=req.model_dump(mode="json"))


@public.get("/plan/projection")
async def projection(ctx: Ctx = Depends(entitled)):
    return await clients.get("plan", "/internal/plan/projection")


@public.get("/meter")
async def meter(ctx: Ctx = Depends(entitled)):
    return await _meter()


# ---------- product: cards ----------

@public.get("/cards")
async def cards(ctx: Ctx = Depends(entitled)):
    views = await _card_views()
    return {"cards": views, "burn_per_day": round(sum(c["burn_per_day"] for c in views), 4),
            "total_balance_cents": sum(c["balance_cents"] for c in views)}


async def _one_card(card_id: str) -> dict:
    card = next((c for c in await _card_views() if c["id"] == card_id), None)
    if not card:
        raise NotFound("card not found", code="card_not_found")
    return card


@public.post("/cards")
async def card_add(req: CardIn, ctx: Ctx = Depends(entitled)):
    c = await clients.post("cards", "/internal/cards", json=req.model_dump(mode="json"))
    await _settle()
    return await _one_card(c["id"])


class CardPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    last4: str | None = Field(default=None, max_length=4)
    close_day: int | None = Field(default=None, ge=1, le=31)
    due_day: int | None = Field(default=None, ge=1, le=28)
    autopay: str | None = Field(default=None, pattern="^(min|none|full)$")
    statement_min_cents: int | None = Field(default=None, ge=0)


@public.patch("/cards/{card_id}")
async def card_patch(card_id: str, req: CardPatch, ctx: Ctx = Depends(entitled)):
    await clients.call("cards", "PATCH", f"/internal/cards/{card_id}", json=req.model_dump(exclude_unset=True))
    await _settle()
    return await _one_card(card_id)


@public.delete("/cards/{card_id}")
async def card_delete(card_id: str, ctx: Ctx = Depends(entitled)):
    await clients.call("cards", "DELETE", f"/internal/cards/{card_id}")
    await _settle()
    return {"ok": True}


class ChargeReq(BaseModel):
    amount_cents: int = Field(ge=1, le=5_000_000)
    date: dt.date | None = None
    note: str = Field(default="", max_length=200)


@public.post("/cards/{card_id}/charges")
async def card_charge(card_id: str, req: ChargeReq, ctx: Ctx = Depends(entitled)):
    """A new purchase landed on a card in payoff. Recorded without comment; the plan adjusts."""
    await clients.post("cards", f"/internal/cards/{card_id}/events", json={
        "type": "purchase", "amount_cents": req.amount_cents, "effective_date": (req.date or today()).isoformat(),
        "source": "user", "note": req.note or "New purchase"})
    await _settle()
    return await _one_card(card_id)


@public.get("/cards/{card_id}/ledger")
async def card_ledger(card_id: str, ctx: Ctx = Depends(entitled)):
    return await clients.get("cards", f"/internal/cards/{card_id}/ledger")


@public.get("/cards/{card_id}/refinance")
async def card_refinance(card_id: str, monthly_payment_cents: int | None = None, ctx: Ctx = Depends(entitled)):
    card = await _one_card(card_id)
    if card["balance_cents"] <= 0:
        raise DomainError("This card has no balance to refinance.", code="no_balance")
    pay = monthly_payment_cents or max(card["min_payment_cents"] * 2, 10_000)
    return await clients.post("engine", "/internal/engine/refinance", json={
        "balance_cents": card["balance_cents"], "apr_bps": card["apr_bps"], "monthly_payment_cents": max(pay, 1)})


# ---------- product: statements ----------

@public.post("/statements")
async def statement_upload(card_id: str = Form(...), file: UploadFile | None = File(default=None), text: str | None = Form(default=None),
                           ctx: Ctx = Depends(entitled)):
    await ratelimit.check(f"stmt:{ctx.user_id}", 12, 3600, "That is a lot of statements for one hour. Try again later.")
    body: dict = {"card_id": card_id, "text": text}
    if file is not None:
        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            raise DomainError("Files up to 10 MB are accepted.", code="file_too_large", status=413)
        if file.content_type not in ("application/pdf", "image/png", "image/jpeg", "image/webp", "text/plain"):
            raise DomainError("Upload a PDF, PNG or JPG.", code="bad_file_type", status=415)
        body.update(filename=file.filename or "statement", mime=file.content_type, data_b64=base64.b64encode(data).decode())
    st = await clients.post("statements", "/internal/statements/upload", json=body)
    await _settle()
    return await clients.get("statements", f"/internal/statements/{st['id']}")


class ManualStatement(BaseModel):
    card_id: str
    fields: dict


@public.post("/statements/manual")
async def statement_manual(req: ManualStatement, ctx: Ctx = Depends(entitled)):
    st = await clients.post("statements", "/internal/statements/manual", json=req.model_dump())
    await _settle()
    return await clients.get("statements", f"/internal/statements/{st['id']}")


@public.get("/statements")
async def statement_list(card_id: str | None = None, ctx: Ctx = Depends(entitled)):
    return await clients.get("statements", "/internal/statements", params={"card_id": card_id} if card_id else None)


@public.get("/statements/{statement_id}")
async def statement_get(statement_id: str, ctx: Ctx = Depends(entitled)):
    return await clients.get("statements", f"/internal/statements/{statement_id}")


class ConfirmReq(BaseModel):
    fields: dict = {}


@public.post("/statements/{statement_id}/confirm")
async def statement_confirm(statement_id: str, req: ConfirmReq, ctx: Ctx = Depends(entitled)):
    await clients.post("statements", f"/internal/statements/{statement_id}/confirm", json=req.model_dump())
    await _settle()
    return await clients.get("statements", f"/internal/statements/{statement_id}")


# ---------- product: asks and rehearsal ----------

@public.get("/asks")
async def asks(ctx: Ctx = Depends(entitled)):
    return await clients.get("asks", "/internal/asks")


@public.get("/asks/{ask_id}")
async def ask_get(ask_id: str, ctx: Ctx = Depends(entitled)):
    return await clients.get("asks", f"/internal/asks/{ask_id}")


@public.post("/asks/{ask_id}/outcome")
async def ask_outcome(ask_id: str, req: dict, ctx: Ctx = Depends(entitled)):
    r = await clients.post("asks", f"/internal/asks/{ask_id}/outcome", json=req)
    await _settle()
    return {"ask": r.get("ask", r), "today": await _today()}


@public.post("/asks/{ask_id}/skip")
async def ask_skip(ask_id: str, ctx: Ctx = Depends(entitled)):
    return await clients.post("asks", f"/internal/asks/{ask_id}/skip")


@public.get("/playbook")
async def playbook(issuer: str = "", type: str = "", ctx: Ctx = Depends(entitled)):
    return await clients.get("asks", "/internal/asks/playbook", params={"issuer": issuer, "type": type})


class RehearsalStart(BaseModel):
    ask_id: str


@public.post("/rehearsal")
async def rehearsal_start(req: RehearsalStart, ctx: Ctx = Depends(entitled)):
    await ratelimit.check(f"reh-start:{ctx.user_id}", 20, 3600, "Practice is paused for an hour. The script is still available.")
    return await clients.post("rehearsal", "/internal/rehearsal", json=req.model_dump(), timeout=60)


@public.get("/rehearsal/{rid}")
async def rehearsal_get(rid: str, ctx: Ctx = Depends(entitled)):
    return await clients.get("rehearsal", f"/internal/rehearsal/{rid}")


class TurnReq(BaseModel):
    text: str = Field(min_length=1, max_length=600)


@public.post("/rehearsal/{rid}/turn")
async def rehearsal_turn(rid: str, req: TurnReq, ctx: Ctx = Depends(entitled)):
    """Server-sent events: `delta` with a piece of the reply, then `done` with the whole session."""
    await ratelimit.check(f"reh-turn:{ctx.user_id}", 40, 600, "Slow down a little. Try again in a minute.")
    lines = clients.stream_lines("rehearsal", "POST", f"/internal/rehearsal/{rid}/turn", json=req.model_dump())
    try:
        first = await anext(lines)  # errors such as "not found" surface as a normal JSON error, not a broken stream
    except StopAsyncIteration:
        first = None

    async def events():
        async def emit(line: str):
            data = json.loads(line)
            if "delta" in data:
                yield f"event: delta\ndata: {json.dumps({'text': data['delta']})}\n\n"
            elif "done" in data:
                yield f"event: done\ndata: {json.dumps(data['done'])}\n\n"

        if first:
            async for chunk in emit(first):
                yield chunk
        async for line in lines:
            async for chunk in emit(line):
                yield chunk

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


# ---------- other ----------

class PrefsReq(BaseModel):
    phone: str | None = Field(default=None, max_length=32)
    sms_opt_in: bool | None = None
    email_opt_in: bool | None = None
    timezone: str | None = Field(default=None, max_length=64)


@public.get("/notifications/prefs")
async def prefs(ctx: Ctx = Depends(current)):
    return await clients.get("notifications", "/internal/notifications/prefs")


@public.put("/notifications/prefs")
async def prefs_put(req: PrefsReq, ctx: Ctx = Depends(current)):
    body = req.model_dump(exclude_unset=True)
    if body.get("sms_opt_in"):
        await clients.post("identity", f"/internal/identity/users/{ctx.user_id}/consents", json={
            "kind": "sms", "text": "I agree to receive payday instructions from Burn Rate by text message. Reply STOP to opt out."})
    return await clients.put("notifications", "/internal/notifications/prefs", json=body)


@public.get("/calendar.ics")
async def calendar(u: str, sig: str):
    """Paydays, closing dates and due dates as a calendar feed. The link is unguessable; no cookie is needed."""
    if sig != signed("cal:" + u):
        raise Forbidden("bad calendar link", code="bad_link")
    set_ctx(Ctx(user_id=u, request_id=get_ctx().request_id))
    events = (await clients.get("plan", "/internal/plan/timeline", params={"days": 120}))["events"]
    stamp = real_now().strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Burn Rate//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:Burn Rate"]
    for i, e in enumerate(events):
        d = e["date"].replace("-", "")
        nxt = (dt.date.fromisoformat(e["date"]) + dt.timedelta(days=1)).strftime("%Y%m%d")
        out += ["BEGIN:VEVENT", f"UID:{u[:8]}-{d}-{i}@burnrate", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{nxt}",
                f"SUMMARY:{e['title']}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return Response("\r\n".join(out) + "\r\n", media_type="text/calendar")


class EventsReq(BaseModel):
    events: list[dict] = Field(min_length=1, max_length=50)


@public.post("/events", status_code=202)
async def events(req: EventsReq, ctx: Ctx = Depends(current)):
    try:
        await clients.post("analytics-api", "/internal/analytics/events", json=req.model_dump())
    except DomainError:
        pass  # analytics never breaks the product
    return {"accepted": len(req.events)}


@public.get("/health")
async def health():
    return {"ok": True, "version": "0.1.0", "engine_version": ENGINE_VERSION}
