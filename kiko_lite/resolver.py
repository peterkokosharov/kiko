# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""DB-backed model resolution, billing writer and monthly cap for kiko-lite.

* ``map_row``      kiko-full ``models`` row (LiteLLM style provider string) -> kiko-lite provider table entry
* ``Catalog``      short-TTL cached snapshot of models / aliases / settings (CLI edits reach a running server)
* ``DBResolver``   ``ModelResolver``: DB alias globs -> class routing -> ``ANTHROPIC_DEFAULT_*_MODEL`` -> direct name
* ``DBProviders``  provider table for ``build_app``: the static ``PROVIDERS`` plus one entry per DB row, so the
                   API key NAME of each row (``api_key_env``) is what the gateway looks up
* ``BillingWriter`` the ``on_result`` hook: one ``billing`` row per request (failures too, cost 0)
* ``cap_gate``     the ``gate`` hook: monthly spending cap
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from kikolib import config as kconfig
from . import app as lite
from . import prices
from . import providers as ptable
from .db import DEFAULT_CLASSES, Database, check_timeout, now_iso
from .health import HealthProber

logger = logging.getLogger("kiko_lite.resolver")

TTL = 2.0
_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# Start of well-known API key formats: a "name" that begins like this is a key VALUE.
_KEY_PREFIXES = ("gsk_", "AIza", "sk-", "sk_", "nvapi-", "xai-", "pplx-", "hf_", "ghp_", "AKIA")
HIDDEN = "<invalid name, value hidden>"
# provider-string prefixes (LiteLLM style, plus kiko-lite's own names) -> kiko-lite provider
# Derived from providers.PROVIDERS + ALIASES: there is no second hand-kept list.
_PREFIX = ptable.prefix_table()
# "anthropic/" and "openrouter/" stay unsupported on purpose (Anthropic upstream is not used, OpenRouter is banned):
# like any unknown prefix they are refused unless the row has its own https api_base (custom endpoint).
_LOCAL = ("localhost", "127.0.0.1", "::1")


class MappingError(ValueError):
    """A models row that cannot be turned into a provider entry (the text never holds a secret)."""


def key_name_problem(name: str | None) -> str | None:
    """Why ``name`` is not a key NAME: None = it is one, "value" = it looks like a key VALUE, "name" = just
    not a valid name (empty, spaces, ...).

    Deliberately conservative (a real name that fails is merely refused, a real key that passes is a leak).
    A name is letters, digits and ``_``, not starting with a digit (upper or lower case: ``gemini_api_key`` is
    fine). It looks like a value when it starts like a known key format (``_KEY_PREFIXES``: gsk_, AIza, sk-,
    nvapi-, ...) AND is at least 20 characters long AND the part after the prefix looks like a token: it has a
    digit, or mixed case, or no ``_`` at all (so ``hf_token``, ``sk_api_key``, ``gsk_name``, ``ghp_x`` are
    names, while ``gsk_`` + 52 letters/digits is a value), or is a long (>= 32) string with a digit and almost no ``_`` (fewer than one per 8
    characters; real names such as ``KIKO_MY_ALIAS_API_KEY`` have many), or is a longer (>= 24) string that is
    not even name-shaped.
    """
    if not name:
        return "name"
    if name.startswith(_KEY_PREFIXES) and len(name) >= 20:
        tail = name[next(len(p) for p in _KEY_PREFIXES if name.startswith(p)):]
        if any(c.isdigit() for c in tail) or (tail != tail.lower() and tail != tail.upper()) or "_" not in tail:
            return "value"
    if len(name) >= 32 and any(c.isdigit() for c in name) and name.count("_") * 8 < len(name):
        return "value"
    if _KEY_NAME.match(name) is None:
        return "value" if len(name) >= 24 else "name"
    return None


def valid_key_name(name: str | None) -> bool:
    return key_name_problem(name) is None


