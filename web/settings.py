# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Settings (a typed form that edits kiko.toml; the table is kikolib.config.SETTINGS) and the admin password change."""

from __future__ import annotations

from kikolib import config as kconfig
from kikolib import tomlwrite

from . import render as r
from .security import COOKIE, HASH_KEY, MIN_PASSWORD, hash_password, set_cookie, verify_password
from .ui import redirect


def _show(setting, value) -> str:
    """Text of a value for an input box."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _toml_has(cfg, setting) -> bool:
    section = cfg.get(setting.section)
    return isinstance(section, dict) and setting.key in section


def register(ui) -> None:  # ui: web.ui.WebUI
    def _restart_notice(cfg):
        """Server values the file holds now that differ from what this web process started with."""
        now = kconfig.server_values(cfg)
        rows = [f"{name}: running with {ui.started.get(name)}, file says {now[name]}"
                for name in now if name in ui.started and ui.started[name] != now[name]]
        if not rows:
            return ""
        return r.tag("div", r.tag("p", "Restart needed: the file holds server values that differ from what this "
                                  "process started with. Restart the servers to use them.", class_="flash error"),
                     r.tag("ul", *[r.tag("li", x) for x in rows]))

    def _row(setting, cfg, db_settings, submitted):
        name = setting.name
        value, source = kconfig.effective(name, cfg, db_settings)
        from_file = source == "file"
        info = [setting.help, " ", r.tag("span", f"Default: {setting.shown_default}.", class_="muted")]
        if source == "db":
            info.append(r.tag("span", " (from DB: the value in the box is the old value of the database; saving "
                              "keeps it, change it to move it to the file).", class_="muted"))
        info.append(r.tag("span", f" Used by: {setting.used_by}.", class_="muted"))
        if setting.type == "bool":
            checked = (submitted.get(name) == "on") if submitted is not None else bool(value)
            box = r.void("input", type="checkbox", name=name, id="f-" + name, checked=checked)
            return r.tag("p", r.tag("label", setting.key, for_="f-" + name), r.tag("span", box, " ", *info))
        if submitted is not None:
            shown = submitted.get(name, "")
        else:
            shown = _show(setting, value) if source in ("file", "db") else ""  # a DB value is shown IN the box
        placeholder = "" if source in ("file", "db") else _show(setting, value)  # what applies while it is empty
        box = r.void("input", type="text", name=name, value=shown, id="f-" + name, placeholder=placeholder or None)
        return r.tag("p", r.tag("label", f"{setting.key}", for_="f-" + name), r.tag("span", box, " ", *info))

    def _form(sess, submitted=None):
        cfg = kconfig.load_config()
        db_settings = ui.db.settings()
        groups = []
        for group in kconfig.GROUPS:
            rows = [_row(st, cfg, db_settings, submitted) for st in kconfig.SETTINGS if st.group == group]
            names = sorted({st.section for st in kconfig.SETTINGS if st.group == group})
            groups.append(r.tag("fieldset", r.tag("legend", f"{group} ({', '.join('[' + n + ']' for n in names)})"),
                                *rows))
        return r.form("/settings/save", sess.csrf, *groups, r.button("Save"))

    def _page(sess, submitted=None, status=200):
        cfg = kconfig.load_config()
        url = f"http://{kconfig.proxy_host(cfg)}:{kconfig.proxy_port(cfg)}"
        body = r.join([
            _restart_notice(cfg),
            r.tag("p", "Settings are stored in ", r.tag("code", str(kconfig.toml_path())),
                  ". Leave a box empty to use its default. A value that is not in the file keeps its default (or "
                  "its old value from the database)."),
            r.tag("h2", "For clients"),
            r.tag("p", "Claude Code and other Anthropic clients: ", r.tag("code", f"ANTHROPIC_BASE_URL={url}")),
            _form(sess, submitted),
        ])
        return ui.render(sess, "Settings", body, "/settings", status=status)

    @ui.get("/settings")
    def settings_page(req, sess):
        return _page(sess)

    @ui.post("/settings/save")
    def settings_save(req, sess, form):
        cfg = kconfig.load_config()
        db_settings = ui.db.settings()
        errors: list[str] = []
        parsed: dict[str, object] = {}
        for st in kconfig.SETTINGS:
            raw = "on" if st.type == "bool" and form.get(st.name) == "on" else ("" if st.type == "bool"
                                                                              else form.get(st.name, ""))
            try:
                if st.type == "bool":
                    parsed[st.name] = raw == "on"
                else:
                    parsed[st.name] = kconfig.parse_value(st, raw)
            except ValueError as e:
                errors.append(str(e))
        if not errors:
            host = parsed.get("proxy.host") or kconfig.proxy_host(cfg)
            refusal = kconfig.check_no_auth(str(host), bool(parsed.get("proxy.no_auth")))
            if refusal:
                errors.append(refusal)
        if errors:
            for e in errors:
                sess.flash("error", e)
            return _page(sess, submitted=dict(form), status=400)
        changes: dict[tuple[str, str], object] = {}
        for st in kconfig.SETTINGS:
            new = parsed[st.name]
            stored = _toml_has(cfg, st)
            if new is None:  # empty box: no value in the file (default, or the old DB value when there is one)
                if stored:
                    changes[(st.section, st.key)] = None
                continue
            current, _src = kconfig.effective(st.name, cfg, db_settings)
            if not stored and new == current:  # unchanged: do not pin a default or an old DB value into the file
                continue
            changes[(st.section, st.key)] = new
        try:
            tomlwrite.update_file(kconfig.toml_path(), changes)
        except (tomlwrite.TomlWriteError, OSError) as e:
            sess.flash("error", f"Not saved: {e}")
            return _page(sess, submitted=dict(form), status=400)
        sess.flash("ok", f"Settings saved to {kconfig.toml_path()}." if changes else "Nothing changed.")
        return redirect("/settings")

    def _password_form(sess):
        return r.form("/password", sess.csrf,
                      r.field("Current password", "current", kind="password", required=True,
                              autocomplete="current-password"),
                      r.field(f"New password (at least {MIN_PASSWORD} characters)", "new", kind="password",
                              required=True, minlength=MIN_PASSWORD, autocomplete="new-password"),
                      r.field("Repeat new password", "again", kind="password", required=True,
                              autocomplete="new-password"),
                      r.button("Change password"))

    def _password_page(sess, status=200):
        body = _password_form(sess)
        if sess.must_change:
            notice = r.tag("p", "You logged in with the default password. Set a new one to continue.",
                           class_="flash error")
            body = r.join([notice, body])
        return ui.render(sess, "Change password", body, "/password", status=status)

    @ui.get("/password")
    def password_form(req, sess):
        return _password_page(sess)

    @ui.post("/password")
    def password_change(req, sess, form):
        new = form.get("new", "")
        if not verify_password(form.get("current", ""), ui.auth.stored_hash()):
            error = "Current password is wrong."
        elif len(new) < MIN_PASSWORD:
            error = f"The new password needs at least {MIN_PASSWORD} characters."
        elif new != form.get("again", ""):
            error = "The two new passwords differ."
        else:
            ui.db.set_setting(HASH_KEY, hash_password(new))
            ui.auth.end_others(sess.token)
            if ui.password_file is not None:  # the first-start password is obsolete now
                try:
                    ui.password_file.unlink()
                except OSError:
                    pass
            sess.must_change = False
            fresh = ui.auth.rotate(sess.token)  # new session and CSRF token
            if fresh is None:
                return redirect("/login")
            fresh.flash("ok", "Password changed; other sessions were ended.")
            return redirect("/password", {"Set-Cookie": set_cookie(COOKIE, fresh.token, int(ui.auth.ttl))})
        sess.flash("error", error)
        return _password_page(sess, 400)
