# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kiko-lite app core: Anthropic ``POST /v1/messages`` served by translating to an upstream provider.

``build_app`` returns a ``kikolib.server.App`` with:

* ``POST /v1/messages``              Claude Code's endpoint (stream and non-stream)
* ``POST /v1/messages/count_tokens`` fallback estimate (``kikolib.tokens``), no upstream call
* ``POST /v1/chat/completions``     OpenAI clients: converted to a Messages body (``chat_front``), same pipeline
* ``POST /v1/responses``            OpenAI Responses clients (codex CLI): ``responses_front``, same pipeline
* ``GET  /v1/models``              the model ids a client may ask for (OpenAI list shape)
* ``GET  /health``                   no auth

Flow: auth (master key) -> resolve model (injected ``ModelResolver``; one target, or an ordered list of
candidates of the asked class when it has ``candidates``) -> provider table entry -> API key
(``kikolib.keystore.get_secret``) -> translate -> ``kikolib.retry.send_with_retry`` -> translate back.
Failover: on an upstream 429 / 408 / 5xx / timeout / connection error the next candidate (at most
``MAX_ATTEMPTS``) is tried, never after the first upstream body byte and never for other 4xx. A failing model
cools down in memory (``Cooldowns``: Retry-After / Gemini RetryInfo, else 60 s) and is skipped meanwhile.
Retries happen only before the first upstream byte. For ``stream: true`` the Anthropic SSE response starts
at once (a ``ping``), more pings are sent while retrying or while the upstream is silent, and a failure
after that is an SSE ``error`` event. Non-stream failures are Anthropic-shaped JSON errors (429 / 529 carry
Retry-After). The client body is never forwarded as is: the translators build the upstream request from a
whitelist (so api_base, extra_headers, ... from the client are ignored). Upstream 401/403 are answered as
502 (they would look like a bad client key) with a generic message; other upstream messages are scrubbed
of the API key and truncated. API keys are never logged or raised.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

from kikolib import http as khttp
from kikolib import keystore, retry, server, sse, tokens
from kikolib.server import HTTPError, JsonResponse, Request, StreamResponse

from . import providers as ptable
from . import chat_front, responses_front, toolshim
from .providers import gemini
from .providers import openai_compat as oc

logger = logging.getLogger("kiko_lite.app")

CLASS_DEFS = (("OPUS", "opus"), ("SONNET", "sonnet"), ("HAIKU", "haiku"), ("FABLE", "fable"))  # kiko-full order
CLASS_KEYWORDS = tuple(k for _, k in CLASS_DEFS)
MAX_ATTEMPTS = 3  # candidates tried per request
DEFAULT_COOLDOWN = 60.0
PROBE_MODEL_CAP = 120.0  # a model's own timeout never lengthens a probe beyond max(probe_timeout, this)
MAX_COOLDOWN = 3600.0
OVERLOADED = frozenset({408, 425, 502, 503, 504, 529})
DEFAULT_RETRY_AFTER = "10"
KEY_CACHE_SECONDS = 300.0
# Upstream model ids end up in URLs (Gemini) and request bodies: only these characters, no '..' segment.
_MODEL_ID = re.compile(r"[A-Za-z0-9._:-]+(/[A-Za-z0-9._:-]+)*\Z")


class _Cancelled(Exception):
    """The client went away: stop retrying / reading upstream (private to this module)."""


def valid_model_id(model_id: str, kind: str = "openai") -> bool:
    """Safe upstream model id? Gemini ids are a single path segment (no '/')."""
    if not isinstance(model_id, str) or not _MODEL_ID.match(model_id):
        return False
    segments = model_id.split("/")
    if any(s in (".", "..") for s in segments):
        return False
    return kind != "gemini" or len(segments) == 1

Front = Union[chat_front.ChatStream, responses_front.ResponsesStream]  # client-protocol stream translator
OPENAI_PATHS = frozenset({"/v1/chat/completions", "/v1/responses", "/v1/models"})

ModelResolver = Callable[[str], "tuple[str, str] | None"]
"""model name from the client -> (provider name, upstream model id), or None when unknown."""


# ---- model resolution ---------------------------------------------------------------------------

def split_target(target: str, table: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, str] | None:
    """``"provider/model-id"`` -> (canonical provider, model id); None if the provider is unknown."""
    prov, sep, model = (target or "").partition("/")
    hit = ptable.lookup(prov, table) if sep and model else None
    return (hit[0], model) if hit else None


class SimpleResolver:
    """In-memory resolver, same order as kiko-full ``_resolve_model_alias``.

    1. ``aliases``: glob pattern -> target (first match wins);
    2. ``classes``: class keyword (found in the lower-cased name) or lower-cased class name -> target;
    3. env ``ANTHROPIC_DEFAULT_<CLASS NAME>_MODEL`` for the same classes;
    the classes are ``class_defs`` (name, keyword) pairs; ``CLASS_DEFS`` (OPUS, SONNET, HAIKU, FABLE) is the
    no-DB default only (a DB supplies its own list, see ``Database.classes``).
    4. the name itself.
    A target is ``"provider/model-id"`` (e.g. ``"nvidia/meta/llama-3.3-70b-instruct"``).
    """

    def __init__(self, aliases: Mapping[str, str] | None = None, classes: Mapping[str, str] | None = None,
                 env: Mapping[str, str] | None = None,
                 providers: Mapping[str, Mapping[str, Any]] | None = None,
                 class_defs: Sequence[tuple[str, str]] | None = None) -> None:
        # (name, keyword) pairs; no-DB default
        self.class_defs = tuple(class_defs) if class_defs is not None else CLASS_DEFS
        self.aliases = dict(aliases or {})
        self.classes = {k.lower(): v for k, v in (classes or {}).items()}
        self.env = os.environ if env is None else env
        self.providers = providers

    def target_for(self, model: str) -> str:
        for pattern, target in self.aliases.items():
            if fnmatch.fnmatchcase(model, pattern):
                return target
        ml = model.lower()
        for name, kw in self.class_defs:
            if kw in ml:
                hit = self.classes.get(kw) or self.classes.get(name.lower())
                if hit:
                    return hit
                break  # class matched but no target: fall through to the env var
        for name, kw in self.class_defs:
            if kw in ml and self.env.get(f"ANTHROPIC_DEFAULT_{name}_MODEL"):
                return self.env[f"ANTHROPIC_DEFAULT_{name}_MODEL"]
        return model

    def __call__(self, model: str) -> tuple[str, str] | None:
        return split_target(self.target_for(model), self.providers)

    def list_models(self) -> list[str]:
        """Ids a client may ask for (``GET /v1/models``): the alias patterns without wildcards and the class
        names / keywords, each only when it resolves to a known provider."""
        names: list[str] = [a for a in self.aliases if not any(c in a for c in "*?[")]
        for name, kw in self.class_defs:
            names += [kw, name.lower()]
        seen: dict[str, None] = {}
        for n in names:
            if n not in seen and self(n) is not None:
                seen[n] = None
        return list(seen)


