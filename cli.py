# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""``python -m kiko_lite <command>``: serve, import, add-model, list-models, enable/disable/delete-model,
costs, doctor (plus small helpers: add-alias, delete-alias, set-setting, unset-setting, list-classes, add-class).

Auth: ``serve`` needs MASTER_KEY (clients send it as x-api-key). ``serve --no-auth`` (or env KIKO_AUTH=none) is the
explicit home mode: no client key at all; use it only on a trusted LAN. It wins over a MASTER_KEY that exists.

Secrets: API key VALUES live in env / the OS keystore / a ``.env`` file next to the DB file (only there); the
DB holds only key NAMES (``fix-key`` repairs legacy rows that hold a value). Nothing in
this module prints a key value (``import`` and ``doctor`` print names and "where", never values).

Notes for users coming from kiko-full:

* kiko-full ``add_model`` uses INSERT OR REPLACE. That gives the row a new id and NULL kiko-lite price columns,
  so a re-import afterwards creates a new ``KIKO_<ALIAS>_<ID>_API_KEY`` name. The old keystore entry can be
  removed by hand.
* Aliases that differ only in case collide; the winner is the first row in model_class, priority, id order.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kikolib import dotenv, factory, http_client as _http, keystore, server as kserver, yaml_lite  # noqa: F401
from . import app as lite
from . import prices
from . import providers as ptable
from .db import CLASSES, Database, SCHEMA_VERSION, default_path
from .resolver import HIDDEN, MappingError, build_db_app, key_name_problem, map_row, show_name, valid_key_name

SETTING_KEYS = ("monthly_cap_usd", "disable_billing", "health_check_interval", "health_no_probe")
_ENV_REF = "os.environ/"
PENDING_WIPE = "fix_key_pending"  # setting: fix-key moved a key but could not wipe the old value from the file


# ---- helpers ----------------------------------------------------------------------------------------

def _dotenv_values(db_path: Path) -> dict[str, str]:
    """Values of the ``.env`` file next to the DB file (only there, never the current folder)."""
    try:
        return dotenv.parse((db_path.parent / ".env").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError):
        return {}


def _key_source(name: str, env_file: Mapping[str, str]) -> str | None:
    """Where the key ``name`` is found: "env", "store" or "dotenv" (that order); never the value."""
    if os.environ.get(name) or os.environ.get(name.upper()):
        return "env"
    if keystore.get_secret(name, env={}):
        return "store"
    if env_file.get(name):
        return "dotenv"
    return None


def _store_name() -> str:
    for r in factory.report():
        if r["kind"] == keystore.KIND:
            return r["chosen"] or "no keystore"
    return "no keystore"


def _master_source() -> str | None:
    if os.environ.get("MASTER_KEY"):
        return "env"
    return "store" if keystore.get_secret("MASTER_KEY", env={}) else None


def _open(args: argparse.Namespace, create: bool = False, readonly: bool = False) -> Database | None:
    path = default_path(args.db)
    if not create and not path.exists():
        print(f"kiko: no database at {path}")
        return None
    return Database(path, readonly=readonly)


def _unmigrated_note(db: Database) -> None:
    v = db.schema_version()
    if v < SCHEMA_VERSION:
        print(f"kiko: schema not migrated (user_version {v}); opened read-only, nothing was changed. "
              "serve, import, add-model or fix-key migrate it.")


def _err(msg: str) -> int:
    print(f"kiko: {msg}", file=sys.stderr)
    return 2


def _class(value: str) -> str:
    return value.upper()  # checked against the model_classes table where the DB is open


def _auth_off(args: argparse.Namespace) -> bool:
    """Explicit home mode: ``serve --no-auth`` or env KIKO_AUTH=none."""
    return bool(getattr(args, "no_auth", False)) or os.environ.get("KIKO_AUTH", "").strip().lower() == "none"


def _class_names(db_path: Path) -> list[str]:
    """Class names of the DB (read-only); the four defaults when there is no DB yet or it is a v1 file."""
    if db_path.exists():
        try:
            return Database(db_path, readonly=True).class_names()
        except Exception:
            pass
    return list(CLASSES)


