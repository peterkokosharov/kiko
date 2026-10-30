# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""kiko-lite provider adapters and the provider table.

The table is plain data (one dict per provider), not classes:

* ``base_url``  API root; for kind "openai" ``/chat/completions`` is appended, for "gemini" it replaces
  ``https://generativelanguage.googleapis.com`` in the native endpoint.
* ``kind``      "openai" (chat completions, ``providers/openai_compat.py``) or "gemini" (``providers/gemini.py``).
* ``key_names`` env / keystore names tried in order (``kikolib.keystore.get_secret``).
* ``auth``      "bearer" (Authorization: Bearer <key>) or "header:<Name>" (key in that header).
  Gemini always uses its own ``x-goog-api-key`` header.
* ``flags``     per-provider quirks: ``max_tokens_cap`` (int | None, default None = no clamp; max_tokens above
  it is lowered; no provider sets it yet, fill in real limits once verified), ``max_tokens_field`` ("max_tokens" default | "max_completion_tokens"),
  ``stream_usage`` (default True; False = do not send ``stream_options.include_usage``),
  ``mistral`` (default False; Mistral tool_choice and tool schema quirks).

Base URLs marked [memory] were written from memory and not verified against the provider docs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROVIDERS: dict[str, dict[str, Any]] = {
    # [memory] NVIDIA NIM hosted API (build.nvidia.com), OpenAI compatible
    "nvidia": {"base_url": "https://integrate.api.nvidia.com/v1", "kind": "openai",
               "key_names": ["NVIDIA_API_KEY"], "auth": "bearer", "flags": {}},
    # [memory] Groq OpenAI compatible endpoint
    "groq": {"base_url": "https://api.groq.com/openai/v1", "kind": "openai",
             "key_names": ["GROQ_API_KEY"], "auth": "bearer", "flags": {}},
    # [memory] Alibaba Model Studio (DashScope) international compatible mode; mainland China uses
    # https://dashscope.aliyuncs.com/compatible-mode/v1
    "dashscope": {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "kind": "openai",
                  "key_names": ["DASHSCOPE_API_KEY"], "auth": "bearer", "flags": {}},
    # [memory] OpenAI; newer models reject max_tokens
    "openai": {"base_url": "https://api.openai.com/v1", "kind": "openai",
               "key_names": ["OPENAI_API_KEY"], "auth": "bearer",
               "flags": {"max_tokens_field": "max_completion_tokens"}},
    # Mistral La Plateforme, OpenAI compatible. Ported from litellm/llms/mistral/chat/transformation.py
    # (_get_openai_compatible_provider_info: base https://api.mistral.ai/v1, key MISTRAL_API_KEY; max_tokens is
    # sent as "max_tokens"; stream_options is never sent, Mistral answers 422 to extra fields). The "mistral" flag
    # turns on tool_choice required/object -> "any" (transformation.py _map_tool_choice) and removal of $id/$schema
    # from tool schemas (litellm/utils.py _remove_json_schema_refs), see openai_compat.to_openai.
    "mistral": {"base_url": "https://api.mistral.ai/v1", "kind": "openai",
                "key_names": ["MISTRAL_API_KEY"], "auth": "bearer",
                "flags": {"stream_usage": False, "mistral": True}},
    # Gemini AI Studio native generateContent (see providers/gemini.py)
    "gemini": {"base_url": "https://generativelanguage.googleapis.com", "kind": "gemini",
               "key_names": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "auth": "header:x-goog-api-key", "flags": {}},
}

ALIASES: dict[str, str] = {"qwen": "dashscope", "google": "gemini"}


def lookup(name: str, table: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, Mapping[str, Any]] | None:
    """(canonical name, entry) for a provider name or alias (case-insensitive); None if unknown."""
    table = PROVIDERS if table is None else table
    key = (name or "").strip().lower()
    key = ALIASES.get(key, key) if key not in table else key
    entry = table.get(key)
    return (key, entry) if entry is not None else None
