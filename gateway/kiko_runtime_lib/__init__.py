# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus 4.8
"""kiko_runtime_lib: a thin, in-process facade over the kiko-lite gateway.

It lets a caller send an OpenAI-style chat completion (stream or not, with tools) and get a
normalized result back, WITHOUT running the ``kiko_lite serve`` HTTP server. The provider is
chosen by the resolver config (openai-compatible passthrough or gemini translation) and the key
is resolved through the existing keystore. Everything here is wiring: it builds the same
``kiko_lite`` app ``serve`` builds and calls its route handlers in-process.

    from kiko_runtime_lib import Gateway

    gw = Gateway.from_config(models={"gpt-4.1": "openai/gpt-4.1", "flash": "gemini/gemini-3.8-flash"})
    answer = gw.chat({"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]})
    for chunk in gw.chat({"model": "flash", "messages": [{"role": "user", "content": "hi"}], "stream": True}):
        print(chunk["choices"][0]["delta"].get("content", ""), end="")
"""

from __future__ import annotations

from .gateway import Gateway, GatewayError, provider_table

__all__ = ["Gateway", "GatewayError", "provider_table"]
__version__ = "0.1.0"