# ---- per-request record -------------------------------------------------------------------------

@dataclass
class RequestResult:
    """What the billing writer needs; passed to the ``on_result`` hook after each request (also after a request
    that failed before any upstream call, and after a health probe: ``route`` tells them apart)."""

    request_id: str
    client_model: str
    provider: str = ""
    upstream_model: str = ""
    stream: bool = False
    attempts: int = 0
    status: int = 0  # HTTP status sent (a stream that ends in an error event is still 200)
    stop_reason: str | None = None
    error: str | None = None  # Anthropic error type, never a secret
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    latency: float = 0.0
    route: str = "messages"  # billing route: "messages" (live request) or "probe" (background health check)
    request: Any = None  # the client's JSON body (never headers), for the text log
    response: dict | None = None  # the final message (a stream assembled) or the error body, for the text log
    path: str = ""  # the request path (/v1/messages, /v1/chat/completions, /v1/responses), for the text log
    upstream_status: int = 0  # a stream that failed after the 200 headers: the status the failure maps to


def error_body(typ: str, message: str) -> dict:
    return {"type": "error", "error": {"type": typ, "message": message}}


class MessageAssembler:
    """Builds the final Anthropic message out of the stream events sent to the client (for the text log)."""

    def __init__(self) -> None:
        self.message: dict[str, Any] = {"type": "message", "role": "assistant", "content": []}
        self._blocks: dict[int, dict[str, Any]] = {}
        self._json: dict[int, list[str]] = {}

    @property
    def started(self) -> bool:
        return bool(self._blocks)

    def feed(self, name: str, ev: dict) -> None:
        if name == "message_start" and isinstance(ev.get("message"), dict):
            m = ev["message"]
            self.message.update({k: m[k] for k in ("id", "model", "role", "usage") if k in m})
        elif name == "content_block_start" and isinstance(ev.get("content_block"), dict):
            self._blocks[ev.get("index", len(self._blocks))] = dict(ev["content_block"])
        elif name == "content_block_delta" and isinstance(ev.get("delta"), dict):
            i, d = ev.get("index", 0), ev["delta"]
            blk = self._blocks.setdefault(i, {"type": "text", "text": ""})
            if "text" in d:
                blk["text"] = blk.get("text", "") + str(d["text"])
            elif "thinking" in d:
                blk["thinking"] = blk.get("thinking", "") + str(d["thinking"])
            elif "partial_json" in d:
                self._json.setdefault(i, []).append(str(d["partial_json"]))
        elif name == "message_delta":
            self.message["stop_reason"] = (ev.get("delta") or {}).get("stop_reason")
            self.message["usage"] = dict(ev.get("usage") or {})

    def result(self) -> dict[str, Any]:
        out = dict(self.message)
        content = []
        for i in sorted(self._blocks):
            blk = dict(self._blocks[i])
            if i in self._json:
                raw = "".join(self._json[i])
                try:
                    blk["input"] = json.loads(raw) if raw else {}
                except ValueError:
                    blk["input"] = raw
            content.append(blk)
        out["content"] = content
        return out


@dataclass
class _Plan:
    provider: str
    upstream_model: str
    url: str
    headers: dict[str, str] = field(repr=False)
    data: bytes = field(repr=False)
    key: str = field(repr=False)
    stream: bool
    new_stream: Callable[[], Any]
    parse: Callable[[dict], dict]
    timeout: float | None = None  # per-model upstream timeout from the models table; None = the gateway's
    shim: frozenset[str] | None = None  # toolshim on and the request has tools: the tool names; else None


# ---- error mapping ------------------------------------------------------------------------------

def _scrub(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "***")
    return text[:2000]


def _upstream_message(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    try:
        d = json.loads(text)
    except (ValueError, RecursionError):  # a deeply nested body: treat as non-JSON
        return text.strip()[:500]
    if isinstance(d, list) and d:
        d = d[0]
    if isinstance(d, dict):
        err = d.get("error", d)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or text[:500])
        if isinstance(err, str):
            return err
        return str(d.get("message") or d.get("detail") or text[:500])
    return text.strip()[:500]


def map_upstream_error(status: int, raw: bytes, retry_after: str | None, provider: str,
                       key: str) -> tuple[int, str, str, dict[str, str]]:
    """Upstream HTTP error -> (status, Anthropic error type, message, extra headers)."""
    status = upstream_status(status, raw)
    msg = _scrub(_upstream_message(raw), key) or f"upstream returned HTTP {status}"
    ra = {"Retry-After": retry_after.strip() if retry_after and "\n" not in retry_after else DEFAULT_RETRY_AFTER}
    if status == 429:
        return 429, "rate_limit_error", msg, ra
    if status in OVERLOADED:
        return 529, "overloaded_error", msg, ra
    if status in (401, 403):
        return 502, "api_error", f"provider {provider} rejected the configured API key", {}
    if status == 404:
        return 404, "not_found_error", msg, {}
    if status == 413:
        return 413, "request_too_large", msg, {}
    if 400 <= status < 500 and status != 409:
        return 400, "invalid_request_error", msg, {}
    return 502, "api_error", msg, {}


def _exc_error(exc: Exception) -> tuple[int, str, str, dict[str, str]]:
    ra = {"Retry-After": DEFAULT_RETRY_AFTER}
    if isinstance(exc, khttp.HttpTimeout):
        return 529, "overloaded_error", "upstream timed out", ra
    if isinstance(exc, (khttp.ConnectError, khttp.ConnectionLost)):
        return 529, "overloaded_error", "upstream is unreachable", ra
    return 502, "api_error", f"upstream request failed: {exc}", {}  # HttpError messages hold host:port only


def _stream_exc_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, (oc.OpenAIStreamError, gemini.GeminiStreamError)):
        code = str(getattr(exc, "code", "") or "").lower()
        kind = str(getattr(exc, "kind", "") or getattr(exc, "status", "") or "").lower()
        if code == "429" or "rate" in kind or "resource_exhausted" in kind:
            return "rate_limit_error", str(exc)
        if "overload" in kind or code in ("503", "529") or kind == "unavailable":
            return "overloaded_error", str(exc)
        return "api_error", str(exc)
    return "api_error", "upstream stream interrupted"


