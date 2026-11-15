"""OpenAI through the official `openai` SDK (written against openai 3.51.0, Responses API).

- statements: `responses.create` with `text.format` = a strict JSON schema; a PDF goes in as an `input_file`
  (base64 data URL), an image as an `input_image`, plain text as `input_text` inside <statement> tags;
- chat: `responses.create(stream=True)`, reading `response.output_text.delta` events;
- `store=False`: nothing is kept on the provider's side after the answer;
- no `temperature` / `top_p`; reasoning models get `reasoning.effort = "low"`.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import openai

from burnrate.common.llm.base import (
    EXTRACT_SYSTEM,
    EXTRACT_USER,
    IMAGES,
    PDF,
    ChatDelta,
    ChatDone,
    Document,
    ExtractResult,
    LLMBadOutput,
    LLMError,
    LLMRefusal,
    Usage,
    statement_schema,
)

EFFORT = "low"
EXTRACT_MAX_TOKENS = 2000
REASONING_HEADROOM = 800  # max_output_tokens also covers reasoning tokens
_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")  # models that accept the `reasoning` parameter


def _reasoning(model: str) -> dict:
    return {"reasoning": {"effort": EFFORT}} if model.startswith(_REASONING_PREFIXES) else {}


def _document_part(doc: Document) -> dict:
    if doc.text is not None:
        return {"type": "input_text", "text": f"<statement>\n{doc.text}\n</statement>"}
    if doc.mime == PDF:
        return {"type": "input_file", "filename": doc.filename or "statement.pdf",
                "file_data": f"data:{PDF};base64,{doc.data_b64}"}
    if doc.mime in IMAGES:
        return {"type": "input_image", "image_url": f"data:{doc.mime};base64,{doc.data_b64}", "detail": "high"}
    raise LLMError(f"Cannot read a file of type {doc.mime or 'unknown'}.", code="unsupported_file", status=422)


def build_extract_request(model: str, doc: Document) -> dict:
    """Keyword arguments for `client.responses.create`. Pure: no client, no network."""
    return {
        "model": model,
        "instructions": EXTRACT_SYSTEM,
        "input": [{"role": "user", "content": [_document_part(doc), {"type": "input_text", "text": EXTRACT_USER}]}],
        "text": {"format": {"type": "json_schema", "name": "statement_summary", "strict": True,
                            "schema": statement_schema(all_required=True)}},
        "max_output_tokens": EXTRACT_MAX_TOKENS + REASONING_HEADROOM,
        "store": False,
        **_reasoning(model),
    }


def build_chat_request(model: str, system: str, messages: list[dict], max_tokens: int) -> dict:
    """Keyword arguments for `client.responses.create` with streaming on."""
    request: dict = {
        "model": model,
        "input": [{"role": m["role"], "content": m["content"]} for m in messages],
        "max_output_tokens": max_tokens + REASONING_HEADROOM,
        "store": False,
        "stream": True,
        **_reasoning(model),
    }
    if system:
        request["instructions"] = system
    return request


def _usage(response) -> Usage:
    u = getattr(response, "usage", None)
    return Usage(input_tokens=(u.input_tokens or 0), output_tokens=(u.output_tokens or 0)) if u else Usage()


def _refusal_text(response) -> str | None:
    for item in response.output or []:
        if item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    return part.refusal
    return None


@contextmanager
def _sdk_errors() -> Iterator[None]:
    """Turn the SDK's typed errors into gateway errors, most specific class first."""
    try:
        yield
    except openai.BadRequestError as e:  # 400
        raise LLMError(f"OpenAI rejected the request: {e.message}", code="llm_bad_request", status=502) from e
    except openai.AuthenticationError as e:  # 401
        raise LLMError("The OpenAI API key was not accepted.", code="llm_auth_failed", status=502) from e
    except openai.PermissionDeniedError as e:  # 403
        raise LLMError("The OpenAI API key may not use this model.", code="llm_auth_failed", status=502) from e
    except openai.NotFoundError as e:  # 404
        raise LLMError("This OpenAI model is not available to the configured key.", code="llm_model_not_found", status=502) from e
    except openai.RateLimitError as e:  # 429 from the provider (rate or quota), not our own budget
        raise LLMError("OpenAI is rate limiting this key, or its quota is used up.", code="llm_rate_limited", status=503) from e
    except openai.InternalServerError as e:  # 5xx
        raise LLMError("OpenAI had a server error. Try again in a minute.", code="llm_unavailable", status=503) from e
    except openai.APIStatusError as e:
        raise LLMError(f"OpenAI answered {e.status_code}.", code="llm_unavailable", status=503) from e
    except openai.APITimeoutError as e:  # subclass of APIConnectionError, so it comes first
        raise LLMError("OpenAI did not answer in time.", code="llm_timeout", status=503) from e
    except openai.APIConnectionError as e:
        raise LLMError("Could not reach OpenAI.", code="llm_unreachable", status=503) from e
    except openai.APIError as e:
        raise LLMError("OpenAI call failed.", code="llm_unavailable", status=503) from e


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str = "", *, client: openai.AsyncOpenAI | None = None):
        self.model = model
        self.client = client or openai.AsyncOpenAI(api_key=api_key, max_retries=1, timeout=90.0)

    async def extract(self, doc: Document) -> ExtractResult:
        request = build_extract_request(self.model, doc)
        with _sdk_errors():
            response = await self.client.responses.create(**request)
        usage = _usage(response)
        if response.error is not None:
            raise LLMError(f"OpenAI could not finish: {response.error.message}", code="llm_unavailable", status=503)
        refusal = _refusal_text(response)
        if refusal:
            raise LLMRefusal("The model declined this request.")
        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else None
            if reason == "content_filter":
                raise LLMRefusal("The model's answer was stopped by a content filter.")
            raise LLMBadOutput("The model ran out of room before finishing the statement fields.")
        try:
            raw = json.loads(response.output_text)
        except json.JSONDecodeError as e:
            raise LLMBadOutput("The model did not return valid JSON.") from e
        return ExtractResult(raw=raw, usage=usage)

    async def chat_stream(self, system: str, messages: list[dict], max_tokens: int) -> AsyncIterator[ChatDelta | ChatDone]:
        request = build_chat_request(self.model, system, messages, max_tokens)
        parts: list[str] = []
        final = None
        truncated = False
        with _sdk_errors():
            stream = await self.client.responses.create(**request)
            async with stream:
                async for event in stream:
                    kind = event.type
                    if kind == "response.output_text.delta":
                        if event.delta:
                            parts.append(event.delta)
                            yield ChatDelta(event.delta)
                    elif kind == "response.refusal.done":
                        raise LLMRefusal("The model declined this request.")
                    elif kind == "response.completed":
                        final = event.response
                    elif kind == "response.incomplete":
                        final = event.response
                        reason = final.incomplete_details.reason if final.incomplete_details else None
                        if reason == "content_filter":
                            raise LLMRefusal("The model's answer was stopped by a content filter.")
                        truncated = True
                    elif kind == "response.failed":
                        message = event.response.error.message if event.response.error else "unknown error"
                        raise LLMError(f"OpenAI could not finish: {message}", code="llm_unavailable", status=503)
                    elif kind == "error":
                        raise LLMError(f"OpenAI stream error: {event.message}", code="llm_unavailable", status=503)
        text = "".join(parts)
        yield ChatDone(text=text, usage=_usage(final) if final is not None else Usage(), truncated=truncated)