# ---- serve ------------------------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    no_auth = _auth_off(args)
    master = os.environ.get("MASTER_KEY") or keystore.get_secret("MASTER_KEY")
    if not master and not no_auth:
        return _err("no master key: set env MASTER_KEY or store it in the OS keystore as MASTER_KEY "
                    "(or run with --no-auth for home mode)")
    db_path = default_path(args.db)
    db = Database(db_path)
    env_file = _dotenv_values(db_path)

    def secret(name: str) -> str | None:  # env -> OS store -> .env
        return keystore.get_secret(name) or env_file.get(name) or None

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if no_auth and master:
        logging.getLogger("kiko_lite.cli").warning("--no-auth given: MASTER_KEY is ignored, no client auth")
    app = build_db_app(db, None if no_auth else master, secret=secret, auth=not no_auth)
    print(f"kiko-lite on http://{args.host}:{args.port}  db={db_path}", flush=True)
    app.health.start()  # background prober (setting health_check_interval; 0 = off)
    try:
        kserver.serve(app, args.host, args.port)
    finally:
        app.health.stop()  # serve() returns after the drain; also on a bind error
    return 0


# ---- import -----------------------------------------------------------------------------------------

@dataclass
class PlanModel:
    alias: str
    provider: str
    api_base: str | None
    key_name: str  # the NAME stored in the DB
    literal_key: str | None = field(default=None, repr=False)  # a key value found in the YAML: goes to the keystore
    model_class: str = "SONNET"
    priority: int = 100
    enabled: bool | None = None
    status: str | None = None


@dataclass
class Plan:
    models: list[PlanModel] = field(default_factory=list)
    aliases: list[tuple[str, str]] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def generated_key_name(alias: str, row_id: int, taken: set[str] | frozenset[str] = frozenset()) -> str:
    """Keystore name for a key moved out of YAML / a legacy row: unique per row (the row id is in it).

    ``KIKO_<ALIAS>_<ID>_API_KEY``; if that is not a valid name (a long alias can look like a token) or is
    taken by another row, ``KIKO_MODEL_<ID>_API_KEY``. The result always passes ``valid_key_name``.
    """
    san = "".join(c if c.isalnum() else "_" for c in alias.upper()).strip("_")
    for cand in ([f"KIKO_{san}_{row_id}_API_KEY"] if san else []) + [f"KIKO_MODEL_{row_id}_API_KEY"]:
        if valid_key_name(cand) and cand not in taken:
            return cand
    n = 1
    while f"KIKO_MODEL_{row_id}_{n}_API_KEY" in taken:
        n += 1
    return f"KIKO_MODEL_{row_id}_{n}_API_KEY"


def _store_key(name: str, value: str) -> bool:
    """Put ``value`` in the keystore under ``name``; refuses to store a key as its own name."""
    return value != name and keystore.set_secret(name, value)


def _drop_key(name: str) -> bool:
    """Delete the keystore entry ``name``; False if the store has no delete or it failed."""
    store = keystore._store()
    try:
        return bool(store is not None and store.delete(name))
    except Exception:
        return False