def check_api_base(api_base: str) -> None:
    """ValueError unless ``api_base`` is a plain https URL (http only to localhost): no credentials, query or
    fragment. Every API base that is stored (provider default or model override) goes through here, because
    kiko-full (LiteLLM) honours the ``models.api_base`` column for every provider."""
    api_base = (api_base or "").strip()
    if any(c.isspace() or ord(c) < 32 for c in api_base):
        raise MappingError("api_base must be a plain URL (no spaces)")
    _generic_entry(api_base)  # https / localhost rule, no credentials (MappingError is a ValueError)
    u = _split(api_base)
    if u.query or u.fragment:
        raise MappingError("api_base must be a plain URL without query or fragment")


def check_provider(api_base: str, api_key_env: str) -> None:
    """ValueError unless ``api_base`` is a plain URL (``check_api_base``) and ``api_key_env`` a key NAME."""
    if not valid_key_name(api_key_env):
        raise ValueError("API key name must be an environment variable name (letters, digits, underscore), "
                         "not the key itself")
    check_api_base(api_base)


def custom_provider_names(db: Database) -> frozenset[str]:
    """Names in the ``providers`` table that kiko-lite does not know itself (served as custom endpoints)."""
    return frozenset(p["name"] for p in db.list_providers() if p["name"] not in _PREFIX)


def show_name(name: str | None) -> str:
    """The name for printing; anything that is not a valid name is never shown."""
    return name if name and valid_key_name(name) else HIDDEN


@dataclass(frozen=True)
class Mapped:
    provider: str  # canonical kiko-lite provider (nvidia, groq, dashscope, openai, gemini, or "custom")
    model_id: str
    entry: dict[str, Any] = field(repr=False)


def _split(api_base: str):
    try:
        u = urlsplit(api_base)
        u.port  # noqa: B018  (raises ValueError for a bad port)
        return u
    except ValueError as e:  # e.g. "https://[::1": one bad row must not break the snapshot or add-model
        raise MappingError("api_base is not a valid URL") from e


# A bare model id (no "provider/") is OpenAI only when it looks like an OpenAI model, as LiteLLM decides
# (litellm_core_utils/get_llm_provider_logic.py: open_ai_chat_completion_models, "ft:gpt-*", gpt-image).
_OPENAI_BARE = ("gpt-", "chatgpt-", "ft:gpt", "text-embedding-", "text-davinci", "text-moderation", "omni-moderation",
                "davinci", "babbage", "dall-e", "whisper-", "tts-", "codex-")


def _looks_openai(model: str) -> bool:
    m = model.lower()
    return m.startswith(_OPENAI_BARE) or re.match(r"o\d", m) is not None


def _host_provider(api_base: str) -> str | None:
    host = (_split(api_base).hostname or "").lower()
    if host == "integrate.api.nvidia.com":
        return "nvidia"
    if host == "api.groq.com":
        return "groq"
    if host.startswith("dashscope"):
        return "dashscope"
    if host == "api.openai.com":
        return "openai"
    return None


def _generic_entry(api_base: str) -> dict[str, Any]:
    u = _split(api_base)
    host = (u.hostname or "").lower()
    if u.username or u.password or not host:
        raise MappingError("api_base must be a plain URL without credentials")
    if u.scheme != "https" and not (u.scheme == "http" and host in _LOCAL):
        raise MappingError("api_base must be https (http only to localhost)")
    return {"base_url": api_base.rstrip("/"), "kind": "openai", "key_names": [], "auth": "bearer", "flags": {}}


