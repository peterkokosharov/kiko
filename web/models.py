# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Models page: list, add/edit, enable/disable, delete. Key NAMES and where they are found; never key values."""

from __future__ import annotations

import math
from typing import Any

from kiko_lite import app as lite
from kiko_lite import prices
from kiko_lite.db import fmt_timeout
from kiko_lite.keys import dotenv_values, key_source
from kiko_lite import providers as ptable
from kiko_lite.resolver import _PREFIX, MappingError, check_api_base, map_row, valid_key_name

from . import render as r
from .ui import redirect


def _num(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError("prices must be finite numbers, zero or more")
    return value


def _timeout_text(value: Any) -> str:
    """Box text of a stored timeout (a number) or of what the user typed (kept as typed when it is not a number)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return str(value or "")


def _key_cell(name: str, env_file: dict[str, str]) -> r.Markup:
    if not valid_key_name(name):  # not a variable name: it may be a pasted secret, so show nothing of it
        return r.tag("span", "invalid key name (hidden)", class_="muted")
    return r.join([r.tag("code", name), " ", r.tag("span", key_source(name, env_file) or "NOT FOUND", class_="badge")])


def split_provider(text: str, names: list[str]) -> tuple[str, str]:
    """Stored ``<provider>/<model id>`` -> (provider name from ``names`` or "", model id).

    The model id may itself contain "/" (``nvidia/nvidia/nemotron-...`` is provider nvidia, id nvidia/nemotron-...).
    A prefix that is not in ``names`` (``nvidia_nim/x``, ``google/x``) or no prefix (``gpt-4o``) gives provider "".
    """
    prefix, sep, rest = (text or "").strip().partition("/")
    if sep and prefix.lower() in names:
        return prefix.lower(), rest
    return "", rest if sep else (text or "").strip()


def known_model_ids(db) -> list[str]:
    """Model ids to suggest: the priced ones (kiko_lite.prices) and those already in the DB."""
    names = [p["name"] for p in db.list_providers()]
    ids = {k.partition("/")[2] if k.partition("/")[0] in names else k for k in prices.PRICES}
    for m in db.models():
        pid = split_provider(m["provider"], names)[1]
        if pid:
            ids.add(pid)
    return sorted(ids)


def row_to_form(row: dict[str, Any], providers: list[dict[str, Any]]) -> dict[str, Any]:
    """A models row -> the form's fields (api_base and api_key_env only when they differ from the provider's)."""
    by_name = {p["name"]: p for p in providers}
    name, model_id = split_provider(row["provider"], list(by_name))
    prov = by_name.get(name)
    key = row.get("api_key_env") or ""
    base = row.get("api_base") or ""
    if prov and key == prov["api_key_env"]:
        key = ""
    if prov and base == prov["api_base"]:
        base = ""
    return {**row, "provider": name, "model_id": model_id, "api_key_env": key if valid_key_name(key) else "",
            "api_base": base}


def register(ui) -> None:  # ui: web.ui.WebUI
    def form_page(sess, values: dict[str, Any], is_new: bool, status: int = 200):
        classes = [c["name"] for c in ui.db.classes()]
        providers = ui.db.list_providers()
        names = [p["name"] for p in providers]
        env_file = dotenv_values(ui.db.path)
        key = values.get("api_key_env", "")

        def v(k: str) -> Any:
            return values.get(k) if values.get(k) is not None else ""

        chosen = v("provider") if v("provider") in names else ""
        prov = next((p for p in providers if p["name"] == chosen), None)
        keys = {p["api_key_env"] for p in providers} | {m["api_key_env"] for m in ui.db.models()}
        prov_key = prov["api_key_env"] if prov and valid_key_name(prov["api_key_env"]) else ""
        key_options = [("", "provider default" + (f" ({prov_key})" if prov_key else ""))]
        key_options += [(k, k) for k in sorted(keys) if valid_key_name(k)]
        if key and valid_key_name(key) and key not in keys:
            key_options.append((key, key))
        defaults = r.table(["Provider", "Default API base", "Default API key name"],
                           [[p["name"], p["api_base"], _key_cell(p["api_key_env"], env_file)] for p in providers])
        used = (r.tag("p", "Used when saved: API base ", r.tag("code", v("api_base") or prov["api_base"]),
                      ", API key name ", r.tag("code", key if valid_key_name(key) and key else (prov_key or "(invalid, hidden)")))
                if prov else "")
        body = r.form(
            "/models/save", sess.csrf,
            r.field("Alias", "alias", v("alias"), required=True, readonly=not is_new),
            r.select("Provider", "provider", ([("", "(choose a provider)")] if not prov else []) + names, chosen),
            r.field("Model ID (for example gemini-3.8-flash; the provider is added in front)", "model_id",
                    v("model_id"), required=True, list_="model-ids", placeholder="gemini-3.8-flash"),
            r.tag("datalist", *[r.tag("option", "", value=i) for i in known_model_ids(ui.db)], id="model-ids"),
            r.select("Class", "model_class", classes, v("model_class") or "SONNET"),
            used,
            r.tag("details", r.tag("summary", "Override the provider's API base and key name (optional)"),
                  r.tag("p", f"The API base is fixed by kiko for {ptable.fixed_base_hint()}; an override is "
                         "only used for openai and for providers you added yourself.", class_="muted"),
                  r.field("API base (empty = the provider's)", "api_base", v("api_base"),
                          placeholder=prov["api_base"] if prov else "https://api.example.com/v1"),
                  r.select("API key name (the name of an environment variable, not the key)", "api_key_env",
                           key_options, key if valid_key_name(key) else ""),
                  open=bool(v("api_base") or key)),
            r.field("Priority", "priority", v("priority") if v("priority") != "" else 100, "number", min=1),
            r.field("Price in ($/Mtok)", "price_in_per_mtok", v("price_in_per_mtok"), "number", step="any", min=0),
            r.field("Price out ($/Mtok)", "price_out_per_mtok", v("price_out_per_mtok"), "number", step="any", min=0),
            r.field("Price cache read ($/Mtok)", "price_cache_read_per_mtok", v("price_cache_read_per_mtok"),
                    "number", step="any", min=0),
            r.select("Thinking (OpenAI-compatible models only)", "thinking",
                     [("default", "default (send nothing)"), ("off", "off (enable_thinking=false)"), ("on", "on (enable_thinking=true)"),
                      ("off-prompt", "off via a /no_think system line")], v("thinking") or "default"),
            r.select("Toolshim (turn a tool call the model wrote as text into a real one)", "toolshim",
                     [("off", "off"), ("on", "on")], v("toolshim") or "off"),
            r.field("Timeout in seconds (empty = the global upstream_timeout; for slow free models, e.g. 600)",
                    "timeout", _timeout_text(v("timeout")), "number",
                    step="any", min=0),
            r.checkbox("Enabled", "enabled", bool(values.get("enabled", True))),
            r.button("Save"), " ", r.link("Cancel", "/models"),
            r.tag("h2", "Provider defaults"), defaults)
        return ui.render(sess, "Add model" if is_new else "Edit model", body, "/models", status=status)

    @ui.get("/models")
    def models_list(req, sess):
        env_file = dotenv_values(ui.db.path)
        rows = []
        for m in ui.db.models():
            alias = m["alias"]
            actions = r.join([
                r.link("Edit", r.url("/models/edit", alias=alias)), " ",
                r.inline_post("/models/toggle", sess.csrf, "Disable" if m["enabled"] else "Enable", "secondary",
                              alias=alias),
                r.inline_post("/models/delete", sess.csrf, "Delete", "danger", alias=alias)])
            rows.append([alias, m["provider"], m["model_class"], m["priority"], "yes" if m["enabled"] else "no",
                         m["status"], m.get("thinking") or "default", m.get("toolshim") or "off",
                         f"{fmt_timeout(m.get('timeout'))} s" if fmt_timeout(m.get("timeout")) else "", _key_cell(m["api_key_env"], env_file), actions])
        body = r.join([r.tag("p", r.link("Add model", "/models/new", class_="button")),
                       r.table(["Alias", "Provider", "Class", "Priority", "Enabled", "Status", "Thinking", "Toolshim", "Timeout", "API key", ""], rows)])
        return ui.render(sess, "Models", body, "/models")

    @ui.get("/models/new")
    def model_new(req, sess):
        return form_page(sess, {}, True)

    @ui.get("/models/edit")
    def model_edit(req, sess):
        row = ui.db.get_model(req.query.get("alias", ""))
        if row is None:
            return ui.error(404, "No such model.")
        values = row_to_form(row, ui.db.list_providers())
        if not values["provider"]:
            sess.flash("error", f"The provider of this model ('{row['provider'].partition('/')[0]}') is not in the "
                                "providers list: choose one and save.")
        return form_page(sess, values, False)

    @ui.post("/models/save")
    def model_save(req, sess, form):
        alias = form.get("alias", "").strip()
        try:
            if not alias:
                raise ValueError("alias is required")
            prov = ui.db.get_provider(form.get("provider", "").strip())
            if prov is None:
                raise ValueError("choose a provider from the list (add one on the Providers page)")
            model_id = form.get("model_id", "").strip()
            if not model_id:
                raise ValueError("model id is required")
            key = form.get("api_key_env", "").strip() or prov["api_key_env"]
            if not valid_key_name(key):
                raise ValueError("API key name must be an environment variable name (letters, digits, "
                                 "underscore), not the key itself")
            override = form.get("api_base", "").strip()
            if override:
                check_api_base(override)  # https, no credentials; kiko-full honours this column for every provider
                if ptable.fixed_base_provider(prov["name"]):
                    raise ValueError(f"kiko fixes the API base for {prov['name']}; leave the override empty")
            # A provider the resolver does not know by name needs its API base on the row (custom endpoint).
            base = override or (prov["api_base"] if prov["name"] not in _PREFIX else None)
            stored = f"{prov['name']}/{model_id}"
            try:
                mapped = map_row({"provider": stored, "api_base": base, "api_key_env": key},
                                 {prov["name"]} - set(_PREFIX))
            except MappingError as e:
                raise ValueError(str(e)) from None
            if not lite.valid_model_id(mapped.model_id, str(mapped.entry.get("kind", "openai"))):
                raise ValueError("the model id has characters that are not allowed")
            created = ui.db.upsert_model(
                alias, stored, key, base, form.get("model_class", "SONNET"),
                int(form.get("priority") or 100), _num(form.get("price_in_per_mtok", "")),
                _num(form.get("price_out_per_mtok", "")), _num(form.get("price_cache_read_per_mtok", "")),
                "enabled" in form, thinking=form.get("thinking", "default") or "default",
                timeout=form.get("timeout", "").strip() or "default",
                toolshim=form.get("toolshim", "off") or "off")  # empty clears
        except (ValueError, TypeError) as e:
            sess.flash("error", str(e))
            values = {**form, "enabled": "enabled" in form}
            return form_page(sess, values, ui.db.get_model(alias) is None, 400)
        sess.flash("ok", f"Model {alias} {'added' if created else 'saved'}.")
        return redirect("/models")

    @ui.post("/models/toggle")
    def model_toggle(req, sess, form):
        row = ui.db.get_model(form.get("alias", ""))
        if row is None:
            sess.flash("error", "No such model.")
        else:
            ui.db.set_enabled(row["alias"], not row["enabled"])
            sess.flash("ok", f"Model {row['alias']} {'disabled' if row['enabled'] else 'enabled'}.")
        return redirect("/models")

    @ui.post("/models/delete")
    def model_delete(req, sess, form):
        alias = form.get("alias", "")
        deleted = ui.db.delete_model(alias)
        sess.flash("ok" if deleted else "error", f"Model {alias} deleted." if deleted else "No such model.")
        return redirect("/models")
