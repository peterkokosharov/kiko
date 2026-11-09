# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Dashboard: a short overview with links to every area."""

from __future__ import annotations

from kikolib import config as kconfig

from . import render as r


def register(ui) -> None:  # ui: web.ui.WebUI
    @ui.get("/")
    def dashboard(req, sess):
        db = ui.db
        models = db.models()
        enabled = sum(1 for m in models if m["enabled"])
        cap = kconfig.cap_label(kconfig.overlay_settings(db.settings()).get("monthly_cap_usd"))
        week = db.billing_summary(7)
        rows = [[m["model"], m["calls"], m["tokens_in"], m["tokens_out"], f"{m['cost_usd'] or 0:.4f}"]
                for m in week[:5]]
        body = r.join([
            r.tag("ul", r.tag("li", f"Models: {enabled} enabled of {len(models)}"),
                  r.tag("li", f"Spent this month: ${db.month_spent():.4f}", f" (cap: {cap})"),
                  r.tag("li", f"Database: {db.path}")),
            r.tag("h2", "Top models, last 7 days"),
            r.table(["Model", "Calls", "Tokens in", "Tokens out", "Cost USD"], rows),
        ])
        return ui.render(sess, "Dashboard", body, "/")