def map_row(row: Mapping[str, Any], custom: Collection[str] = ()) -> Mapped:
    """Row (provider, api_base, api_key_env) -> ``Mapped``; raises ``MappingError`` when it cannot be served.

    ``custom``: names of the providers table that kiko-lite does not know itself. A row ``acme/m1`` whose prefix is
    one of them (and that has an api_base) is a custom OpenAI-compatible endpoint that is sent the bare id ``m1``.
    Any other unknown prefix keeps the old rule (the whole text is the model id)."""
    provider = str(row.get("provider") or "").strip()
    api_base = str(row.get("api_base") or "").strip()
    prefix, sep, rest = provider.partition("/")
    unknown = False
    if sep and prefix.lower() in _PREFIX:
        canonical, model_id = _PREFIX[prefix.lower()], rest
    elif sep and prefix.lower() in custom and api_base:
        canonical, model_id, unknown = "openai", rest, True
    elif sep:
        # Unknown LiteLLM prefix (anthropic/, openrouter/, ...): NEVER OpenAI, or its key would go to
        # api.openai.com. Only a row with its own api_base can be served (custom OpenAI-compatible endpoint).
        if not api_base:
            raise MappingError(f"provider prefix '{prefix}/' is not supported and the row has no api_base; "
                               "refused (it would be sent to OpenAI)")
        canonical, model_id, unknown = "openai", provider, True
    else:
        canonical, model_id = "openai", provider  # LiteLLM: a bare id such as "gpt-4o" is OpenAI
        if not api_base:
            keyname = str(row.get("api_key_env") or "")
            if provider and not _looks_openai(provider):
                raise MappingError(f"model id '{provider}' is not a known OpenAI model; use a provider prefix "
                                   "(e.g. mistral/...) or give an api_base; refused (it would be sent to OpenAI)")
            if keyname and "openai" not in keyname.lower():
                raise MappingError("a bare OpenAI model id may only use an OpenAI key name (e.g. OPENAI_API_KEY); "
                                   "refused (that key would be sent to OpenAI)")
    if not model_id:
        raise MappingError("provider has no model id")
    if canonical == "openai" and (api_base or unknown):
        generic = _generic_entry(api_base)  # validates the URL (https, or http to localhost)
        host_hit = _host_provider(api_base)
        if host_hit is None:
            entry, canonical = generic, "custom"
        else:  # known host: that provider's quirks, but the row's own base URL (e.g. dashscope mainland)
            canonical, entry = host_hit, {**ptable.PROVIDERS[host_hit], "base_url": generic["base_url"]}
    else:
        entry = dict(ptable.PROVIDERS[canonical])
    if canonical == "openai" and _split(entry["base_url"]).hostname != "api.openai.com":
        raise MappingError("internal: openai provider with a non-OpenAI base URL")  # key-leak guard
    name = str(row.get("api_key_env") or "")
    entry["key_names"] = [name] if valid_key_name(name) else []
    thinking = str(row.get("thinking") or "")
    if thinking in ("on", "off", "off-prompt") and entry.get("kind", "openai") == "openai":
        entry["flags"] = {**(entry.get("flags") or {}), "thinking": thinking}  # copy: never edit the shared table
    if row.get("toolshim") == "on":  # tool calls written as text become real ones (kiko_lite.toolshim)
        entry["flags"] = {**(entry.get("flags") or {}), "toolshim": True}
    try:
        timeout = check_timeout(row.get("timeout"))  # per-model upstream timeout (seconds), None = the global one
    except ValueError:
        timeout = None  # a hand-edited bad value in the DB: the global timeout
    if timeout is not None:
        entry["flags"] = {**(entry.get("flags") or {}), "timeout": timeout}  # every kind of upstream
    return Mapped(canonical, model_id, entry)


@dataclass(frozen=True)
class ModelInfo:
    row: dict[str, Any]
    pname: str  # key in the provider table handed to the gateway
    mapped: Mapped | None
    error: str | None


@dataclass
class Snapshot:
    aliases: list[tuple[str, str]]
    settings: dict[str, str]
    models: list[ModelInfo]
    by_alias: dict[str, ModelInfo]
    by_pname: dict[str, ModelInfo]
    classes: tuple[tuple[str, str], ...] = tuple((n, k) for n, k, _, _ in DEFAULT_CLASSES)  # (name, keyword)

    def best(self, cls: str) -> ModelInfo | None:
        """Best enabled, mappable model of a class: status ok first, then unknown, by priority then id."""
        for status in ("ok", "unknown"):
            hits = [m for m in self.models if m.mapped and m.row["enabled"] and m.row["model_class"] == cls
                    and m.row["status"] == status]
            if hits:
                return min(hits, key=lambda m: (m.row["priority"], m.row["id"]))
        return None

    def ranked(self, cls: str) -> list[ModelInfo]:
        """All enabled, mappable models of a class, best first: ok, unknown, then error; priority, id inside."""
        rank = {"ok": 0, "unknown": 1}
        hits = [m for m in self.models if m.mapped and m.row["enabled"] and m.row["model_class"] == cls]
        return sorted(hits, key=lambda m: (rank.get(m.row["status"], 2), m.row["priority"], m.row["id"]))


