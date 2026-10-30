# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Per-million-token prices (USD) for the owner's models, used to cost a request (kiko-lite has no LiteLLM).

Extracted at dev time (never read at run time) from ``model_prices_and_context_window.json`` in this repo
(LiteLLM's backup price map): ``input_cost_per_token`` / ``output_cost_per_token`` /
``cache_read_input_token_cost`` multiplied by 1e6. Key = ``"<kiko-lite provider>/<upstream model id>"``,
value = ``(input, output, cache_read | None)``. JSON keys used:

* ``gemini/gemini-3.8-flash``, ``gemini/gemini-3.5-flash-lite``
* ``gpt-4.1``, ``gpt-4.1-mini``, ``gpt-4o``
* ``groq/qwen/qwen3.8-27b``, ``groq/openai/gpt-oss-120b``
* ``dashscope/qwen-plus``, ``dashscope/qwen-max``, ``dashscope/qwen-turbo``

Deliberately NOT here (no entry for NVIDIA's own hosted NIM API in the JSON; only third-party hosts such as
nebius/deepinfra/openrouter list nemotron, and their prices are not NVIDIA's): ``nvidia/nemotron-3-super-120b-a12b``,
``nvidia/nemotron-3-ultra-550b-a55b``, ``nvidia/llama-3.1-nemotron-ultra-253b-v1``; also none for
``gemini-2.0-flash-lite`` / ``gemini-1.5-pro`` / ``gemini-2.0-flash-thinking-exp`` (kiko-sample.yaml). Such models
cost 0 until ``--price-in/--price-out`` is given; ``kiko doctor`` warns.
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float, float | None]] = {
    "gemini/gemini-3.8-flash": (0.75, 3.75, 0.075),
    "gemini/gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
    "openai/gpt-4.1": (2.0, 8.0, 0.5),
    "openai/gpt-4.1-mini": (0.4, 1.6, 0.1),
    "openai/gpt-4o": (2.5, 10.0, 1.25),
    "groq/qwen/qwen3.8-27b": (0.8, 4.0, None),
    "groq/openai/gpt-oss-120b": (0.15, 0.6, 0.075),
    "dashscope/qwen-plus": (0.4, 1.2, None),
    "dashscope/qwen-max": (1.6, 6.4, None),
    "dashscope/qwen-turbo": (0.05, 0.2, None),
}


def lookup(provider: str, model_id: str) -> tuple[float, float, float | None] | None:
    """(input, output, cache_read) USD per million tokens, or None when the model has no known price."""
    return PRICES.get(f"{provider}/{model_id}")


def cost(tokens_in: int, tokens_out: int, cache_read: int,
         price_in: float | None, price_out: float | None, price_cache: float | None) -> float:
    """USD for one request. ``tokens_in`` excludes ``cache_read``. Unknown price (None) counts as 0."""
    p_in, p_out = price_in or 0.0, price_out or 0.0
    p_cache = p_in if price_cache is None else price_cache
    return (tokens_in * p_in + cache_read * p_cache + tokens_out * p_out) / 1_000_000