def build_plan(conf: Any, classes: Sequence[str] = CLASSES) -> Plan:
    """kiko-full YAML (already parsed) -> what to import. Pure: no DB, no keystore."""
    plan = Plan()
    if not isinstance(conf, dict):
        plan.problems.append("the YAML top level is not a mapping")
        return plan
    for top in conf:
        if top not in ("model_list", "model_aliases", "general_settings"):
            plan.ignored.append(f"{top}" if top != "litellm_settings" else "litellm_settings (LiteLLM only)")
    for i, entry in enumerate(conf.get("model_list") or []):
        if not isinstance(entry, dict) or not entry.get("model_name"):
            plan.problems.append(f"model_list[{i}]: no model_name, skipped")
            continue
        alias = str(entry["model_name"])
        params = entry.get("litellm_params") or {}
        if not isinstance(params, dict):
            params = {}
        for k in entry:
            if k not in ("model_name", "litellm_params", "model_class", "priority", "enabled", "status"):
                plan.ignored.append(f"model_list[{alias}].{k}")
        for k in params:
            if k not in ("model", "api_base", "api_key"):
                plan.ignored.append(f"model_list[{alias}].litellm_params.{k}")
        provider = str(params.get("model") or entry.get("provider") or "")
        ref = str(params.get("api_key") or "")
        flat = False
        if not ref and entry.get("api_key_env"):  # flat kiko-full form: a NAME, but may hold a key value
            ref, flat = str(entry["api_key_env"]), True
        literal = None
        key_name = ""
        if ref.startswith(_ENV_REF):
            key_name = ref[len(_ENV_REF):]
        elif ref and flat and key_name_problem(ref) is None:
            key_name = ref
        elif ref and flat and key_name_problem(ref) == "name":
            plan.problems.append(f"model_list[{alias}]: api_key_env is not a valid NAME, skipped")
            continue
        elif ref:  # a literal key value (never kept in the DB, never printed): the name is made at import time
            literal = ref
        if key_name and not valid_key_name(key_name):
            plan.problems.append(f"model_list[{alias}]: the key name is not a valid NAME, skipped")
            continue
        cls = str(entry.get("model_class") or "SONNET").upper()
        if cls not in classes:
            plan.problems.append(f"model_list[{alias}]: unknown model_class, using SONNET")
            cls = "SONNET"
        try:
            prio = int(entry.get("priority", 100))
        except (TypeError, ValueError):
            plan.problems.append(f"model_list[{alias}]: bad priority, using 100")
            prio = 100
        plan.models.append(PlanModel(
            alias, provider, str(params.get("api_base") or entry.get("api_base") or "") or None, key_name, literal,
            cls, prio, bool(entry["enabled"]) if "enabled" in entry else None,
            str(entry["status"]) if entry.get("status") else None))
    aliases = conf.get("model_aliases") or {}
    if isinstance(aliases, dict):
        plan.aliases = [(str(g), str(t)) for g, t in aliases.items()]
    else:
        plan.problems.append("model_aliases is not a mapping, skipped")
    gs = conf.get("general_settings") or {}
    for k, v in (gs.items() if isinstance(gs, dict) else []):
        if k in SETTING_KEYS:
            if k == "monthly_cap_usd":
                try:
                    float(v)
                except (TypeError, ValueError):
                    plan.problems.append("general_settings.monthly_cap_usd is not a number, skipped")
                    continue
            plan.settings[k] = str(v).lower() if isinstance(v, bool) else str(v)
        elif k == "billing_db":
            plan.ignored.append("general_settings.billing_db (use --db or env KIKO_DB_PATH)")
        else:
            plan.ignored.append(f"general_settings.{k}")
    return plan