class Catalog:
    """Snapshot of the DB, rebuilt at most every ``ttl`` seconds (a DB error keeps the last good snapshot)."""

    def __init__(self, db: Database, ttl: float = TTL, clock: Callable[[], float] = time.monotonic) -> None:
        self.db, self.ttl, self._clock = db, ttl, clock
        self._snap: Snapshot | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._expires = 0.0

    def snapshot(self) -> Snapshot:
        with self._lock:
            if self._snap is not None and self._clock() < self._expires:
                return self._snap
            try:
                self._snap = self._load()
            except Exception:
                if self._snap is None:
                    raise
                logger.error("DB read failed, using the previous snapshot", exc_info=True)
            self._expires = self._clock() + self.ttl
            return self._snap

    def _load(self) -> Snapshot:
        infos = []
        seen: dict[str, str] = {}
        custom = custom_provider_names(self.db)
        for row in self.db.models():
            pname = f"db/{row['alias'].strip().lower()}"  # the provider table lower-cases names
            try:
                if pname in seen:
                    raise MappingError(f"alias differs only in case from '{seen[pname]}'")
                seen[pname] = row["alias"]
                mapped, err = map_row(row, custom), None
            except MappingError as e:
                mapped, err = None, str(e)
            infos.append(ModelInfo(row, pname, mapped, err))
        by_pname: dict[str, ModelInfo] = {}
        for i in infos:  # first row wins, in the same order as the collision check above
            by_pname.setdefault(i.pname, i)
        classes = tuple((c["name"], c["keyword"]) for c in self.db.classes())
        return Snapshot(self.db.aliases(), kconfig.overlay_settings(self.db.settings()), infos, {i.row["alias"]: i for i in infos}, by_pname,
                        classes)


