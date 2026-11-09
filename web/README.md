<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com> -->
<!-- LLM: Anthropic Opus -->
# kiko web admin UI

Server-rendered HTML, Python standard library only (on `kikolib.server` and the `kiko_lite` data layer).
Plain forms, full page reloads, no framework. JavaScript is used only for the live request log
(`static/live.js`, EventSource + `textContent`); everything else works with JavaScript off.

## Start

From the repo root:

    python -m web                     # http://127.0.0.1:4001
    python -m web --host 127.0.0.1 --port 4001 --db path/to/kiko.db

Host and port come from `kiko.toml` in the config folder (`$KIKO_CONFIG_DIR`, else `~/.kiko`; see
`kikolib/config.py`): `[web] host` and `[web] port`, else `[kiko] base_port` + 1 (4000 + 1 = 4001 by default;
the proxy is `base_port`, spy `base_port` + 2). A flag overrides the file. (Before TASK #124 the default was the
preview port 4080.) A non-loopback `--host` needs the names used in the browser in `KIKO_ALLOWED_HOSTS` (comma
separated). The DB is the one of kiko-lite (`--db`, `KIKO_DB_PATH`, `[database] path`, else `kiko.db` in the
config folder).

Login: the admin password (one user). It is the same hash (`webui_admin_hash` setting) the old `web-ui/` uses.
On a first start the password is `admin` (only "first start: login with the default password, then change it" is
printed). A session that logged in with the default password is redirected to `/password` from every page (only
`/password` and logout work) until it is changed. `python -m web` refuses to start on a non-loopback `--host` while
the stored password is still the default. An existing hash (e.g. from the earlier random password) is never reset;
a leftover `web-initial-password` file in the config folder is removed on the first successful password change.

## Pages

Dashboard, Models (add/edit/enable/disable/delete; key NAME and where it is found: env, store, dotenv, never the
value), Providers (add/edit/delete), Classes, Aliases, Billing (per model, period, probes), Logs (paged, with live
view), Text log, Settings, Password.

The Text log lists every stored exchange, newest first (paged): time, model the client asked for, upstream model,
status, a short prompt preview and answer preview. "Open" shows `/responses/<id>` with the full prompt (parameters,
system, tool names, every message) and the full answer, escaped and pre-wrapped. The proxy writes these rows to the
`responses` table of kiko.db, with the same id as `billing.request_id`; headers and keys are never stored. Turn it off
with `[logs] store_text = false`.

## Settings page

A typed form, grouped (Servers, Billing, Health, Logs), that edits `kiko.toml` and nothing else; the fields and
their help texts come from the one table `kikolib.config.SETTINGS`, and every field is read by the code named under
it (proxy, web, health prober, log pruner). Save validates (ports 1-65535, hosts, numbers) and writes the file
through `kikolib/tomlwrite.py`, which keeps comments and order and replaces the file atomically. An empty box
removes the key (default). A field you did not change is not written into the file. "Restart needed" appears when the
file holds a server value (host, port, no-auth) that differs from what this web process started with. The page also
shows the ready `ANTHROPIC_BASE_URL=http://<proxy host>:<proxy port>` line for clients. `[spy]` is shown but spy is
not built yet. `proxy.no_auth` is refused together with a non-loopback proxy host. The raw key=value editor is gone;
the four old DB settings (monthly cap, billing off, health interval, health skip list) are read from the file first (an old DB value is shown in its box, marked "from DB", and kept on save; a cap of 0 means no cap)
and from the database only when the file has no such key.

```toml
[kiko]
base_port = 4000        # proxy 4000, web 4001, spy 4002
[logs]
retention_days = 30     # prune billing rows and stored responses older than 30 days (proxy start, then daily)
store_text = true       # keep the prompts and answers in kiko.db (the Text log); false = store nothing
file = "logs/kiko.log"  # proxy and web also log to this file (relative to the config folder)
```

The model form uses list boxes: Provider (from the `providers` table, schema v4) and Class (from `model_classes`).
The model id is text (a `<datalist>` suggests the known ids); it is stored as `<provider>/<model id>`, as the CLI's
`--provider` does. API base and API key name come from the provider and are shown as text; an optional override
(a base URL, or a key name from a list of the names already in use) is in a closed-by-default section. There is no
Status field: the health prober sets the status. Every API base that is stored (a provider's or a model override) must be a
plain https URL (http only to localhost) without credentials, query or fragment. kiko-lite itself fixes the API
base for gemini, nvidia, groq, dashscope and mistral, so those are shown read-only ("fixed by kiko for this
provider") and an override is refused for them; it is used only for openai and for providers you add yourself. A
model of a provider you added is stored as `<provider>/<id>` with the provider's base and the bare `<id>` is what is
sent upstream. A provider that is in use by a model cannot be deleted.

## Security

- Every interpolated value goes through `html.escape(quote=True)` (`render.py`: values are escaped unless they are
  `Markup` built by the helpers).
- Session cookie `HttpOnly; SameSite=Strict`; a CSRF token in every POST form plus an Origin check (an Origin must
  equal `http://<Host>`; a refusal is an HTML 403 page); Host allow-list against DNS rebinding.
- Headers on every response: `Content-Security-Policy` (`default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self'; frame-ancestors 'none'; form-action 'self'`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: same-origin` (not `no-referrer`: browsers send `Origin: null` on form POSTs under
  that policy, which the Origin check must refuse), `Cache-Control: no-store`.
- Login: PBKDF2-HMAC-SHA256, 600000 iterations for new hashes (older hashes keep verifying), 5 failures per minute
  then 429. Passwords and key values are never printed, logged or shown.

## Layout

`ui.py` (routing, login, static) · `security.py` · `render.py` · `dashboard.py` · `models.py` · `providers.py` · `classes.py`
(classes + aliases) · `billing.py` (billing, logs, text log) · `settings.py` · `static/style.css`, `static/live.js`.

Tests: `.venv/Scripts/python -m pytest tests/kiko/test_web_ui.py`.

The spy (live request and server log viewer on base_port + 2) is documented in [spy/README.md](../spy/README.md).
