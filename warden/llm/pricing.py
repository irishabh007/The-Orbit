"""List prices for cost estimates in run reports (USD per 1M tokens).

Estimates only: verify against the provider's pricing page. Thinking tokens are billed
as output. Unknown models are reported as unpriced, never guessed.
"""

from __future__ import annotations

# model -> (input, output) per 1M tokens. Source: ai.google.dev/gemini-api/docs/pricing,
# standard paid tier, introductory price valid until 2026-12-31 (doubles from 2027-01-01).
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int, thinking_tokens: int = 0) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + (output_tokens + thinking_tokens) * price[1]) / 1_000_000