def cmd_import(args: argparse.Namespace) -> int:
    try:
        conf = yaml_lite.load_file(args.yaml)
    except (OSError, ValueError) as e:  # YamlError is a ValueError
        return _err(f"cannot read {args.yaml}: {e}")
    path = default_path(args.db)
    plan = build_plan(conf, _class_names(path))
    dry = args.dry_run
    print(f"kiko import {'(dry run, nothing is written) ' if dry else ''}-> {path}")
    db = None if dry else Database(path)
    done = 0
    for m in plan.models:
        try:
            mapped = map_row({"provider": m.provider, "api_base": m.api_base,
                              "api_key_env": m.key_name or ("KIKO_PENDING_API_KEY" if m.literal_key is not None else "")})
        except MappingError as e:
            plan.problems.append(f"model {m.alias}: {e}, skipped")
            continue
        if not m.key_name and m.literal_key is None:  # no key in the YAML: the provider's default name, if any
            defaults = ptable.PROVIDERS.get(mapped.provider, {}).get("key_names") or []
            if not defaults:
                plan.problems.append(f"model {m.alias}: no api_key in the YAML, skipped")
                continue
            m.key_name = defaults[0]
        price = prices.lookup(mapped.provider, mapped.model_id)
        where = f"{mapped.provider}/{mapped.model_id}"
        if dry:
            via = ("store its key in the keystore under a generated KIKO_..._API_KEY name" if m.literal_key
                   else f"key name {m.key_name}")
            print(f"  would import model {m.alias} -> {where} ({via}), class {m.model_class}, priority {m.priority},"
                  f" price {'known' if price else 'unknown'}")
            done += 1
            continue
        assert db is not None
        if m.literal_key is not None:
            try:
                db.check_alias(m.alias)
            except ValueError as e:
                plan.problems.append(f"model {m.alias}: {e}, skipped")
                continue
            m.key_name = generated_key_name(m.alias, db.next_model_id(m.alias), db.key_names_in_use(m.alias))
            existed = bool(keystore.get_secret(m.key_name, env={}))
            if not _store_key(m.key_name, m.literal_key):
                plan.problems.append(f"model {m.alias}: could not store its key in the keystore, skipped")
                continue
            print(f"  stored key for {m.alias} in {_store_name()} as {m.key_name}")
        try:
            new = db.upsert_model(m.alias, m.provider, m.key_name, m.api_base, m.model_class, m.priority,
                                  price[0] if price else None, price[1] if price else None,
                                  price[2] if price else None, m.enabled, m.status)
        except ValueError as e:
            plan.problems.append(f"model {m.alias}: {e}, skipped")
            if m.literal_key is not None and not existed and not _drop_key(m.key_name):
                plan.problems.append(f"model {m.alias}: orphaned keystore entry {m.key_name} (remove it by hand)")
            continue
        print(f"  {'added' if new else 'updated'} model {m.alias} -> {where}"
              f"{'' if price else '  (no known price: cost 0)'}")
        done += 1
    for glob, target in plan.aliases:
        print(f"  {'would set' if dry else 'alias'} {glob} -> {target}")
        if db is not None:
            db.set_alias(glob, target)
    for k, v in plan.settings.items():
        print(f"  {'would set' if dry else 'setting'} {k} = {v}")
        if db is not None:
            db.set_setting(k, v)
    for item in plan.ignored:
        print(f"  ignored: {item}")
    for p in plan.problems:
        print(f"  problem: {p}")
    print(f"kiko import: {done} model(s), {len(plan.aliases)} alias(es), {len(plan.settings)} setting(s)"
          f"{'' if not plan.problems else ', ' + str(len(plan.problems)) + ' problem(s)'}")
    if not dry and not plan.problems:
        print(f"{args.yaml} is no longer needed and can be deleted.")
    return 1 if plan.problems else 0


# ---- model commands ---------------------------------------------------------------------------------

def cmd_add_model(args: argparse.Namespace) -> int:
    if not valid_key_name(args.api_key_env):
        return _err("--api-key-env must be a NAME such as GEMINI_API_KEY, not a key value")
    try:
        mapped = map_row({"provider": args.provider, "api_base": args.api_base, "api_key_env": args.api_key_env})
    except MappingError as e:
        return _err(str(e))
    if not lite.valid_model_id(mapped.model_id, str(mapped.entry.get("kind", "openai"))):
        return _err("the model id in --provider has characters that are not allowed")
    known = prices.lookup(mapped.provider, mapped.model_id)
    p_in = args.price_in if args.price_in is not None else (known[0] if known else None)
    p_out = args.price_out if args.price_out is not None else (known[1] if known else None)
    p_cache = args.price_cache if args.price_cache is not None else (known[2] if known else None)
    db = _open(args, create=True)
    assert db is not None
    try:
        new = db.upsert_model(args.alias, args.provider, args.api_key_env, args.api_base, args.model_class,
                              args.priority, p_in, p_out, p_cache)
    except ValueError as e:
        return _err(str(e))
    print(f"kiko: {'added' if new else 'updated'} model '{args.alias}' in {db.path}")
    if p_in is None or p_out is None:
        print("kiko: warning: no price known for this model, its cost is recorded as 0 (use --price-in/--price-out)")
    return 0


def cmd_list_classes(args: argparse.Namespace) -> int:
    db = _open(args, readonly=True)
    if db is None:
        return 0
    _unmigrated_note(db)
    print(f"Model classes in {db.path}:")
    print(f"  {'NAME':<14} {'KEYWORD':<12} {'MODELS':>6}  ROLE")
    for c in db.classes():
        print(f"  {c['name']:<14} {c['keyword']:<12} {c['models']:>6}  {c['role']}")
    return 0