def _error_event(err_type: str, message: str) -> bytes:
    return sse.encode("error", {"type": "error", "error": {"type": err_type, "message": message}})


# ---- failover helpers ---------------------------------------------------------------------------

def failover_status(status: int) -> bool:
    """Upstream statuses that make the next candidate worth trying (other 4xx are the caller's fault)."""
    return status in (408, 425, 429) or status >= 500


def live_failover_status(status: int) -> bool:
    """Live requests also fail over on a broken model (401 / 403 / 404: bad key, model gone)."""
    return failover_status(status) or status in (401, 403, 404)


def _bad_key_400(raw: bytes) -> bool:
    """Gemini answers an invalid API key with HTTP 400 INVALID_ARGUMENT; the error details carry the reason
    ``API_KEY_INVALID`` (google.rpc.ErrorInfo). Any other 400 is the caller's fault and stays a 400."""
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(d, list) and d:
            d = d[0]
        return any(isinstance(det, dict) and det.get("reason") == "API_KEY_INVALID"
                   for det in d["error"]["details"])
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        return False


def upstream_status(status: int, raw: bytes) -> int:
    """The status to act on: a Gemini 400 API_KEY_INVALID counts as 401 (rejected key), everything else as is."""
    return 401 if status == 400 and _bad_key_400(raw) else status


def _failure_rank(status: int) -> int:
    """Which failure to show the client when every candidate failed: a transient one (429 / 5xx, and a connection
    error or timeout, which count as status 0) says more about the request than a broken key or a missing model,
    so it wins; among equals the later one."""
    return 0 if status in (401, 403, 404) else 1


def _retry_info(raw: bytes) -> float | None:
    """Gemini ``error.details[].retryDelay`` ("37s") from an error body, else None."""
    try:
        d = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(d, list) and d:
            d = d[0]
        for det in d["error"]["details"]:
            if str(det.get("@type", "")).endswith("RetryInfo"):
                return float(str(det["retryDelay"]).rstrip("s"))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        pass
    return None


def cooldown_seconds(retry_after: str | None, raw: bytes = b"") -> float:
    """How long to skip a model after a failure: Retry-After, else Gemini RetryInfo, else 60 s (1 s .. 1 h)."""
    wait = retry.parse_retry_after(retry_after) if retry_after else None
    if wait is None:
        wait = _retry_info(raw)
    if wait is None:
        wait = DEFAULT_COOLDOWN
    return min(max(wait, 1.0), MAX_COOLDOWN)


