"""Provider pricing helpers used by the Web dashboard.

Only prices that can be tied to an official provider price table belong here.
The translation pipeline may use OpenAI-compatible relays, so an estimate is
informational and must never be presented as the relay's final bill.
"""
from __future__ import annotations

from typing import Any


DEEPSEEK_PRICING_CNY_PER_MILLION: dict[str, dict[str, float]] = {
    # https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    # Cache-miss input pricing is intentionally used for a conservative
    # estimate because the current usage metrics do not expose cached tokens.
    "deepseek-v4-flash": {"input_cache_miss": 1.0, "output": 2.0},
    "deepseek-v4-pro": {"input_cache_miss": 3.0, "output": 6.0},
}

DEEPSEEK_PRICING_SOURCE = (
    "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
)
DEEPSEEK_PRICING_UPDATED = "2026-07-30"


def estimate_model_cost(
    model: str,
    tokens_in: int | float = 0,
    tokens_out: int | float = 0,
) -> dict[str, Any]:
    """Return an explicit cost-estimate contract for one translation stage."""
    normalized_model = str(model or "").strip().lower()
    rates = DEEPSEEK_PRICING_CNY_PER_MILLION.get(normalized_model)
    if rates is None:
        return {
            "estimable": False,
            "model": normalized_model,
            "reason": "unsupported_model",
        }

    safe_tokens_in = max(0, int(tokens_in or 0))
    safe_tokens_out = max(0, int(tokens_out or 0))
    amount = (
        safe_tokens_in * rates["input_cache_miss"]
        + safe_tokens_out * rates["output"]
    ) / 1_000_000
    return {
        "estimable": True,
        "model": normalized_model,
        "currency": "CNY",
        "amount": round(amount, 8),
        "tokens_in": safe_tokens_in,
        "tokens_out": safe_tokens_out,
        "input_rate": rates["input_cache_miss"],
        "output_rate": rates["output"],
        "basis": "cache_miss",
        "source": DEEPSEEK_PRICING_SOURCE,
        "updated_at": DEEPSEEK_PRICING_UPDATED,
        "notice": "按 DeepSeek 官方缓存未命中价估算；中转站实际账单可能不同",
    }
