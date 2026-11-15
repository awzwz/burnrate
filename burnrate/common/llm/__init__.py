"""Language-model providers behind one interface. Only the llm-gateway service imports this package."""
from __future__ import annotations

from burnrate.common.llm.base import (
    ChatDelta,
    ChatDone,
    Document,
    ExtractResult,
    LLMBadOutput,
    LLMError,
    LLMRefusal,
    Provider,
    Usage,
    to_statement_fields,
)

PROVIDERS = ("openai", "anthropic", "fake")

__all__ = ["PROVIDERS", "ChatDelta", "ChatDone", "Document", "ExtractResult", "LLMBadOutput", "LLMError", "LLMRefusal",
           "Provider", "Usage", "make_provider", "to_statement_fields"]


def make_provider(name: str, model: str, api_key: str = "") -> Provider:
    """Adapters import their SDK lazily, so a deployment that only uses one provider never loads the other."""
    if name == "openai":
        from burnrate.common.llm.openai_adapter import OpenAIProvider

        return OpenAIProvider(model, api_key)
    if name == "anthropic":
        from burnrate.common.llm.anthropic_adapter import AnthropicProvider

        return AnthropicProvider(model, api_key)
    from burnrate.common.llm.fake import FakeProvider

    return FakeProvider()
