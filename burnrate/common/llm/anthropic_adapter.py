"""Claude through the official `anthropic` SDK (written against anthropic 2.8.0, Messages API).

What this adapter deliberately does and does not send:
- structured output through `output_config.format` (a JSON schema); no assistant prefill, no forced `tool_choice`;
- `output_config.effort = "low"`: reading a statement and playing a phone rep need little reasoning;
- no `temperature` / `top_p` (removed from the SDK and rejected by current models) and no `thinking` config
  (Claude Opus 5.5 thinks adaptively on its own; effort is the control);
- PDFs as `document` blocks, images as `image` blocks, plain text as a text `document`, so the statement is never
  mixed into the instructions;
- chat replies are streamed; `stop_reason` is read before the content, and a refusal is an ordinary handled error.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import anthropic

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
TIMEOUT_S = 50.0  # with one retry this stays under the 120 s the statements service waits for an extraction
EXTRACT_MAX_TOKENS = 2048
THINKING_HEADROOM = 1024  # max_tokens also covers the model's own reasoning; leave room for it above the reply
_NO_EFFORT_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5")  # older models reject output_config.effort


def _output_config(model: str, schema: dict | None = None) -> dict:
    config: dict = {}
    if not model.startswith(_NO_EFFORT_PREFIXES):
        config["effort"] = EFFORT
    if schema is not None:
        config["format"] = {"type": "json_schema", "schema": schema}
    return config


def _document_block(doc: Document) -> dict:
    if doc.text is not None:
        return {"type": "document", "title": doc.filename or "statement.txt",
                "source": {"type": "text", "media_type": "text/plain", "data": doc.text}}
    if doc.mime == PDF:
        return {"type": "document", "title": doc.filename or "statement.pdf",
                "source": {"type": "base64", "media_type": PDF, "data": doc.data_b64}}
    if doc.mime in IMAGES:
        return {"type": "image", "source": {"type": "base64", "media_type": doc.mime, "data": doc.data_b64}}
    raise LLMError(f"Cannot read a file of type {doc.mime or 'unknown'}.", code="unsupported_file", status=422)


def build_extract_request(model: str, doc: Document) -> dict:
    """Keyword arguments for `client.messages.create`. Pure: no client, no network."""
    request = {
        "model": model,
        "max_tokens": EXTRACT_MAX_TOKENS,
        "system": EXTRACT_SYSTEM,
        "messages": [{"role": "user", "content": [_document_block(doc), {"type": "text", "text": EXTRACT_USER}]}],
    }
    request["output_config"] = _output_config(model, statement_schema(all_required=False))
    return request


def build_chat_request(model: str, system: str, messages: list[dict], max_tokens: int) -> dict:
    """Keyword arguments for `client.messages.stream`. The conversation must end with the user's turn."""
    request: dict = {
        "model": model,
        "max_tokens": max_tokens + THINKING_HEADROOM,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    if system:
        request["system"] = system
    config = _output_config(model)
    if config:
        request["output_config"] = config
    return request


def _usage(message) -> Usage:
    u = message.usage
    cached = (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
    return Usage(input_tokens=(u.input_tokens or 0) + cached, output_tokens=u.output_tokens or 0)


def _text(message) -> str:
    return "".join(block.text for block in message.content if block.type == "text")


def _refusal(message) -> LLMRefusal:
    details = message.stop_details
    category = getattr(details, "category", None) if details else None
    return LLMRefusal("The model declined this request." + (f" (category: {category})" if category else ""), usage=_usage(message))


@contextmanager
def _sdk_errors() -> Iterator[None]:
    """Turn the SDK's typed errors into gateway errors, most specific class first."""
    try:
        yield
    except anthropic.BadRequestError as e:  # 400: our request is wrong (bad parameter, unsupported file)
        raise LLMError(f"Claude rejected the request: {e.message}", code="llm_bad_request", status=502) from e
    except anthropic.AuthenticationError as e:  # 401
        raise LLMError("The Anthropic API key was not accepted.", code="llm_auth_failed", status=502) from e
    except anthropic.PermissionDeniedError as e:  # 403
        raise LLMError("The Anthropic API key may not use this model.", code="llm_auth_failed", status=502) from e
    except anthropic.NotFoundError as e:  # 404: unknown model, or one this key cannot use
        raise LLMError("This Claude model is not available to the configured key.", code="llm_model_not_found", status=502) from e
    except anthropic.RequestTooLargeError as e:  # 413
        raise LLMError("The file is too large for the model.", code="llm_request_too_large", status=413) from e
    except anthropic.RateLimitError as e:  # 429 from the provider, not our own budget
        raise LLMError("Claude is rate limiting this key. Try again in a minute.", code="llm_rate_limited", status=503) from e
    except anthropic.OverloadedError as e:  # 529
        raise LLMError("Claude is overloaded. Try again in a minute.", code="llm_unavailable", status=503) from e
    except anthropic.InternalServerError as e:  # 5xx
        raise LLMError("Claude had a server error. Try again in a minute.", code="llm_unavailable", status=503) from e
    except anthropic.APIStatusError as e:  # any other HTTP status
        raise LLMError(f"Claude answered {e.status_code}.", code="llm_unavailable", status=503) from e
    except anthropic.APITimeoutError as e:  # subclass of APIConnectionError, so it comes first
        raise LLMError("Claude did not answer in time.", code="llm_timeout", status=503) from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Could not reach Claude.", code="llm_unreachable", status=503) from e
    except anthropic.APIError as e:  # anything else the SDK raises, e.g. a response it could not parse
        raise LLMError("Claude call failed.", code="llm_unavailable", status=503) from e


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str = "", *, client: anthropic.AsyncAnthropic | None = None):
        self.model = model
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, timeout=TIMEOUT_S)

    async def extract(self, doc: Document) -> ExtractResult:
        request = build_extract_request(self.model, doc)
        with _sdk_errors():
            message = await self.client.messages.create(**request)
        usage = _usage(message)
        if message.stop_reason == "refusal":
            raise _refusal(message)
        if message.stop_reason == "max_tokens":
            raise LLMBadOutput("The model ran out of room before finishing the statement fields.", usage=usage)
        try:
            raw = json.loads(_text(message))
        except json.JSONDecodeError as e:
            raise LLMBadOutput("The model did not return valid JSON.", usage=usage) from e
        return ExtractResult(raw=raw, usage=usage)

    async def chat_stream(self, system: str, messages: list[dict], max_tokens: int) -> AsyncIterator[ChatDelta | ChatDone]:
        request = build_chat_request(self.model, system, messages, max_tokens)
        with _sdk_errors():
            async with self.client.messages.stream(**request) as stream:
                async for piece in stream.text_stream:
                    if piece:
                        yield ChatDelta(piece)
                message = await stream.get_final_message()
        if message.stop_reason == "refusal":
            raise _refusal(message)
        yield ChatDone(text=_text(message), usage=_usage(message), truncated=message.stop_reason == "max_tokens")
