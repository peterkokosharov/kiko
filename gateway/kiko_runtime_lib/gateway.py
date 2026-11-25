# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus 4.8
"""In-process facade over the kiko-lite gateway. Pure wiring, no gateway logic of its own."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Callable

from kiko_lite import app as lite
from kikolib import keystore, server, sse
from kiko_lite.providers import PROVIDERS as _PROVIDERS


class GatewayError(RuntimeError):
    """A non-streamed request came back with an error status; ``body`` is the gateway's error JSON."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = ""
        if isinstance(body, Mapping):
            err = body.get("error")
            message = (err or {}).get("message", "") if isinstance(err, Mapping) else str(err or "")
        super().__init__(f"gateway error {status}: {message}".rstrip(": "))


def provider_table(overrides: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """A fresh copy of the kiko-lite provider registry, optionally with providers added or replaced.

    Each provider is one unit (a dict with ``base_url`` / ``kind`` / ``key_names`` / ``auth`` / ``flags``),
    so swapping, adding or removing one is a single mapping entry. Pass the result to ``Gateway.from_config``.
    """
    table = {name: dict(entry) for name, entry in _PROVIDERS.items()}
    for name, entry in (overrides or {}).items():
        table[name] = dict(entry)
    return table


def _lines(chunks: Iterable[bytes | str]) -> Iterator[str]:
    """Split a stream of byte/str frames into text lines, carrying a partial line across frames."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        while True:
            nl = buffer.find("\n")
            if nl < 0:
                break
            yield buffer[:nl]
            buffer = buffer[nl + 1:]
    if buffer:
        yield buffer


class Gateway:
    """Drive a kiko-lite gateway in-process. Holds the same ``kiko_lite`` app that ``serve`` builds."""

    def __init__(self, app: server.App) -> None:
        self.app = app  # the kikolib server.App; kiko_lite.serve(app) would still serve it over HTTP

    @classmethod
    def from_config(
        cls,
        models: Mapping[str, str] | None = None,
        classes: Mapping[str, str] | None = None,
        *,
        providers: Mapping[str, Mapping[str, Any]] | None = None,
        secret: Callable[[str], str | None] | None = None,
        env: Mapping[str, str] | None = None,
        master_key: str | None = None,
        auth: bool = False,
        **build_kwargs: Any,
    ) -> "Gateway":
        """Build an in-process gateway.

        ``models`` maps a client model name (glob allowed) to ``"provider/upstream-model-id"``; ``classes`` maps a
        class keyword (opus/sonnet/haiku/...) the same way. ``providers`` is the provider registry (default: a copy
        of the built-in one via ``provider_table``). ``secret`` resolves a key name to its value (default: the
        existing ``kikolib.keystore.get_secret``, i.e. OS keystore / env). ``auth=False`` is in-process home mode
        (no client key); set ``auth=True`` with ``master_key`` to require one. Extra keyword arguments pass straight
        to ``kiko_lite.app.build_app`` (``http``, ``policy``, ``on_result``, ``upstream_timeout``, ...).
        """
        table = provider_table() if providers is None else {n: dict(e) for n, e in providers.items()}
        resolver = lite.SimpleResolver(aliases=dict(models or {}), classes=dict(classes or {}),
                                       env=env, providers=table)
        app = lite.build_app(resolver, master_key, providers=table,
                             secret=secret or keystore.get_secret, auth=auth, **build_kwargs)
        return cls(app)

    @property
    def gateway(self) -> Any:
        """The underlying ``kiko_lite`` ``_Gateway`` (tune policy / hooks, as the server code does)."""
        return self.app.gateway

    def _call(self, path: str, body: Mapping[str, Any],
              headers: Mapping[str, str] | None = None) -> server.JsonResponse | server.StreamResponse:
        handler, params = self.app.match("POST", path)
        pairs = [("content-type", "application/json"), *(headers or {}).items()]
        req = server.Request(method="POST", path=path, query={}, headers=server.Headers(pairs),
                             body=json.dumps(body).encode("utf-8"), params=params)
        return handler(req)

    def chat(self, body: Mapping[str, Any], *, stream: bool | None = None,
             headers: Mapping[str, str] | None = None) -> Any:
        """Send an OpenAI-style chat completion to ``POST /v1/chat/completions`` in-process.

        Non-stream: returns the normalized ``chat.completion`` dict, or raises ``GatewayError`` on an error status.
        Stream (``body["stream"]`` true or ``stream=True``): returns an iterator of parsed ``chat.completion.chunk``
        dicts (the terminal ``[DONE]`` marker is consumed, not yielded).
        """
        want_stream = bool(body.get("stream")) if stream is None else stream
        if stream is not None and bool(body.get("stream")) != want_stream:
            body = {**body, "stream": want_stream}
        resp = self._call("/v1/chat/completions", body, headers)
        if want_stream:
            return self._iter_chunks(resp)
        if isinstance(resp, server.StreamResponse):  # an error before streaming starts arrives here too
            data = b"".join(c if isinstance(c, bytes) else c.encode("utf-8") for c in resp.chunks)
            parsed = json.loads(data) if data.strip() else {}
            if resp.status >= 400:
                raise GatewayError(resp.status, parsed)
            return parsed
        if resp.status >= 400:
            raise GatewayError(resp.status, resp.data)
        return resp.data

    @staticmethod
    def _iter_chunks(resp: server.JsonResponse | server.StreamResponse) -> Iterator[dict[str, Any]]:
        if isinstance(resp, server.JsonResponse):  # the gateway answered an error without a stream
            raise GatewayError(resp.status, resp.data)
        if resp.status >= 400:
            data = b"".join(c if isinstance(c, bytes) else c.encode("utf-8") for c in resp.chunks)
            raise GatewayError(resp.status, json.loads(data) if data.strip() else {})
        for event in sse.iter_events(_lines(resp.chunks)):
            if event.data == "[DONE]":
                break
            try:
                yield json.loads(event.data)
            except json.JSONDecodeError:
                continue
