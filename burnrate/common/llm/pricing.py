"""Token prices in US dollars per million tokens (input, output). Kept in code on purpose: a wrong price here
only makes the budget stricter or looser, and the owner can read the whole table at a glance."""
from __future__ import annotations

from burnrate.common.llm.base import Usage

PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.4-mini": (0.75, 4.50),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Claude Haiku 5.5 costs five times more once a prompt is longer than 100K tokens.
LONG_PROMPT: dict[str, tuple[int, tuple[float, float]]] = {"claude-haiku-5-5": (100_000, (0.50, 2.50))}
MOST_EXPENSIVE: tuple[float, float] = max(PRICES.values(), key=lambda p: (p[1], p[0]))


def price_for(model: str, input_tokens: int = 0) -> tuple[float, float]:
    """A dated snapshot ("gpt-5.4-mini-2026-03-17") takes its family's price; an unknown model takes the highest."""
    known = model if model in PRICES else next((k for k in sorted(PRICES, key=len, reverse=True) if model.startswith(k + "-")), None)
    if known is None:
        return MOST_EXPENSIVE
    threshold, long_price = LONG_PROMPT.get(known, (0, PRICES[known]))
    return long_price if threshold and input_tokens > threshold else PRICES[known]


def cost_usd(provider: str, model: str, usage: Usage) -> float:
    if provider == "fake":
        return 0.0
    inp, out = price_for(model, usage.input_tokens)
    return round((usage.input_tokens * inp + usage.output_tokens * out) / 1_000_000, 6)
