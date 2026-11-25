<!-- SPDX-License-Identifier: MIT -->
<!-- Author: Peter Kokosharov <peter.kokosharov@gmail.com> -->
<!-- AI models: claude-opus-4-8 -->

# kiko_runtime_lib

A thin, importable facade over the **kiko-lite** gateway. It gives one clean in-process entry point for
sending OpenAI-style chat completions, **without** starting the `kiko_lite serve` HTTP server. The underlying
gateway is unchanged: this package only builds the same app `serve` builds and calls its route handlers
directly. Standard library only.

* Provider chosen by config: openai-compatible passthrough or Gemini translation (the gateway decides from the
  resolved `provider/model` target).
* Key resolved through the existing keystore (`kikolib.keystore.get_secret`) by default.
* Provider adapters stay a swappable registry: each provider is one dict unit (`provider_table`).

## Public API

* `Gateway.from_config(models=None, classes=None, *, providers=None, secret=None, env=None, master_key=None, auth=False, **build_kwargs) -> Gateway`
  Build an in-process gateway. `models` maps a client model name (glob allowed) to `"provider/upstream-id"`;
  `classes` maps a class keyword (opus/sonnet/haiku/fable) the same way. `secret` defaults to the keystore;
  `auth=False` is in-process home mode (no client key). Extra kwargs pass to `kiko_lite.app.build_app`
  (`http`, `policy`, `on_result`, `upstream_timeout`, ...).
* `Gateway.chat(body, *, stream=None, headers=None) -> dict | Iterator[dict]`
  Send an OpenAI chat completion. Non-stream returns the `chat.completion` dict (raises `GatewayError` on an
  error status); stream returns an iterator of `chat.completion.chunk` dicts (the `[DONE]` marker is consumed).
* `Gateway.app` — the underlying `kikolib.server.App` (still serveable with `kikolib.server.serve`).
* `Gateway.gateway` — the underlying `kiko_lite` `_Gateway` (tune policy / hooks).
* `provider_table(overrides=None) -> dict` — a fresh copy of the provider registry, with providers added/replaced.
* `GatewayError(status, body)` — non-2xx non-stream result; `body` is the gateway's error JSON.

## Usage

```python
from kiko_runtime_lib import Gateway
gw = Gateway.from_config(models={"gpt-4.1": "openai/gpt-4.1", "flash": "gemini/gemini-3.8-flash"})
answer = gw.chat({"model": "gpt-4.1", "messages": [{"role": "user", "content": "hi"}]})
print(answer["choices"][0]["message"]["content"])
for chunk in gw.chat({"model": "flash", "messages": [{"role": "user", "content": "hi"}], "stream": True}):
    print(chunk["choices"][0]["delta"].get("content", ""), end="")
```

## Tests

`gateway/tests/kiko/test_kiko_runtime_lib.py` (runs with the normal kiko runner,
`python tools/run_tests.py`). It drives a fake upstream in-process and checks non-stream text, streaming, tools,
Gemini translation and error surfacing.
