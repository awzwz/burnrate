"""llm-gateway: the only service that talks to a language model.

It picks the provider (OpenAI by default, Claude selectable, a free scripted fake when no key is configured),
enforces the daily spending limits before every call, records what each call cost, and validates what comes back.
The model reads statements and plays a phone representative; it never calculates and never decides.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Date, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from burnrate.common.config import Settings, settings
from burnrate.common.context import Ctx, get_ctx, real_now
from burnrate.common.db import UTCDateTime, make_base, new_id, session
from burnrate.common.errors import DomainError, RateLimited
from burnrate.common.llm import (
    PROVIDERS,
    ChatDelta,
    Document,
    LLMError,
    Provider,
    Usage,
    make_provider,
    to_statement_fields,
)
from burnrate.common.llm.base import IMAGES, PDF, estimate_tokens
from burnrate.common.llm.fake import MODEL as FAKE_MODEL
from burnrate.common.llm.pricing import cost_usd

SVC = "llm-gateway"
Base = make_base(SVC)
router = APIRouter(prefix="/internal/llm", tags=["llm-gateway"])
log = logging.getLogger("burnrate.llm")

MAX_FILE_BYTES = 10 * 1024 * 1024
NDJSON = "application/x-ndjson"


class UsageRow(Base):
    """One row per call to a provider. The budget is the sum of `cost_usd` over a UTC day."""

    __tablename__ = "usage"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    day: Mapped[dt.date] = mapped_column(Date, index=True)  # real UTC day, never the demo clock
    user_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    demo: Mapped[bool] = mapped_column(default=False)
    task: Mapped[str] = mapped_column(String(40), default="")
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    ok: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class ConfigRow(Base):
    """Run-time choice of provider and model; one row, read on every request."""

    __tablename__ = "config"
    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


# ---------- which provider answers ----------

@dataclass(frozen=True)
class Active:
    provider: str  # the one that will actually answer
    model: str
    live: bool  # False whenever the fake answers
    configured_provider: str
    configured_model: str | None
    note: str = ""


def resolve(provider: str | None, model: str | None, st: Settings) -> Active:
    """Pure: stored choice + settings -> who answers. No key, or a test run, always means the fake."""
    configured = provider or st.llm_provider
    if configured not in PROVIDERS:
        return Active("fake", FAKE_MODEL, False, configured, model, f"unknown provider '{configured}': using the fake")
    if configured == "fake":
        return Active("fake", FAKE_MODEL, False, "fake", None)
    key, default_model = ((st.openai_api_key, st.openai_model) if configured == "openai"
                          else (st.anthropic_api_key, st.anthropic_model))
    wanted = model or default_model
    if st.is_test:
        return Active("fake", FAKE_MODEL, False, configured, wanted, "test environment: live calls are disabled")
    if not key:
        return Active("fake", FAKE_MODEL, False, configured, wanted, f"no API key for {configured}: using the fake")
    return Active(configured, wanted, True, configured, wanted)


_providers: dict[tuple[str, str, str], Provider] = {}


def _provider(active: Active) -> Provider:
    st = settings()
    key = {"openai": st.openai_api_key, "anthropic": st.anthropic_api_key}.get(active.provider, "")
    cache_key = (active.provider, active.model, key)
    if cache_key not in _providers:
        _providers[cache_key] = make_provider(active.provider, active.model, key)
    return _providers[cache_key]


async def _active() -> Active:
    async with session(SVC) as s:
        row = await s.get(ConfigRow, "active")
    return resolve(row.provider if row else None, row.model if row else None, settings())


# ---------- budgets and the usage log ----------

def _utc_day() -> dt.date:
    return real_now().date()


async def _spent(s, day: dt.date, user_id: str | None = None) -> float:
    q = select(func.coalesce(func.sum(UsageRow.cost_usd), 0.0)).where(UsageRow.day == day)
    if user_id is not None:
        q = q.where(UsageRow.user_id == user_id)
    return float((await s.execute(q)).scalar_one())


def _user_budget(ctx: Ctx, st: Settings) -> float:
    return st.llm_guest_daily_budget_usd if ctx.demo else st.llm_user_daily_budget_usd


async def _check_budget(ctx: Ctx) -> None:
    """Refuse before spending anything when today's limit for the deployment or for this user is used up."""
    st = settings()
    day = _utc_day()
    async with session(SVC) as s:
        total = await _spent(s, day)
        mine = await _spent(s, day, ctx.user_id) if ctx.user_id else 0.0
    if total >= st.llm_daily_budget_usd:
        raise RateLimited("The AI features are paused until tomorrow (UTC). Everything else keeps working.",
                          code="llm_budget_exceeded", details={"scope": "deployment"})
    if ctx.user_id and mine >= _user_budget(ctx, st):
        raise RateLimited("You have used today's AI allowance. It resets at midnight UTC.",
                          code="llm_budget_exceeded", details={"scope": "guest" if ctx.demo else "user"})