class DBProviders(Mapping[str, Mapping[str, Any]]):
    """Provider table: static ``PROVIDERS`` plus a ``db/<alias>`` entry per mappable DB row."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def __getitem__(self, key: str) -> Mapping[str, Any]:
        info = self.catalog.snapshot().by_pname.get(key)
        if info is not None and info.mapped is not None:
            return info.mapped.entry
        return ptable.PROVIDERS[key]

    def __iter__(self) -> Iterator[str]:
        snap = self.catalog.snapshot()
        yield from ptable.PROVIDERS
        yield from (i.pname for i in snap.models if i.mapped)

    def __len__(self) -> int:
        return sum(1 for _ in self)


class DBResolver:
    """``ModelResolver`` over the DB; same order as kiko-full ``_resolve_model_alias`` (and ``SimpleResolver``)."""

    def __init__(self, catalog: Catalog, env: Mapping[str, str] | None = None) -> None:
        self.catalog = catalog
        self.env = os.environ if env is None else env

    @staticmethod
    def _named(snap: Snapshot, name: str) -> tuple[str, str] | None:
        """An enabled DB model alias, else the direct ``provider/model-id`` form of the static table."""
        info = snap.by_alias.get(name)
        if info is not None:
            return (info.pname, info.mapped.model_id) if info.mapped and info.row["enabled"] else None
        return lite.split_target(name)

    def __call__(self, model: str) -> tuple[str, str] | None:
        snap = self.catalog.snapshot()
        for glob, target in snap.aliases:
            if fnmatch.fnmatchcase(model, glob):
                return self._named(snap, target)
        ml = model.lower()
        for name, kw in snap.classes:
            if kw in ml:
                best = snap.best(name)
                if best is not None and best.mapped is not None:
                    return best.pname, best.mapped.model_id
                break  # class matched but no usable model: fall through to the env var
        for name, kw in snap.classes:
            if kw in ml and self.env.get(f"ANTHROPIC_DEFAULT_{name}_MODEL"):
                return self._named(snap, self.env[f"ANTHROPIC_DEFAULT_{name}_MODEL"])
        return self._named(snap, model)

    def list_models(self) -> list[str]:
        """Ids a client may ask for (``GET /v1/models``): enabled, mappable DB aliases, alias globs without
        wildcards and the class keywords, each only when it resolves (so every listed id works)."""
        snap = self.catalog.snapshot()
        names = [m.row["alias"] for m in snap.models if m.mapped and m.row["enabled"]]
        names += [g for g, _ in snap.aliases if not any(c in g for c in "*?[")]
        names += [kw for _, kw in snap.classes]
        seen: dict[str, None] = {}
        for n in names:
            if n not in seen and self(n) is not None:
                seen[n] = None
        return list(seen)

    def candidates(self, model: str) -> list[tuple[str, str]]:
        """Ordered targets for the gateway's failover: for a class name every enabled model of the class (ok,
        unknown, then error; priority inside), else the single ``__call__`` target (aliases, env, direct)."""
        snap = self.catalog.snapshot()
        if not any(fnmatch.fnmatchcase(model, glob) for glob, _ in snap.aliases):
            ml = model.lower()
            for name, kw in snap.classes:
                if kw in ml:
                    rows = [m for m in snap.ranked(name) if m.mapped]
                    ranked = [(m.pname, m.mapped.model_id) for m in rows]
                    if ranked:
                        if all(m.row["status"] == "error" for m in rows):
                            one = self(model)  # env / direct name: may work while the class models are down
                            if one and one not in ranked:
                                ranked.insert(0, one)  # before the error rows
                        return ranked
                    break
        one = self(model)
        return [one] if one else []


def status_hook(db: Database, catalog: Catalog) -> Callable[[str, bool], None]:
    """``on_health`` for the gateway and the prober: write ``models.status`` (ok / error) when it changed."""
    def hook(pname: str, ok: bool) -> None:
        info = catalog.snapshot().by_pname.get(pname)
        if info is None:
            return  # a static provider (no row)
        want = "ok" if ok else "error"
        if info.row["status"] != want and db.set_status(info.row["alias"], want):
            catalog.invalidate()
    return hook


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


class BillingWriter:
    """``on_result`` hook: one ``billing`` row per request (route "messages"), per request refused before the
    upstream (tokens 0, cost 0) and per health probe that reached the upstream (route "probe").
    Failed requests are recorded with the tokens the provider reported (none: cost 0)."""

    def __init__(self, db: Database, catalog: Catalog) -> None:
        self.db, self.catalog = db, catalog

    def price(self, snap: Snapshot, provider: str, upstream: str) -> tuple[float | None, float | None, float | None]:
        info = snap.by_pname.get(provider)
        if info is not None:
            r = info.row
            if r.get("price_in_per_mtok") is not None or r.get("price_out_per_mtok") is not None:
                return r.get("price_in_per_mtok"), r.get("price_out_per_mtok"), r.get("price_cache_read_per_mtok")
            if info.mapped is not None:
                hit = prices.lookup(info.mapped.provider, info.mapped.model_id)
                return hit if hit else (None, None, None)
        hit = prices.lookup(provider, upstream)
        return hit if hit else (None, None, None)

    def __call__(self, res: lite.RequestResult) -> None:
        snap = self.catalog.snapshot()
        self.store_text(res)
        if _truthy(snap.settings.get("disable_billing")):
            return
        usage = res.usage or {}
        cache = int(usage.get("cache_read_input_tokens") or 0)
        t_in, t_out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        info = snap.by_pname.get(res.provider)
        canonical = info.mapped.provider if info is not None and info.mapped else res.provider
        # Tokens the provider reported are billed even when the request ended in an error (zero tokens: 0).
        p_in, p_out, p_cache = self.price(snap, res.provider, res.upstream_model)
        cost = prices.cost(t_in, t_out, cache, p_in, p_out, p_cache)
        self.db.add_billing(
            res.upstream_model or res.client_model, t_in + cache, t_out, cost,
            ts=now_iso(), route=res.route, attempts=res.attempts, latency_ms=int(res.latency * 1000),
            upstream_model=f"{canonical}/{res.upstream_model}" if res.upstream_model else None, error=res.error,
            request_id=res.request_id if res.route == "messages" else None)

    def store_text(self, res: lite.RequestResult) -> None:
        """The Text log: one ``responses`` row per /v1/messages exchange (``id`` = ``billing.request_id``), the
        client's JSON body and the final message or error body. ``[logs] store_text`` = false stores nothing.
        A failure is one warning, never an error for the client."""
        if res.route != "messages" or res.request is None or res.response is None:
            return
        try:
            if not kconfig.value("logs.store_text"):
                return
            self.db.add_response(res.request_id, json.dumps(res.request, ensure_ascii=False, default=str),
                                 json.dumps(res.response, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning("could not store the request text: %s", type(e).__name__)


def _billing_only(res: lite.RequestResult) -> lite.RequestResult:
    """A copy without the request and response bodies: ``store_text`` skips it, the billing row is unchanged."""
    return dataclasses.replace(res, request=None, response=None)


def _approx_bytes(res: lite.RequestResult) -> int:
    """Rough memory a queued result holds: the text (str) and bytes in the request and response bodies (the only
    big parts). Walks at most a few thousand nodes, so it stays cheap even for a huge body."""
    total, budget = 256, 5000
    stack: list[Any] = [res.request, res.response]
    while stack and budget > 0:
        budget -= 1
        x = stack.pop()
        if isinstance(x, (str, bytes, bytearray)):
            total += len(x)
        elif isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x)
    return total


class AsyncWriter:
    """``on_result`` hook that keeps the request thread off the database: the result goes into a bounded queue and ONE
    background thread runs ``writer(res)`` (the billing row and the text row, JSON serialisation included).

    * BILLING IS NEVER DROPPED (the monthly cap gate reads ``billing``). Only the text can be dropped:
      - byte bound (``max_bytes``, default 32 MiB, approximate bytes of the queued request and response texts; an
        empty queue always takes one result, however large): a billing-only copy (request/response None, so
        ``store_text`` skips it) is queued instead; the text is dropped and counted in ``dropped``;
      - count bound (``maxsize``) / queue full: the billing-only copy is written INLINE on the request thread
        (a single small INSERT), text dropped, one warning per minute. This is the only case where a request
        thread touches the database while the writer runs, and it is the price of never losing a billing row;
    * not started (``start`` not called, e.g. in tests) or stopping / stopped: ``writer`` runs inline, as before;
    * every ``writer`` call from the thread, and every inline call when not started / stopping / stopped, is
      serialised by one lock, so such an inline write never runs next to the writer thread, even when ``stop``
      timed out and the old thread is still busy (it then waits for it: only at shutdown). The queue-full inline
      billing write does NOT take that lock (it must not wait behind a stuck writer; the database serialises its
      own writes);
    * ``stop(timeout)`` lets the thread drain what is queued, for at most ``timeout`` seconds in total (the
      sentinel wait and the join share one deadline), then gives up. It does not hold the main lock while it
      waits, so ``__call__`` never blocks on it; results arriving meanwhile are written inline. If the sentinel
      cannot be queued in time (queue full, writer stuck) the thread is told to stop by an event instead and
      exits as soon as it is idle;
    * lag: a row appears a moment after the answer, so the monthly cap gate may miss the spend of the results still
      queued (normally a few, written within milliseconds; at most the queue bound when the database is slow)."""

    def __init__(self, writer: Callable[[lite.RequestResult], None], maxsize: int = 1000,
                 max_bytes: int = 32 * 1024 * 1024) -> None:
        self.writer = writer
        self.queue: queue.Queue[Any] = queue.Queue(maxsize)
        self.max_bytes = max_bytes
        self.thread: threading.Thread | None = None
        self.dropped = 0  # results whose text was dropped (billing was kept)
        self._pending = 0  # approximate bytes in the queue
        self._lock = threading.Lock()  # start / stop / the thread switch (never held while waiting)
        self._plock = threading.Lock()  # the byte count (the writer thread takes only this one, so stop cannot stall it)
        self._wlock = threading.Lock()  # serialises every writer(res) call
        self._stopping: threading.Event | None = None  # set by stop for the running thread
        self._last_warn = 0.0

    def start(self) -> None:
        with self._lock:
            if self.thread is None:
                self._stopping = threading.Event()
                self.thread = threading.Thread(target=self._run, args=(self._stopping,), name="kiko-log-writer",
                                               daemon=True)
                self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            t, ev = self.thread, self._stopping
            if t is None or ev is None:
                return
            # From here on __call__ writes inline (serialised with the writer by _wlock) and ``start`` may make a
            # new thread; nothing more is queued behind the sentinel. The lock is NOT held while waiting below.
            self.thread = None
            ev.set()
        try:
            self.queue.put(None, timeout=max(0.0, deadline - time.monotonic()))
        except queue.Full:
            pass  # queue full and the writer stuck: the event makes the thread exit once it is idle (queue empty)
        t.join(max(0.0, deadline - time.monotonic()))  # a thread that outlives the timeout is harmless: _wlock

    def _inline(self, res: lite.RequestResult, serialise: bool = True) -> None:
        if serialise:
            with self._wlock:
                self._write(res)
        else:
            self._write(res)

    def _write(self, res: lite.RequestResult) -> None:
        try:
            self.writer(res)
        except Exception:
            logger.error("log writer failed", exc_info=True)

    def __call__(self, res: lite.RequestResult) -> None:
        if self.thread is None:  # a cheap peek (not started: inline); the real decision is made under the lock
            self._inline(res)
            return
        size = _approx_bytes(res)  # outside the lock
        with self._lock:
            ev = self._stopping
            full = False
            if self.thread is None or ev is None or ev.is_set():
                inline = True
            else:
                inline = False
                with self._plock:
                    over = bool(self._pending) and self._pending + size > self.max_bytes
                    if over:  # byte bound: keep the billing, drop the text
                        res = _billing_only(res)
                        size = 256
                        self.dropped += 1
                        self._warn()
                    try:
                        self.queue.put_nowait((size, res))
                        self._pending += size
                    except queue.Full:  # count bound: billing-only, written inline below
                        if not over:
                            res = _billing_only(res)
                            self.dropped += 1
                            self._warn()
                        inline = full = True
        if inline:
            self._inline(res, serialise=not full)

    def _warn(self) -> None:
        now = time.monotonic()
        if now - self._last_warn > 60:
            self._last_warn = now
            logger.warning("log writer queue is full: dropping request texts, billing rows are kept "
                           "(%d dropped so far)", self.dropped)

    def _run(self, stopping: threading.Event) -> None:
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                if stopping.is_set():
                    return
                continue
            if item is None:
                return
            size, res = item
            with self._plock:
                self._pending -= size
            self._inline(res)


def cap_gate(db: Database, catalog: Catalog) -> Callable[[], str | None]:
    """Gate for ``build_app``: message when this month's spend reached ``settings.monthly_cap_usd``."""
    def gate() -> str | None:
        raw = catalog.snapshot().settings.get("monthly_cap_usd")
        try:
            cap = float(raw) if raw not in (None, "") else None
        except ValueError:
            logger.warning("settings.monthly_cap_usd is not a number: cap not enforced")
            return None
        if cap is None or cap <= 0 or db.month_spent() < cap:  # 0 = no cap
            return None
        return f"Monthly spending cap of {cap:.2f} USD reached"
    return gate


def build_db_app(db: Database, master_key: str | None, *, secret: Callable[[str], str | None] | None = None,
                 env: Mapping[str, str] | None = None, ttl: float = TTL, **kw: Any) -> Any:
    """The kiko-lite app wired to ``db`` (resolver, provider table, billing, cap). ``kw`` goes to ``build_app``."""
    catalog = Catalog(db, ttl)
    hook = status_hook(db, catalog)
    results = AsyncWriter(BillingWriter(db, catalog))  # ``serve`` starts it; until then it writes inline
    app = lite.build_app(DBResolver(catalog, env), master_key, providers=DBProviders(catalog), secret=secret,
                         on_result=results, gate=cap_gate(db, catalog), on_health=hook, **kw)
    app.results = results  # type: ignore[attr-defined]
    app.health = HealthProber(app.gateway, catalog, hook)  # type: ignore[attr-defined]  # started by ``serve``
    return app
