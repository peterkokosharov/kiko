# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Providers page: list, add, edit, delete. The model form takes provider, API base and key NAME from here."""

from __future__ import annotations

from kiko_lite import providers as ptable
from kiko_lite.keys import dotenv_values
from kiko_lite.resolver import check_provider, valid_key_name

from . import render as r
from .models import _key_cell
from .ui import redirect


def register(ui) -> None:  # ui: web.ui.WebUI
    def form(sess, values: dict[str, str], is_new: bool, status: int = 200):
        key = values.get("api_key_env", "")
        fixed = values.get("name", "") in ptable.PROVIDERS  # kiko-lite uses its own base for these
        body = r.form(
            "/providers/save", sess.csrf,
            r.field("Name (lower case; the PROVIDER/ part of a stored model)", "name", values.get("name", ""),
                    required=True, readonly=not is_new, placeholder="gemini"),
            r.field("API base (https)" + (" - fixed by kiko for this provider" if fixed else ""), "api_base",
                    values.get("api_base", ""), required=True, readonly=fixed,
                    placeholder="https://api.example.com/v1"),
            r.field("API key name (environment variable, not the key)", "api_key_env",
                    key if valid_key_name(key) else "", required=True, placeholder="EXAMPLE_API_KEY"),
            r.button("Save"), " ", r.link("Cancel", "/providers"))
        return ui.render(sess, "Add provider" if is_new else "Edit provider", body, "/providers", status=status)

    @ui.get("/providers")
    def providers_list(req, sess):
        env_file = dotenv_values(ui.db.path)
        rows = [[p["name"], r.join([p["api_base"], r.tag("span", " (fixed by kiko for this provider)", class_="muted")
                                    if p["name"] in ptable.PROVIDERS else ""]), _key_cell(p["api_key_env"], env_file), p["models"],
                 r.join([r.link("Edit", r.url("/providers/edit", name=p["name"])), " ",
                         r.inline_post("/providers/delete", sess.csrf, "Delete", "danger", name=p["name"])])]
                for p in ui.db.list_providers()]
        body = r.join([r.tag("p", r.link("Add provider", "/providers/new", class_="button")),
                       r.table(["Name", "API base", "API key", "Models", ""], rows)])
        return ui.render(sess, "Providers", body, "/providers")

    @ui.get("/providers/new")
    def provider_new(req, sess):
        return form(sess, {}, True)

    @ui.get("/providers/edit")
    def provider_edit(req, sess):
        row = ui.db.get_provider(req.query.get("name", ""))
        if row is None:
            return ui.error(404, "No such provider.")
        return form(sess, row, False)

    @ui.post("/providers/save")
    def provider_save(req, sess, form_data):
        name = form_data.get("name", "").strip().lower()
        base, key = form_data.get("api_base", "").strip(), form_data.get("api_key_env", "").strip()
        if name in ptable.PROVIDERS:  # kiko-lite ignores the base of its own providers: keep the known one
            base = (ui.db.get_provider(name) or {}).get("api_base") or ptable.PROVIDERS[name]["base_url"]
        try:
            check_provider(base, key)
            created = ui.db.set_provider(name, base, key)
        except ValueError as e:
            sess.flash("error", str(e))
            return form(sess, {"name": name, "api_base": base, "api_key_env": key if valid_key_name(key) else ""},
                        ui.db.get_provider(name) is None, 400)
        sess.flash("ok", f"Provider {name} {'added' if created else 'saved'}.")
        return redirect("/providers")

    @ui.post("/providers/delete")
    def provider_delete(req, sess, form_data):
        name = form_data.get("name", "")
        try:
            ok = ui.db.delete_provider(name)
            sess.flash("ok" if ok else "error", f"Provider {name} deleted." if ok else "No such provider.")
        except ValueError as e:
            sess.flash("error", str(e))
        return redirect("/providers")
