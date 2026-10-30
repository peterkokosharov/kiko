# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
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

import fnmatch
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from . import app as lite
from . import prices
from . import providers as ptable
from .db import DEFAULT_CLASSES, Database, now_iso
from .health import HealthProber

logger = logging.getLogger("kiko_lite.resolver")

TTL = 2.0
_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# Start of well-known API key formats: a "name" that begins like this is a key VALUE.
_KEY_PREFIXES = ("gsk_", "AIza", "sk-", "sk_", "nvapi-", "xai-", "pplx-", "hf_", "ghp_", "AKIA")
HIDDEN = "<invalid name, value hidden>"
# provider-string prefixes (LiteLLM style, plus kiko-lite's own names) -> kiko-lite provider
_PREFIX = {"gemini": "gemini", "google": "gemini", "nvidia_nim": "nvidia", "nvidia": "nvidia", "groq": "groq",
           "dashscope": "dashscope", "qwen": "dashscope", "openai": "openai", "mistral": "mistral"}
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


def map_row(row: Mapping[str, Any]) -> Mapped:
    """Row (provider, api_base, api_key_env) -> ``Mapped``; raises ``MappingError`` when it cannot be served."""
    provider = str(row.get("provider") or "").strip()
    api_base = str(row.get("api_base") or "").strip()
    prefix, sep, rest = provider.partition("/")
    unknown = False
    if sep and prefix.lower() in _PREFIX:
        canonical, model_id = _PREFIX[prefix.lower()], rest
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
        for row in self.db.models():
            pname = f"db/{row['alias'].strip().lower()}"  # the provider table lower-cases names
            try:
                if pname in seen:
                    raise MappingError(f"alias differs only in case from '{seen[pname]}'")
                seen[pname] = row["alias"]
                mapped, err = map_row(row), None
            except MappingError as e:
                mapped, err = None, str(e)
            infos.append(ModelInfo(row, pname, mapped, err))
        by_pname: dict[str, ModelInfo] = {}
        for i in infos:  # first row wins, in the same order as the collision check above
            by_pname.setdefault(i.pname, i)
        classes = tuple((c["name"], c["keyword"]) for c in self.db.classes())
        return Snapshot(self.db.aliases(), self.db.settings(), infos, {i.row["alias"]: i for i in infos}, by_pname,
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
    """``on_result`` hook: one ``billing`` row per request. Failed requests are recorded with cost 0."""

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
            ts=now_iso(), route="messages", attempts=res.attempts, latency_ms=int(res.latency * 1000),
            upstream_model=f"{canonical}/{res.upstream_model}" if res.upstream_model else None, error=res.error)


def cap_gate(db: Database, catalog: Catalog) -> Callable[[], str | None]:
    """Gate for ``build_app``: message when this month's spend reached ``settings.monthly_cap_usd``."""
    def gate() -> str | None:
        raw = catalog.snapshot().settings.get("monthly_cap_usd")
        try:
            cap = float(raw) if raw not in (None, "") else None
        except ValueError:
            logger.warning("settings.monthly_cap_usd is not a number: cap not enforced")
            return None
        if cap is None or db.month_spent() < cap:
            return None
        return f"Monthly spending cap of {cap:.2f} USD reached"
    return gate


def build_db_app(db: Database, master_key: str | None, *, secret: Callable[[str], str | None] | None = None,
                 env: Mapping[str, str] | None = None, ttl: float = TTL, **kw: Any) -> Any:
    """The kiko-lite app wired to ``db`` (resolver, provider table, billing, cap). ``kw`` goes to ``build_app``."""
    catalog = Catalog(db, ttl)
    hook = status_hook(db, catalog)
    app = lite.build_app(DBResolver(catalog, env), master_key, providers=DBProviders(catalog), secret=secret,
                         on_result=BillingWriter(db, catalog), gate=cap_gate(db, catalog), on_health=hook, **kw)
    app.health = HealthProber(app.gateway, catalog, hook)  # type: ignore[attr-defined]  # started by ``serve``
    return app