class Cooldowns:
    """In-memory "skip this model until ..." table; ``clock`` is injectable (tests)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._until: dict[tuple[str, str], float] = {}
        self._ok: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def mark(self, key: tuple[str, str], seconds: float) -> None:
        with self._lock:
            self._until[key] = self.clock() + seconds

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._until.pop(key, None)

    def mark_ok(self, key: tuple[str, str]) -> None:
        """A live request just succeeded (passive health: the prober need not spend a call on it)."""
        with self._lock:
            self._ok[key] = self.clock()

    def since_ok(self, key: tuple[str, str]) -> float | None:
        """Seconds since the last live success of the model, None if none since start."""
        with self._lock:
            t = self._ok.get(key)
        return None if t is None else self.clock() - t

    def remaining(self, key: tuple[str, str]) -> float:
        with self._lock:
            until = self._until.get(key)
            if until is None:
                return 0.0
            left = until - self.clock()
            if left <= 0:
                del self._until[key]
                return 0.0
            return left


# ---- request shape ------------------------------------------------------------------------------

_TRANSLATE_ERRORS = (TypeError, AttributeError, KeyError, ValueError, IndexError, RecursionError)


def _bad(msg: str) -> HTTPError:
    return HTTPError(400, "invalid_request_error", msg)


def _check_blocks(blocks: list, path: str) -> None:
    """Every client value the translators dereference (block dicts, text, ids, source, tool_result content)."""
    for j, b in enumerate(blocks):
        where = f"{path}.{j}"
        if not isinstance(b, dict):
            raise _bad(f"{where}: Input should be a valid dictionary")
        kind = b.get("type")
        if kind == "text" and not isinstance(b.get("text", ""), str):
            raise _bad(f"{where}.text: Input should be a valid string")
        if kind in ("image", "document"):
            src = b.get("source")
            if not isinstance(src, dict) or not isinstance(src.get("type"), str):
                raise _bad(f"{where}.source: Input should be an object with a string type")
        if kind == "tool_use":
            if not (isinstance(b.get("id", ""), str) and isinstance(b.get("name", ""), str)):
                raise _bad(f"{where}: tool_use id and name must be strings")
            if not isinstance(b.get("input"), (dict, type(None))):
                raise _bad(f"{where}.input: Input should be an object")
        if kind == "tool_result":
            if not isinstance(b.get("tool_use_id", ""), str):
                raise _bad(f"{where}.tool_use_id: Input should be a valid string")
            inner = b.get("content")
            if not isinstance(inner, (str, list, type(None))):
                raise _bad(f"{where}.content: Input should be a string or a list")
            if isinstance(inner, list):
                _check_blocks(inner, f"{where}.content")


def validate_request(body: dict) -> None:
    """Check the Anthropic request shape once, before any translator, so a malformed client body is a 400 for
    every provider kind (never a 500). Only what the translators rely on is checked: unknown block types and
    extra fields (thinking, cache_control, images, documents, metadata ...) pass through. A body nested so
    deeply that checking it overflows the stack is a 400 too."""
    try:
        _validate(body)
    except RecursionError:
        raise _bad("request body is nested too deeply") from None


def _validate(body: dict) -> None:
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        raise _bad("messages: Input should be a valid list")
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise _bad(f"messages.{i}: Input should be a valid dictionary")
        if m.get("role") not in ("user", "assistant"):
            raise _bad(f"messages.{i}.role: Input should be 'user' or 'assistant'")
        content = m.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise _bad(f"messages.{i}.content: Input should be a string or a list of blocks")
        _check_blocks(content, f"messages.{i}.content")
    system = body.get("system")
    if system is not None and not isinstance(system, str):
        if not isinstance(system, list):
            raise _bad("system: Input should be a string or a list of text blocks")
        _check_blocks(system, "system")
    stops = body.get("stop_sequences")
    if stops is not None and not (isinstance(stops, list) and all(isinstance(x, str) for x in stops)):
        raise _bad("stop_sequences: Input should be a list of strings")
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, dict) and isinstance(t.get("name"), str)
                                                  for t in tools):
            raise _bad("tools: Input should be a list of objects with a string name")
    if "max_tokens" in body:
        mt = body["max_tokens"]
        if isinstance(mt, bool) or not isinstance(mt, int) or mt <= 0:
            raise _bad("max_tokens: Input should be a positive integer")


# ---- the gateway --------------------------------------------------------------------------------

class _Gateway:
    def __init__(self, resolver: ModelResolver, master_key: str | None, providers: Mapping[str, Mapping[str, Any]],
                 http: khttp.HttpBackend | None, policy: retry.RetryPolicy | None,
                 secret: Callable[[str], str | None], on_result: Callable[[RequestResult], None] | None,
                 upstream_timeout: float, ping_interval: float,
                 gate: Callable[[], str | None] | None = None, auth: bool = True,
                 on_health: Callable[[str, bool], None] | None = None, probe_timeout: float = 20.0) -> None:
        self.gate = gate
        self.on_health = on_health  # (provider table name, worked?) after a live request or a probe
        self.cooldowns = Cooldowns()
        self.auth = auth
        self.resolver, self.master = resolver, (master_key or "").encode("utf-8")
        self.providers = providers
        self._http = http
        self.policy = policy or retry.RetryPolicy()
        self._secret = secret
        self.on_result = on_result or (lambda r: None)
        self.timeout = upstream_timeout
        self.probe_timeout = probe_timeout
        self.ping_interval = ping_interval
        self._keys: dict[str, tuple[str | None, float, Any]] = {}
        self._lock = threading.Lock()

    @property
    def http(self) -> khttp.HttpBackend:
        if self._http is None:
            self._http = khttp.client()
        return self._http

    # -- auth / keys --
    def before(self, req: Request) -> None:
        """The app-wide auth hook. The OpenAI routes authenticate inside their own handler (``_openai``), so a
        401 there is OpenAI-shaped; every other route is checked here."""
        if req.path in OPENAI_PATHS:
            return
        self.authenticate(req)

    def authenticate(self, req: Request) -> None:
        if not self.auth:  # explicit home mode (build_app(auth=False)): no client key
            return
        if req.method == "GET" and req.path in ("/health", "/ui", "/ui/"):  # /ui is the static page, no data
            return
        supplied = req.headers.get("x-api-key") or ""
        if not supplied:
            auth = req.headers.get("authorization") or ""
            if auth[:7].lower() == "bearer ":
                supplied = auth[7:].strip()
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), self.master):
            raise HTTPError(401, "authentication_error", "invalid x-api-key")

    def _secret_version(self) -> Any:
        """The secret source may carry a ``version()`` (the .env reloader): a changed version drops cached keys."""
        fn = getattr(self._secret, "version", None)
        try:
            return fn() if callable(fn) else None
        except Exception:
            return None

    def _api_key(self, entry: Mapping[str, Any]) -> str | None:
        version = self._secret_version()
        for name in entry.get("key_names") or []:
            now = time.monotonic()
            with self._lock:
                value, ts, ver = self._keys.get(name, (None, 0.0, None))
            if value is None or now - ts > KEY_CACHE_SECONDS or ver != version:
                try:
                    value = self._secret(name)
                except Exception:
                    value = None
                with self._lock:
                    self._keys[name] = (value, now, version)
            if value:
                return value
        return None

    # -- planning --
    def _plan(self, body: Any) -> _Plan:
        return self._plans(body)[0]

    def _plans(self, body: Any) -> list[_Plan]:
        """Ordered plans (best first, at most ``MAX_ATTEMPTS``) for the asked model; the first plan's error is
        raised when none can be built. Cooling candidates are skipped (all cooling: the one that recovers first)."""
        if not isinstance(body, dict):
            raise HTTPError(400, "invalid_request_error", "request body must be a JSON object")
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise HTTPError(400, "invalid_request_error", "model: Field required")
        if not isinstance(body.get("messages"), list):
            raise HTTPError(400, "invalid_request_error", "messages: Field required")
        validate_request(body)
        many = getattr(self.resolver, "candidates", None)
        targets = []
        for t in (many(model) if many else [self.resolver(model)]):
            hit = ptable.lookup(t[0], self.providers) if t else None
            targets.append((hit[0], t[1]) if hit else t)  # canonical provider name = the cooldown key
        targets = [t for t in targets if t]
        if not targets:
            raise HTTPError(404, "not_found_error", f"model: {model[:100]}")
        live = [t for t in targets if not self.cooldowns.remaining(t)]
        if not live:
            live = [min(targets, key=self.cooldowns.remaining)]
        plans: list[_Plan] = []
        first_error: HTTPError | None = None
        for target in live:
            if len(plans) >= MAX_ATTEMPTS:
                break
            try:
                plans.append(self._build(body, model, target))
            except HTTPError as e:
                first_error = first_error or e
        if not plans:
            assert first_error is not None
            raise first_error
        self._check_gate()
        return plans

    def _build(self, body: dict, model: str, target: tuple[str, str]) -> _Plan:
        hit = ptable.lookup(target[0], self.providers)
        if hit is None:
            raise HTTPError(404, "not_found_error", f"model: {model[:100]}")
        pname, entry = hit
        upstream = target[1]
        if not valid_model_id(upstream, str(entry.get("kind", "openai"))):
            raise HTTPError(400, "invalid_request_error", "model: invalid upstream model id")
        flags = entry.get("flags") or {}
        cap = flags.get("max_tokens_cap")
        if isinstance(cap, int) and cap > 0 and isinstance(body.get("max_tokens"), int) and body["max_tokens"] > cap:
            body = {**body, "max_tokens": cap}
        key = self._api_key(entry)
        if not key:
            raise HTTPError(503, "api_error", f"no API key configured for provider {pname}")
        stream = bool(body.get("stream"))
        stops = body.get("stop_sequences")
        try:  # safety net around the translation only (never around the upstream call)
            if entry.get("kind") == "gemini":
                payload = gemini.to_gemini({**body, "model": upstream})
                url = gemini.endpoint(upstream, stream).replace(gemini.BASE, str(entry["base_url"]).rstrip("/"), 1)
                headers = gemini.headers(key)
                new_stream: Callable[[], Any] = lambda: gemini.StreamTranslator(model)
                parse: Callable[[dict], dict] = lambda d: gemini.from_gemini(d, model)
            else:
                payload = oc.to_openai(body, upstream, max_tokens_field=flags.get("max_tokens_field", "max_tokens"),
                                       stream_usage=flags.get("stream_usage", True),
                                       mistral=bool(flags.get("mistral", False)),
                                       thinking=flags.get("thinking"))
                url = oc.endpoint(str(entry["base_url"]))
                headers = oc.headers(key, str(entry.get("auth", "bearer")))
                new_stream = lambda: oc.StreamTranslator(model, stops)
                parse = lambda d: oc.from_openai(d, model, stops)
        except _TRANSLATE_ERRORS:
            logger.warning("request translation failed (%s); the client gets a 400", pname, exc_info=True)
            raise _bad("malformed request body") from None
        tc = body.get("tool_choice")
        no_tools = isinstance(tc, dict) and tc.get("type") == "none"  # the client forbids tool calls: no shim
        return _Plan(pname, upstream, url, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                     key, stream, new_stream, parse, self._model_timeout(flags),
                     (toolshim.tool_names(body) or None) if flags.get("toolshim") and not no_tools else None)

    @staticmethod
    def _model_timeout(flags: Mapping[str, Any]) -> float | None:
        t = flags.get("timeout")
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else None

    def gate_message(self) -> str | None:
        """Run the injected pre-upstream gate (monthly cap): a message means "do not call upstream"; no gate or a
        failing gate (fails open, logged) gives None. Live requests and paid health probes use the same gate."""
        if self.gate is None:
            return None
        try:
            return self.gate() or None
        except Exception:
            logger.error("gate hook failed (request allowed)", exc_info=True)
            return None

    def _check_gate(self) -> None:
        msg = self.gate_message()
        if msg:
            raise HTTPError(429, "rate_limit_error", msg)

    def _sender(self, plan: _Plan, calls: list[int],
                cancel: threading.Event | None = None) -> Callable[[], khttp.Response]:
        # Always stream=True: http.request() then returns as soon as the response head arrived, so the body is
        # read by the caller AFTER send_with_retry and a body read failure is never retried (that would send
        # the same, possibly already billed, request again). Known tradeoff kept on purpose: a failure while
        # waiting for the response head (ConnectionLost / HttpTimeout after the request was sent) IS retried,
        # which can double-bill on providers that bill an abandoned request; the owner wants NVIDIA retries.
        def send() -> khttp.Response:
            if cancel is not None and cancel.is_set():
                raise _Cancelled
            calls[0] += 1
            return self.http.request("POST", plan.url, plan.headers, plan.data, timeout=plan.timeout or self.timeout,
                                     stream=True)
        return send

    # -- failover bookkeeping --
    def _cool(self, plan: _Plan, retry_after: str | None = None, raw: bytes = b"", status: int = 0) -> None:
        """The model failed (429 / 5xx / timeout ...): skip it for a while and tell the health hook. A 429 only
        cools down in memory (rate limited is not broken: the DB status stays)."""
        secs = cooldown_seconds(retry_after, raw)
        self.cooldowns.mark((plan.provider, plan.upstream_model), secs)
        logger.warning("model %s/%s failed, cooling down %.0fs", plan.provider, plan.upstream_model, secs)
        if status != 429:
            self._health(plan.provider, False)

    def _worked(self, plan: _Plan) -> None:
        self.cooldowns.clear((plan.provider, plan.upstream_model))
        self.cooldowns.mark_ok((plan.provider, plan.upstream_model))
        self._health(plan.provider, True)

    def _health(self, pname: str, ok: bool) -> None:
        if self.on_health is None:
            return
        try:
            self.on_health(pname, ok)
        except Exception:
            logger.error("health hook failed", exc_info=True)

    def _policy_for(self, last: bool) -> retry.RetryPolicy:
        """Full retry policy for the last candidate only; earlier ones fail over at once."""
        return self.policy if last else dataclasses.replace(self.policy, attempts=1)

    def _probe_timeout(self, model_timeout: float | None) -> float:
        if not model_timeout:
            return self.probe_timeout
        return min(model_timeout, max(self.probe_timeout, PROBE_MODEL_CAP))

    def probe(self, pname: str, upstream: str, timeout: float | None = None) -> bool | None:
        """One minimal (max_tokens 1) request to a model: True if it answered 200, False if it is broken (401 /
        403 / 404, other failover statuses, a connection error), None for 429 (cooldown only) and any other answer
        (e.g. 400 from the minimal body: says nothing about the model). No retry. The gate (monthly cap) is NOT
        run here: the caller (``HealthProber``) checks ``gate_message`` before it probes a paid model.

        Timeout: ``timeout`` if given, else the model's own timeout (models.timeout) capped at
        max(``probe_timeout``, 120 s), else ``probe_timeout`` (``[proxy] probe_timeout``, default 20 s): the prober
        is sequential, so a 24 h model timeout must not stall it.

        Every probe that reached the upstream is reported to ``on_result`` with route "probe" (tokens and cost
        when the answer carried a usage, else 0), so billing / ``costs`` show it and it counts toward the cap.

        A failure starts the model's cooldown (from Retry-After / RetryInfo) like a live failure does. Never
        raises and never logs a key."""
        try:
            plan = self._build({"model": upstream, "max_tokens": 1, "stream": False,
                                "messages": [{"role": "user", "content": "ping"}]}, upstream, (pname, upstream))
        except HTTPError:
            return False  # no key / bad id: cannot serve
        t0 = time.monotonic()
        try:
            resp = self.http.request("POST", plan.url, plan.headers, plan.data,
                                     timeout=timeout or self._probe_timeout(plan.timeout), stream=True)
        except khttp.HttpError as e:
            self.cooldowns.mark((pname, upstream), DEFAULT_COOLDOWN)
            if not isinstance(e, khttp.ConnectError):  # the request may have been sent: show it
                self._probe_row(plan, 0, b"", t0, _exc_error(e)[1])
            return False
        with resp:
            try:
                raw = resp.read()
            except khttp.HttpError:
                raw = b""
            eff = upstream_status(resp.status, raw)  # a Gemini 400 API_KEY_INVALID is a 401
            if resp.status == 200:
                self.cooldowns.clear((pname, upstream))
                verdict: bool | None = True
            elif failover_status(resp.status):
                self.cooldowns.mark((pname, upstream), cooldown_seconds(resp.headers.get("Retry-After"), raw))
                verdict = None if resp.status == 429 else False  # rate limited is not broken (as for a live 429)
            else:
                verdict = False if eff in (401, 403, 404) else None
            status = resp.status
        self._probe_row(plan, status, raw, t0)
        return verdict

    def _probe_row(self, plan: _Plan, status: int, raw: bytes, t0: float, error: str | None = None) -> None:
        """Report one probe that reached the upstream to ``on_result`` (billing route "probe")."""
        res = RequestResult(uuid.uuid4().hex[:16], plan.upstream_model, plan.provider, plan.upstream_model, False,
                            1, status, latency=time.monotonic() - t0, route="probe")
        if status == 200:
            try:
                res.usage = dict(plan.parse(json.loads(raw.decode("utf-8")))["usage"])
            except Exception:  # no usable usage in the answer: tokens and cost stay 0
                pass
        elif status:
            res.error = map_upstream_error(status, raw, None, plan.provider, plan.key)[1]
        else:
            res.error = error
        logger.info("probe %s %s/%s status=%d %.2fs", res.request_id, res.provider, res.upstream_model, status,
                    res.latency)
        self._emit(res)

    # -- handlers --
    def messages(self, req: Request) -> JsonResponse | StreamResponse:
        return self._messages(req.json(), None, req.path)

    def _openai(self, fn: Callable[[Request], Any], errors: Callable[..., dict] = chat_front.error_body
                ) -> Callable[[Request], Any]:
        """Wrap an OpenAI route: authenticate (fail closed), and answer errors in the OpenAI shape."""
        def handler(req: Request) -> Any:
            try:
                self.authenticate(req)
                return fn(req)
            except HTTPError as e:
                return JsonResponse(errors(e.error_type, e.message), e.status, e.headers)
        return handler

    def chat_completions(self, req: Request) -> JsonResponse | StreamResponse:
        try:
            body, opts = chat_front.convert_request(req.json())
        except chat_front.ChatRequestError as e:
            raise HTTPError(400, "invalid_request_error", e.message) from None
        return self._messages(body, chat_front.ChatStream(opts["model"], opts["include_usage"]), req.path)

    def responses(self, req: Request) -> JsonResponse | StreamResponse:
        try:
            body, opts = responses_front.convert_request(req.json())
        except chat_front.ChatRequestError as e:
            raise HTTPError(400, "invalid_request_error", e.message) from None
        return self._messages(body, responses_front.ResponsesStream(opts["model"]), req.path)

    def models(self, req: Request) -> JsonResponse:
        listing = getattr(self.resolver, "list_models", None)
        ids = list(listing()) if callable(listing) else []
        return JsonResponse({"object": "list", "data": [
            {"id": i, "object": "model", "created": 0, "owned_by": "kiko"} for i in ids]})

    def _messages(self, body: Any, front: Front | None, path: str = "") -> JsonResponse | StreamResponse:
        try:
            plans = self._plans(body)
        except HTTPError as e:  # failed before any upstream call: still one billing row (tokens 0, cost 0)
            self._rejected(body, e)
            raise
        plan = plans[0]
        res = RequestResult(uuid.uuid4().hex[:16], body["model"], plan.provider, plan.upstream_model, plan.stream,
                            request=body, path=path)
        t0 = time.monotonic()
        calls = [0]
        if plan.stream:
            return StreamResponse(self._stream(plans, body, res, calls, t0, front))
        try:
            out = self._nonstream(plans, body, res, calls)
            if front is not None:
                conv = responses_front if isinstance(front, responses_front.ResponsesStream) else chat_front
                return JsonResponse(conv.convert_response(out.data, body["model"]))
            return out
        except HTTPError as e:
            res.status, res.error = e.status, e.error_type
            res.response = error_body(e.error_type, e.message)
            raise
        except Exception as e:  # a bug: the client gets a 500, the text log an error body (no message: no leak)
            res.status, res.error = 500, "api_error"
            res.response = error_body("api_error", f"internal error ({type(e).__name__})")
            raise
        finally:
            self._finish(res, calls, t0)

    def _rejected(self, body: Any, e: HTTPError) -> None:
        """One result for a request refused before the upstream (400 / 404 / 503 no key / 429 cap): the model name
        as the client sent it (cut to 100 characters), an error text, no body, no tokens."""
        model = body.get("model") if isinstance(body, dict) else None
        res = RequestResult(uuid.uuid4().hex[:16], model[:100] if isinstance(model, str) else "",
                            stream=isinstance(body, dict) and bool(body.get("stream")), status=e.status,
                            error=f"{e.error_type}: {e.message}"[:200], request=body,
                            response=error_body(e.error_type, e.message))
        logger.info("request %s %s refused before upstream status=%d", res.request_id, res.client_model, res.status)
        self._emit(res)

    def _emit(self, res: RequestResult) -> None:
        try:
            self.on_result(res)
        except Exception:
            logger.error("on_result hook failed", exc_info=True)

    def _finish(self, res: RequestResult, calls: list[int], t0: float) -> None:
        res.attempts, res.latency = calls[0], time.monotonic() - t0
        extra = f" path={res.path}" if res.path else ""
        if res.upstream_status:  # a stream that failed after the 200 headers: the real outcome (no message, no key)
            extra += f" upstream={res.upstream_status} error={res.error} outcome=error"
        logger.info("request %s %s -> %s/%s attempts=%d status=%d %.2fs%s", res.request_id, res.client_model,
                    res.provider, res.upstream_model, res.attempts, res.status, res.latency, extra)
        self._emit(res)

    def _nonstream(self, plans: list[_Plan], body: dict, res: RequestResult, calls: list[int]) -> JsonResponse:
        # most meaningful failure so far: (rank, plan, error); error is (status, raw, retry-after) or an HttpError
        # (connection error / timeout: ranks like a transient status)
        worst: tuple[int, _Plan, Any] | None = None
        for i, plan in enumerate(plans):
            last = i == len(plans) - 1
            res.provider, res.upstream_model = plan.provider, plan.upstream_model  # the model actually used
            try:
                resp, _ = retry.send_with_retry(self._sender(plan, calls), self._policy_for(last))
            except khttp.HttpError as e:
                if not isinstance(e, (khttp.ConnectError, khttp.HttpTimeout, khttp.ConnectionLost)):
                    st, typ, msg, hdrs = _exc_error(e)
                    raise HTTPError(st, typ, msg, hdrs) from None
                self._cool(plan)
                if worst is None or _failure_rank(0) >= worst[0]:
                    worst = (_failure_rank(0), plan, e)
                if not last:
                    continue
                self._raise_worst(worst, res)
            with resp:
                try:
                    raw = resp.read()  # after the retry loop: a failure here is not retried
                except khttp.HttpError as e:
                    st, typ, msg, hdrs = _exc_error(e)
                    raise HTTPError(st, typ, msg, hdrs) from None
                status = upstream_status(resp.status, raw)
                if status != 200 and live_failover_status(status):
                    ra = resp.headers.get("Retry-After")
                    self._cool(plan, ra, raw, status)
                    if worst is None or _failure_rank(status) >= worst[0]:
                        worst = (_failure_rank(status), plan, (status, raw, ra))
                    if not last:
                        continue
                    self._raise_worst(worst, res)
                if resp.status != 200:
                    st, typ, msg, hdrs = map_upstream_error(resp.status, raw, resp.headers.get("Retry-After"),
                                                            plan.provider, plan.key)
                    raise HTTPError(st, typ, msg, hdrs)
                self._worked(plan)
                return self._parsed(plan, raw, body, res)
        raise HTTPError(502, "api_error", "no upstream model available")  # unreachable: plans is not empty

    @staticmethod
    def _raise_worst(worst: tuple[int, _Plan, Any], res: RequestResult) -> None:
        _, wplan, err = worst
        res.provider, res.upstream_model = wplan.provider, wplan.upstream_model
        if isinstance(err, khttp.HttpError):
            st, typ, msg, hdrs = _exc_error(err)
        else:
            st, typ, msg, hdrs = map_upstream_error(err[0], err[1], err[2], wplan.provider, wplan.key)
        raise HTTPError(st, typ, msg, hdrs)

    def _parsed(self, plan: _Plan, raw: bytes, body: dict, res: RequestResult) -> JsonResponse:
        try:
            message = plan.parse(json.loads(raw.decode("utf-8")))
        except (oc.OpenAIStreamError, gemini.GeminiStreamError) as e:
            typ, msg = _stream_exc_error(e)
            raise HTTPError(502, typ, _scrub(msg, plan.key)) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPError(502, "api_error", "unexpected response from the upstream provider") from None
        if plan.shim:
            n = toolshim.apply_message(message, plan.shim)
            if n:
                logger.info("toolshim %s %s: %d text tool call(s) made real", res.request_id, plan.upstream_model, n)
        usage = message["usage"]
        if not usage.get("input_tokens") and not usage.get("output_tokens"):
            usage["input_tokens"] = tokens.estimate_messages(body)
            usage["output_tokens"] = tokens.estimate_messages({"messages": [{"content": message["content"]}]})
        res.status, res.stop_reason, res.usage = 200, message["stop_reason"], dict(usage)
        res.response = message
        return JsonResponse(message)

    def _stream(self, plans: list[_Plan], body: dict, res: RequestResult, calls: list[int],
                t0: float, front: Front | None = None) -> Iterator[bytes]:
        q: queue.Queue[tuple[Any, ...]] = queue.Queue()
        cancel = threading.Event()
        holder: list[khttp.Response] = []  # the open upstream response, so the generator can close it
        cur = [plans[0]]  # the candidate in use; the worker moves it on a failover (before any body byte)
        plan = plans[0]

        def on_wait(piece: float, attempt: int, why: str) -> None:
            if cancel.is_set():
                raise _Cancelled
            q.put(("ping",))

        def worker() -> None:
            resp = None
            worst: tuple[int, _Plan, tuple] | None = None  # most meaningful failure: (rank, plan, queue item)
            try:
                for i, cand in enumerate(plans):
                    last = i == len(plans) - 1
                    cur[0] = cand
                    res.provider, res.upstream_model = cand.provider, cand.upstream_model
                    try:
                        resp, _ = retry.send_with_retry(self._sender(cand, calls, cancel), self._policy_for(last),
                                                        on_wait=on_wait)
                    except (khttp.ConnectError, khttp.HttpTimeout, khttp.ConnectionLost) as e:
                        self._cool(cand)
                        if worst is None or _failure_rank(0) >= worst[0]:
                            worst = (_failure_rank(0), cand, ("exc", e))
                        if not last:
                            continue  # failover: nothing was sent to the client but pings
                        _, cur[0], item = worst
                        res.provider, res.upstream_model = cur[0].provider, cur[0].upstream_model
                        q.put(item)
                        return
                    holder.append(resp)
                    if cancel.is_set():
                        return
                    if resp.status == 200:
                        break
                    raw = resp.read()
                    ra = resp.headers.get("Retry-After")
                    status = upstream_status(resp.status, raw)
                    if live_failover_status(status):
                        self._cool(cand, ra, raw, status)
                        if worst is None or _failure_rank(status) >= worst[0]:
                            worst = (_failure_rank(status), cand, ("status", status, raw, ra))
                        if not last:
                            holder.remove(resp)
                            resp.close()
                            resp = None
                            continue
                        _, cur[0], item = worst
                        res.provider, res.upstream_model = cur[0].provider, cur[0].upstream_model
                        q.put(item)
                        return
                    q.put(("status", resp.status, raw, ra))
                    return
                self._worked(cur[0])
                q.put(("open",))
                for ev in sse.iter_events(resp.iter_lines()):
                    if cancel.is_set():
                        return
                    q.put(("data", ev.data))
                q.put(("end",))
            except _Cancelled:
                return
            except Exception as e:  # HttpError and anything unexpected; the generator turns it into an event
                if not cancel.is_set():
                    q.put(("exc", e))
            finally:
                if resp is not None:
                    resp.close()

        threading.Thread(target=worker, name="kiko-upstream", daemon=True).start()
        tr = None
        out_text: list[str] = []
        asm = MessageAssembler()
        ping = sse.ping if front is None else front.ping

        def err(typ: str, msg: str) -> bytes:
            return _error_event(typ, msg) if front is None else front.error(typ, msg)

        def failed(typ: str, msg: str, st: int = 502) -> None:
            """Remember the error for the text log together with the answer assembled so far."""
            res.error, res.upstream_status = typ, st
            res.response = error_body(typ, msg)
            if asm.started:
                res.response = {**asm.result(), "error": res.response["error"]}
        completed = False
        try:
            yield ping()  # the client gets a response at once, whatever the upstream does
            last = time.monotonic()  # last write to the client
            while True:
                try:
                    item = q.get(timeout=self.ping_interval)
                except queue.Empty:
                    yield ping()
                    last = time.monotonic()
                    continue
                kind = item[0]
                plan = cur[0]
                if kind == "ping":
                    yield ping()
                    last = time.monotonic()
                elif kind == "open":
                    tr = plan.new_stream()
                    if plan.shim:  # buffer the text of this response; the converted answer goes out at the end
                        tr = toolshim.ShimStream(tr, plan.shim)
                elif kind == "status":
                    _, typ, msg, _h = map_upstream_error(item[1], item[2], item[3], plan.provider, plan.key)
                    failed(typ, msg, item[1])
                    yield err(typ, msg)
                    return
                elif kind == "exc":
                    exc = item[1]
                    if isinstance(exc, khttp.HttpError):
                        st, typ, msg = _exc_error(exc)[:3]
                    else:
                        logger.error("upstream worker failed", exc_info=exc)
                        st, typ, msg = 500, "api_error", "internal error while reading the upstream stream"
                    failed(typ, msg, st)
                    yield err(typ, msg)
                    return
                else:  # "data" or "end"
                    events: list[tuple[str, dict]] = []
                    done = kind == "end"
                    if kind == "data":
                        data = item[1].strip()
                        if data == "[DONE]":
                            done = True
                        else:
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                chunk = None
                            if isinstance(chunk, dict):
                                try:
                                    events = tr.feed(chunk)
                                except (oc.OpenAIStreamError, gemini.GeminiStreamError) as e:
                                    typ, msg = _stream_exc_error(e)
                                    failed(typ, _scrub(msg, plan.key), 502)
                                    yield err(typ, _scrub(msg, plan.key))
                                    return
                    if kind == "end" and not tr.complete:
                        # EOF without [DONE] and without a finish reason: the upstream cut the stream
                        failed("api_error", "upstream stream ended unexpectedly", 502)
                        yield err("api_error", "upstream stream ended unexpectedly")
                        return
                    if done:
                        events += tr.finish()
                        if getattr(tr, "converted", 0):
                            logger.info("toolshim %s %s: %d text tool call(s) made real", res.request_id,
                                        plan.upstream_model, tr.converted)
                    for name, ev in events:
                        if name == "content_block_delta":
                            d = ev["delta"]
                            out_text.append(str(d.get("text") or d.get("thinking") or d.get("partial_json") or ""))
                        elif name == "message_delta":
                            self._patch_usage(ev, body, out_text)
                            res.stop_reason, res.usage = ev["delta"].get("stop_reason"), dict(ev["usage"])
                        asm.feed(name, ev)
                        if front is None:
                            yield sse.encode(name, ev)
                        else:
                            yield from front.event(name, ev)
                        last = time.monotonic()
                    if not events and not done and time.monotonic() - last >= self.ping_interval:
                        yield ping()  # buffering (toolshim): frequent chunks must not starve the keepalive
                        last = time.monotonic()
                    if done:
                        completed = True
                        if front is not None:
                            yield from front.close()
                        return
        finally:
            cancel.set()
            # Client gone or stream failed: close() here could block behind a worker stuck in readline (buffer
            # lock) for up to upstream_timeout; abort() only shuts the socket down, which wakes that worker, and
            # the worker closes the response itself. A normally completed stream is left to the worker, which
            # returns the connection to the pool; abort() is a no-op on an already finished response anyway.
            if not completed:
                for r in list(holder):
                    r.abort()
            res.status = 200
            if res.response is None:  # finished, or the client went away part-way: what was sent so far
                res.response = asm.result()
            self._finish(res, calls, t0)

    @staticmethod
    def _patch_usage(ev: dict, body: dict, out_text: list[str]) -> None:
        u = ev.setdefault("usage", {})
        if not u.get("input_tokens") and not u.get("output_tokens"):
            u["input_tokens"] = tokens.estimate_messages(body)
            u["output_tokens"] = tokens.estimate_text("".join(out_text))

    def count_tokens(self, req: Request) -> JsonResponse:
        body = req.json()
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise HTTPError(400, "invalid_request_error", "messages: Field required")
        validate_request(body)
        try:
            n = tokens.estimate_messages(body)
        except _TRANSLATE_ERRORS:
            logger.warning("token estimate failed; the client gets a 400", exc_info=True)
            raise _bad("malformed request body") from None
        return JsonResponse({"input_tokens": max(n, 1)})


def build_app(resolver: ModelResolver, master_key: str | None, *, providers: Mapping[str, Mapping[str, Any]] | None = None,
              http: khttp.HttpBackend | None = None, policy: retry.RetryPolicy | None = None,
              secret: Callable[[str], str | None] | None = None,
              on_result: Callable[[RequestResult], None] | None = None,
              upstream_timeout: float = 300.0, ping_interval: float = retry.PING_INTERVAL,
              gate: Callable[[], str | None] | None = None, auth: bool = True,
              on_health: Callable[[str, bool], None] | None = None, probe_timeout: float = 20.0) -> server.App:
    """Build the kiko-lite ``App``. An empty ``master_key`` is refused (fail closed) unless ``auth=False``.

    ``auth=False`` is the explicit home mode: every request is accepted without a client key (``master_key`` is
    then ignored); one WARNING is logged.

    ``gate`` runs after the model and key are resolved and before the upstream call: it returns None to let the
    request through or a message to answer 429 ``rate_limit_error`` (the monthly spending cap); if it raises, the
    request is let through and the error is logged.

    ``on_health(provider name, worked)`` is called after a live request finished (worked = got a 200, not worked =
    429 / 5xx / timeout / connection error); the DB wiring uses it to keep ``models.status`` true.

    ``upstream_timeout`` (default 300 s) is how long a socket operation may wait: for the response head and then
    between two reads of the body (idle / first-byte, never a total limit); a model's own ``models.timeout`` wins.
    ``probe_timeout`` (default 20 s) is the same for a health probe, unless the model has its own timeout.
    """
    if auth and not master_key:
        raise ValueError("master_key must not be empty")
    if not auth:
        logger.warning("no client auth: home mode (anyone who can reach this port can use the gateway)")
    gw = _Gateway(resolver, master_key, ptable.PROVIDERS if providers is None else providers, http, policy,
                  secret or keystore.get_secret, on_result, upstream_timeout, ping_interval, gate, auth, on_health,
                  probe_timeout)
    app = server.App()
    app.browser_guard = True  # no browser may drive the gateway (Origin / Host / Content-Type), auth or not
    app.gateway = gw  # type: ignore[attr-defined]  # tests and the later admin code tune policy / hooks here
    app.before(gw.before)

    @app.route("GET", "/health")
    def health(req: Request) -> JsonResponse:
        return JsonResponse({"status": "ok"})

    app.route("POST", "/v1/messages")(gw.messages)
    app.route("POST", "/v1/messages/count_tokens")(gw.count_tokens)
    app.route("POST", "/v1/chat/completions")(gw._openai(gw.chat_completions))
    app.route("POST", "/v1/responses")(gw._openai(gw.responses, responses_front.error_body))
    app.route("GET", "/v1/models")(gw._openai(gw.models))
    return app