async def _record(ctx: Ctx, task: str, active: Active, usage: Usage, ok: bool = True) -> float:
    cost = cost_usd(active.provider, active.model, usage)
    async with session(SVC) as s:
        s.add(UsageRow(id=new_id(), day=_utc_day(), user_id=ctx.user_id, demo=ctx.demo, task=task[:40],
                       provider=active.provider, model=active.model, input_tokens=usage.input_tokens,
                       output_tokens=usage.output_tokens, cost_usd=cost, ok=ok, created_at=real_now()))
    return cost


def _domain(e: LLMError) -> DomainError:
    return DomainError(e.message, code=e.code, status=e.status)


# ---------- endpoints ----------

class ExtractReq(BaseModel):
    filename: str = Field(default="", max_length=200)
    mime: str = Field(default="", max_length=100)
    data_b64: str | None = Field(default=None, max_length=14_500_000)
    text: str | None = Field(default=None, max_length=200_000)


def _document(req: ExtractReq) -> Document:
    mime = req.mime.split(";")[0].strip().lower()
    mime = {"image/jpg": "image/jpeg", "application/x-pdf": PDF}.get(mime, mime)
    if req.text is not None and req.text.strip():
        return Document(filename=req.filename, mime="text/plain", text=req.text)
    if not req.data_b64:
        raise DomainError("Send the statement as text or as a file.", code="nothing_to_read", status=422)
    try:
        data = base64.b64decode(req.data_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise DomainError("The file is not valid base64.", code="bad_file", status=422) from e
    if len(data) > MAX_FILE_BYTES:
        raise DomainError("The file is larger than 10 MB.", code="file_too_large", status=413)
    if mime.startswith("text/"):
        return Document(filename=req.filename, mime="text/plain", text=data.decode("utf-8", errors="replace"))
    if mime != PDF and mime not in IMAGES:
        raise DomainError("Upload a PDF, PNG or JPG, or paste the statement as text.", code="unsupported_file", status=422)
    return Document(filename=req.filename, mime=mime, data_b64=req.data_b64)


@router.post("/extract-statement")
async def extract_statement(req: ExtractReq):
    """Summary fields of one statement, in the `StatementFields` shape. The transaction list is never asked for."""
    ctx = get_ctx()
    doc = _document(req)
    await _check_budget(ctx)
    active = await _active()
    try:
        result = await _provider(active).extract(doc)
    except LLMError as e:
        await _record(ctx, "extract-statement", active, Usage(), ok=False)
        raise _domain(e) from e
    cost = await _record(ctx, "extract-statement", active, result.usage)
    try:
        fields, dropped = to_statement_fields(result.raw)
    except LLMError as e:  # the call is paid for either way; the garbage stops here
        raise _domain(e) from e
    return {"fields": fields, "provider": active.provider, "model": active.model, "cost_usd": cost, "dropped": dropped}


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatReq(BaseModel):
    task: str = Field(default="chat", max_length=40)
    system: str = Field(default="", max_length=12_000)
    messages: list[Msg] = Field(min_length=1, max_length=80)
    max_tokens: int = Field(default=300, ge=16, le=1_000)
    stream: bool = False


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


async def _chat_events(ctx: Ctx, req: ChatReq, active: Active) -> AsyncIterator[dict]:
    """{"delta"} for each piece, then one {"done"} or one {"error"}. Usage is recorded exactly once, even when the
    provider fails half-way or the caller goes away (then from an estimate)."""
    messages = [m.model_dump() for m in req.messages]
    parts: list[str] = []
    recorded = False

    def estimate() -> Usage:
        return Usage(estimate_tokens(req.system, *[m["content"] for m in messages]), estimate_tokens("".join(parts)))

    try:
        done = None
        async for event in _provider(active).chat_stream(req.system, messages, req.max_tokens):
            if isinstance(event, ChatDelta):
                parts.append(event.text)
                yield {"delta": event.text}
            else:
                done = event
        text = done.text if done is not None else "".join(parts)
        usage = done.usage if done is not None and (done.usage.input_tokens or done.usage.output_tokens) else estimate()
        recorded = True
        cost = await _record(ctx, req.task, active, usage)
        yield {"done": True, "text": text, "provider": active.provider, "model": active.model, "cost_usd": cost}
    except LLMError as e:
        recorded = True
        await _record(ctx, req.task, active, estimate(), ok=False)
        yield {"error": {"code": e.code, "message": e.message, "status": e.status}}
    finally:
        if not recorded and parts:  # the caller went away mid-reply: the tokens were still paid for
            try:
                await _record(ctx, req.task, active, estimate(), ok=False)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 - the request is already being torn down
                log.warning("could not record usage of an abandoned chat stream")


@router.post("/chat")
async def chat(req: ChatReq):
    """One reply in a conversation. `stream: true` answers as NDJSON: {"delta": "..."} lines, then
    {"done": true, "text", "provider", "model", "cost_usd"}; a provider failure after the start ends the stream with
    {"error": {"code", "message", "status"}} instead."""
    ctx = get_ctx()
    if req.messages[0].role != "user" or req.messages[-1].role != "user":
        raise DomainError("A conversation starts and ends with a user message.", code="bad_messages", status=422)
    await _check_budget(ctx)
    active = await _active()
    if req.stream:
        async def body() -> AsyncIterator[bytes]:
            async for event in _chat_events(ctx, req, active):
                yield _line(event)

        return StreamingResponse(body(), media_type=NDJSON, headers={"cache-control": "no-store", "x-accel-buffering": "no"})
    final: dict = {}
    async for event in _chat_events(ctx, req, active):
        if "error" in event:
            raise DomainError(event["error"]["message"], code=event["error"]["code"], status=event["error"]["status"])
        if event.get("done"):
            final = event
    return {"text": final["text"], "provider": final["provider"], "model": final["model"], "cost_usd": final["cost_usd"]}


async def _status(ctx: Ctx) -> dict:
    st = settings()
    active = await _active()
    day = _utc_day()
    async with session(SVC) as s:
        total = await _spent(s, day)
        mine = await _spent(s, day, ctx.user_id) if ctx.user_id else 0.0
    return {"provider": active.provider, "model": active.model, "live": active.live,
            "spent_today_usd": round(total, 6), "budget_usd": st.llm_daily_budget_usd,
            "configured_provider": active.configured_provider, "configured_model": active.configured_model,
            "note": active.note, "day": day.isoformat(),
            "user_spent_today_usd": round(mine, 6), "user_budget_usd": _user_budget(ctx, st)}


@router.get("/status")
async def status():
    return await _status(get_ctx())


class ConfigReq(BaseModel):
    provider: Literal["openai", "anthropic", "fake"]
    model: str | None = Field(default=None, min_length=1, max_length=80)


@router.put("/config")
async def set_config(req: ConfigReq):
    """Switch provider and model without a restart. Leaving `model` out means the provider's default from settings."""
    async with session(SVC) as s:
        row = await s.get(ConfigRow, "active")
        if row is None:
            s.add(ConfigRow(key="active", provider=req.provider, model=req.model, updated_at=real_now()))
        else:
            row.provider, row.model, row.updated_at = req.provider, req.model, real_now()
    return await _status(get_ctx())