def cmd_add_class(args: argparse.Namespace) -> int:
    db = _open(args, create=True)
    assert db is not None
    try:
        db.add_class(args.name, args.keyword, args.role)
    except (ValueError, sqlite3.IntegrityError) as e:  # IntegrityError: another process added it first
        return _err(str(e))
    print(f"kiko: added class {args.name} (keyword '{args.keyword}'); "
          f"env fallback ANTHROPIC_DEFAULT_{args.name}_MODEL")
    return 0


def cmd_list_models(args: argparse.Namespace) -> int:
    db = _open(args, readonly=True)
    if db is None:
        return 0
    _unmigrated_note(db)
    models = db.models()
    print(f"Models in {db.path}:" if models else f"No models in {db.path}")
    for r in models:
        try:
            m = map_row(r)
            target = f"{m.provider}/{m.model_id}"
        except MappingError as e:
            target = f"UNUSABLE ({e})"
        key = show_name(r["api_key_env"])
        print(f"  {r['alias']:<28} {r['model_class']:<7} prio {r['priority']:<4} {r['status']:<8} "
              f"{'on ' if r['enabled'] else 'OFF'} {target}  key={key}")
    for glob, target in db.aliases():
        print(f"  alias {glob} -> {target}")
    for k, v in db.settings().items():
        print(f"  setting {k} = {v}")
    return 0


def _change(args: argparse.Namespace, what: str) -> int:
    db = _open(args)
    if db is None:
        return 1
    if what == "delete":
        ok = db.delete_model(args.alias)
    else:
        ok = db.set_enabled(args.alias, what == "enable")
    if not ok:
        print(f"kiko: model '{args.alias}' not found in {db.path}")
        return 1
    print(f"kiko: {what}d '{args.alias}'")
    return 0


def cmd_add_alias(args: argparse.Namespace) -> int:
    db = _open(args, create=True)
    assert db is not None
    db.set_alias(args.glob, args.target)
    print(f"kiko: alias {args.glob} -> {args.target}")
    return 0


def cmd_delete_alias(args: argparse.Namespace) -> int:
    db = _open(args)
    if db is None or not db.delete_alias(args.glob):
        print(f"kiko: alias '{args.glob}' not found")
        return 1
    print(f"kiko: deleted alias {args.glob}")
    return 0


def cmd_set_setting(args: argparse.Namespace) -> int:
    if args.key not in SETTING_KEYS:
        return _err(f"unknown setting; known: {', '.join(SETTING_KEYS)}")
    if args.key == "monthly_cap_usd":
        try:
            float(args.value)
        except ValueError:
            return _err("monthly_cap_usd must be a number")
    db = _open(args, create=True)
    assert db is not None
    db.set_setting(args.key, args.value)
    print(f"kiko: setting {args.key} = {args.value}")
    return 0


def cmd_unset_setting(args: argparse.Namespace) -> int:
    db = _open(args)
    if db is None or not db.delete_setting(args.key):
        print(f"kiko: setting '{args.key}' not set")
        return 1
    print(f"kiko: removed setting {args.key}")
    return 0


# ---- costs / doctor ---------------------------------------------------------------------------------

def cmd_fix_key(args: argparse.Namespace) -> int:
    """Legacy kiko-full row whose api_key_env holds a key VALUE: move it to the keystore, keep only a name."""
    db = _open(args)
    if db is None:
        return 1
    row = db.get_model(args.alias)
    if row is None:
        print(f"kiko: model '{args.alias}' not found in {db.path}")
        return 1
    value = str(row["api_key_env"] or "")
    problem = key_name_problem(value)
    if problem is None:  # a valid name: nothing to move (never store a name as a key)
        print(f"kiko: '{args.alias}' already uses the key name {value}; nothing to fix")
        if db.settings().get(PENDING_WIPE):  # an earlier fix-key could not wipe the old value from the file
            return _finish_wipe(db)
        return 0
    if problem == "name":
        return _err(f"api_key_env of '{args.alias}' is not a valid NAME and does not look like a key value, so "
                    f"fix-key does not apply; use `kiko add-model {args.alias} --provider PROVIDER/ID "
                    "--api-key-env NAME`")
    name = generated_key_name(args.alias, int(row["id"]), db.key_names_in_use(args.alias))
    if not _store_key(name, value):
        return _err("could not store the key in the keystore; the row was not changed")
    _, wiped = db.set_key_name(args.alias, name)
    print(f"kiko: moved the key of '{args.alias}' to {_store_name()} as {name}; the DB now holds only the name")
    if not wiped:
        db.set_setting(PENDING_WIPE, "1")
        return _wipe_blocked()
    return 0


