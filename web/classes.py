# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Model classes and request-name aliases (glob -> target)."""

from __future__ import annotations

from . import render as r
from .ui import redirect


def register(ui) -> None:  # ui: web.ui.WebUI
    @ui.get("/classes")
    def classes_list(req, sess):
        rows = [[c["name"], c["keyword"], c["role"], c["models"],
                 r.inline_post("/classes/delete", sess.csrf, "Delete", "danger", name=c["name"])]
                for c in ui.db.classes()]
        add = r.form("/classes/add", sess.csrf,
                     r.field("Name", "name", placeholder="VOICE", required=True),
                     r.field("Keyword (lower case, 3+ letters)", "keyword", placeholder="voice", required=True),
                     r.field("Role", "role", placeholder="Voice models"),
                     r.button("Add class"))
        body = r.join([r.table(["Name", "Keyword", "Role", "Enabled models", ""], rows),
                       r.tag("h2", "Add a class"), add])
        return ui.render(sess, "Model classes", body, "/classes")

    @ui.post("/classes/add")
    def class_add(req, sess, form):
        name = form.get("name", "").strip().upper()
        try:
            ui.db.add_class(name, form.get("keyword", "").strip().lower(), form.get("role", "").strip())
            sess.flash("ok", f"Class {name} added.")
        except ValueError as e:
            sess.flash("error", str(e))
        return redirect("/classes")

    @ui.post("/classes/delete")
    def class_delete(req, sess, form):
        name = form.get("name", "")
        try:
            ok = ui.db.delete_class(name)
            sess.flash("ok" if ok else "error", f"Class {name} deleted." if ok else "No such class.")
        except ValueError as e:
            sess.flash("error", str(e))
        return redirect("/classes")

    @ui.get("/aliases")
    def aliases_list(req, sess):
        rows = [[g, t, r.inline_post("/aliases/delete", sess.csrf, "Delete", "danger", glob=g)]
                for g, t in ui.db.aliases()]
        add = r.form("/aliases/set", sess.csrf,
                     r.field("Glob pattern", "glob", placeholder="claude-*", required=True),
                     r.field("Target model", "target", placeholder="gemini-flash", required=True),
                     r.button("Save alias"))
        body = r.join([r.table(["Glob", "Target", ""], rows), r.tag("h2", "Add or change an alias"), add])
        return ui.render(sess, "Aliases", body, "/aliases")

    @ui.post("/aliases/set")
    def alias_set(req, sess, form):
        glob, target = form.get("glob", "").strip(), form.get("target", "").strip()
        if glob and target:
            ui.db.set_alias(glob, target)
            sess.flash("ok", f"Alias {glob} saved.")
        else:
            sess.flash("error", "glob and target are required.")
        return redirect("/aliases")

    @ui.post("/aliases/delete")
    def alias_delete(req, sess, form):
        glob = form.get("glob", "")
        ok = ui.db.delete_alias(glob)
        sess.flash("ok" if ok else "error", f"Alias {glob} deleted." if ok else "No such alias.")
        return redirect("/aliases")