def _wipe_blocked() -> int:
    print("kiko: the old value may stay in the DB file until the server stops (a reader holds the database). "
          "Stop kiko, then run `kiko fix-key <alias>` again to finish.", file=sys.stderr)
    return 1


def _finish_wipe(db: Database) -> int:
    if db.checkpoint():
        db.delete_setting(PENDING_WIPE)
        print("kiko: the old value is now wiped from the DB file")
        return 0
    return _wipe_blocked()


def cmd_costs(args: argparse.Namespace) -> int:
    db = _open(args, readonly=True)
    if db is None:
        return 0
    _unmigrated_note(db)
    rows = db.billing_summary(args.days)
    if not rows:
        print(f"No billing data in {db.path} for the last {args.days} day(s)")
        return 0
    col = f"{'Model':<35} {'Calls':>6} {'Errors':>6} {'Tokens In':>10} {'Tokens Out':>11} {'Cost USD':>12}"
    print(f"Cost summary - last {args.days} day(s)  [{db.path}]")
    print(col)
    print("-" * len(col))
    total = 0.0
    for r in rows:
        print(f"  {r['model']:<33} {r['calls']:>6} {r['errors']:>6} {r['tokens_in']:>10} {r['tokens_out']:>11} "
              f"{r['cost_usd']:>12.6f}")
        total += r["cost_usd"]
    print("-" * len(col))
    print(f"  {'TOTAL':<33} {'':>6} {'':>6} {'':>10} {'':>11} {total:>12.6f}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    path = default_path(args.db)
    env_file = _dotenv_values(path)
    warnings = 0
    print(f"Python {platform.python_version()} on {sys.platform}")
    print("Implementations (kikolib factory):")
    for r in factory.report():
        print(f"  {r['kind']:<10} {r['chosen'] or 'NONE'} ({r['source'] or '-'}; {r['reason']})")
        warnings += r["chosen"] is None
    src = _master_source()
    print(f"Master key: {'found in ' + src if src else 'NOT FOUND (serve will refuse to start without --no-auth)'}")
    if os.environ.get("KIKO_AUTH", "").strip().lower() == "none":
        print("Auth mode: OFF (env KIKO_AUTH=none): home mode, serve takes requests without a client key"
              f"{'; the master key is ignored' if src else ''}")
        warnings += 1
    else:
        print("Auth mode: client key required (home mode: serve --no-auth or env KIKO_AUTH=none)")
        warnings += src is None
    if not path.exists():
        print(f"Database: {path} (does not exist yet; created by serve / import / add-model)")
        print(f"\n{warnings} warning(s)")
        return 0
    db = Database(path, readonly=True)  # doctor never modifies a DB
    models = db.models()
    print(f"Database: {path}  schema version {db.schema_version()} (kiko-lite {SCHEMA_VERSION})  "
          f"{len(models)} model(s)")
    if db.schema_version() < SCHEMA_VERSION:
        print(f"  schema not migrated (user_version {db.schema_version()}): older schema (kiko-full or older "
              "kiko-lite), left untouched; "
              "serve, import, add-model or fix-key migrate it")
        warnings += 1
    settings = db.settings()
    if settings.get(PENDING_WIPE):
        print("  WARNING a key moved by fix-key may still be in the DB file; stop kiko and run fix-key again "
              "on that model")
        warnings += 1
    cap = settings.get("monthly_cap_usd")
    print(f"Monthly cap: {cap if cap else 'not set'}  spent this month: {db.month_spent():.4f} USD")
    for r in models:
        line = f"  {r['alias']}: {'enabled' if r['enabled'] else 'DISABLED'}, class {r['model_class']}"
        try:
            m = map_row(r)
        except MappingError as e:
            print(f"{line}\n      mapping: ERROR {e}")
            warnings += 1
            continue
        print(line)
        print(f"      provider: {r['provider']} -> {m.provider}/{m.model_id} ({m.entry['kind']}, {m.entry['base_url']})")
        if not lite.valid_model_id(m.model_id, str(m.entry["kind"])):
            print("      WARNING: model id has characters the gateway refuses")
            warnings += 1
        name = r["api_key_env"]
        problem = key_name_problem(name)
        if problem is not None:
            print(f"      key: {HIDDEN}")
            if problem == "value":
                print(f"      WARNING row {r['alias']} holds a key value in api_key_env; "
                      f"run `kiko fix-key {r['alias']}`")
            else:
                print(f"      WARNING row {r['alias']} has no valid key NAME in api_key_env (fix-key does not "
                      f"apply); run `kiko add-model {r['alias']} --provider PROVIDER/ID --api-key-env NAME`")
            warnings += 1
        else:
            where = _key_source(name, env_file)
            print(f"      key {name}: {'found in ' + where if where else 'NOT FOUND'}")
            warnings += where is None
        has_price = r.get("price_in_per_mtok") is not None and r.get("price_out_per_mtok") is not None
        print(f"      price: {'known' if has_price else 'UNKNOWN (cost recorded as 0)'}")
        warnings += not has_price
    print(f"\n{warnings} warning(s)")
    return 0


# ---- parser -----------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH", help="database file (default: env KIKO_DB_PATH, else ~/.kiko/kiko.db)")
    p = argparse.ArgumentParser(prog="kiko", description="kiko-lite: Anthropic API in front of other providers")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help)
        sp.set_defaults(func=fn)
        return sp

    s = add("serve", cmd_serve, "run the server (needs MASTER_KEY in env or keystore, or --no-auth)")
    s.add_argument("--no-auth", action="store_true",
                   help="home mode: accept requests without a client key (also env KIKO_AUTH=none); trusted LAN only")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=4000)
    s = add("import", cmd_import, "import an old kiko.yaml once (models, aliases, settings; keys -> keystore)")
    s.add_argument("yaml")
    s.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    s = add("add-model", cmd_add_model, "add or update a model")
    s.add_argument("alias")
    s.add_argument("--provider", required=True, metavar="PROVIDER/ID", help="e.g. gemini/gemini-3.8-flash")
    s.add_argument("--api-key-env", required=True, metavar="NAME", help="NAME of the API key (not the key)")
    s.add_argument("--api-base", metavar="URL")
    s.add_argument("--class", dest="model_class", type=_class, default="SONNET", metavar="CLASS",
                   help="a class from list-classes (default SONNET)")
    s.add_argument("--priority", type=int, default=100)
    s.add_argument("--price-in", type=float, metavar="USD_PER_MTOK")
    s.add_argument("--price-out", type=float, metavar="USD_PER_MTOK")
    s.add_argument("--price-cache", type=float, metavar="USD_PER_MTOK", help="cache-read price")
    add("list-models", cmd_list_models, "list models, aliases and settings")
    add("list-classes", cmd_list_classes, "list model classes (name, keyword, role, enabled models)")
    s = add("add-class", cmd_add_class, "add a model class (detected by KEYWORD in the requested model name)")
    s.add_argument("name", help="upper-case, e.g. VOICE")
    s.add_argument("--keyword", required=True, help="lower-case text looked for in the requested model name")
    s.add_argument("--role", default="", help="what the class is for")
    for name in ("enable", "disable", "delete"):
        s = add(f"{name}-model", lambda a, n=name: _change(a, n), f"{name} a model")
        s.add_argument("alias")
    s = add("add-alias", cmd_add_alias, "route client model names matching GLOB to a model alias")
    s.add_argument("glob")
    s.add_argument("target")
    s = add("delete-alias", cmd_delete_alias, "remove an alias glob")
    s.add_argument("glob")
    s = add("set-setting", cmd_set_setting, f"set one of: {', '.join(SETTING_KEYS)}")
    s.add_argument("key")
    s.add_argument("value")
    s = add("unset-setting", cmd_unset_setting, "remove a setting")
    s.add_argument("key")
    s = add("fix-key", cmd_fix_key, "move a key VALUE found in a legacy row's api_key_env into the keystore")
    s.add_argument("alias")
    s = add("costs", cmd_costs, "cost summary from the billing table")
    s.add_argument("--days", type=int, default=30)
    add("doctor", cmd_doctor, "check the installation (no network, never prints a secret)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
