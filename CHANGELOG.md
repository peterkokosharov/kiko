# Changelog

## [0.3.0] — 2026-10-14

### Added

- **Endpoints:**
  - `POST /v1/messages` — Anthropic Messages API (Claude Code integration)
  - `POST /v1/messages/count_tokens` — local token count, no upstream call
  - `POST /v1beta/models/{m}:generateContent` — Gemini generateContent
  - `POST /v1beta/models/{m}:streamGenerateContent` — Gemini streaming
  - `POST /v1beta/models/{m}:countTokens` — Gemini local token count
  - `POST /v1/responses` — OpenAI Responses API (Codex integration), streaming
- **Model aliases:**
  - YAML `model_aliases` section: glob patterns mapped to kiko model names
  - Env-var tier aliases: `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL`
- **Monthly cap:**
  - `general_settings.monthly_cap_usd` — hard per-calendar-month spending cap
  - Returns 429 in the client's native error format (Anthropic / Gemini / OpenAI)
- **Billing:**
  - `route` column added to billing table (migration: `ALTER TABLE billing ADD COLUMN route TEXT`)
  - Route-tagged rows for all endpoints (messages, gemini_generate, responses, etc.)
- **Docs:**
  - `docs/HOME-TEST.md` — live test checklist for all three client integrations
  - `docs/HOME-TEST.bg.md` — same checklist in Bulgarian (shlyokavitsa)
  - `docs/kiko-sample.yaml` — full annotated config reference

### Security

- `_prepare()` chokepoint enforced on every upstream request:
  - Strips `api_key`, `api_base`, `base_url`, `extra_headers`, `mock_response`,
    `fallbacks`, `headers`, `input_cost_per_token`, `output_cost_per_token`,
    `model_list`, `mock_tool_calls`, `mock_timeout` from client body
  - Fail-closed: config not loaded → 404 guard skipped, stripping still applied
  - `hmac.compare_digest` used for constant-time MASTER_KEY comparison
- `web_search` and `computer_use_preview` tool types stripped from Codex requests

---

## [0.2.0] — 2026-10-08

### Breaking

- CLI renamed: `kircho-proxy` → `kiko`. Update any start scripts or shortcuts.

### Changed

- Branding: "KIKO" everywhere; "LiteLLM Home Edition" and "Uvicorn" removed from all user-facing output
- `litellm.suppress_debug_info = True`: upstream BerriAI/LiteLLM debug lines no longer printed to console
- HTTP error details: upstream `litellm.ClassName:` prefix stripped before returning to clients
- Server startup: prints `KIKO running on http://<host>:<port>` before handing off to uvicorn; uvicorn log level set to warning; `Server:` response header disabled

---

## [0.1.0] — 2026-10-08

### Initial release — KIKO

**Base:** litellm v1.104.2 (frozen; selective cherry-picks planned)

### Removed from upstream litellm

- `proxy/enterprise/` — enterprise auth, SSO, RBAC (not needed)
- `proxy/db/` — PostgreSQL / Prisma ORM (replaced by SQLite)
- `proxy/management_endpoints/` — team, key, budget management API
- `proxy/guardrails/` — prompt injection filters
- `proxy/ui_crud_endpoints/` — internal dashboard API
- `proxy/_experimental/` — unreleased features
- `litellm-rust/` — Rust build extension
- `proxy/proxy_server.py` — 17 000-line entry point (replaced by `kiko_server.py`)
- `proxy/auth/user_api_key_auth.py` — 800-line auth module (replaced by 45-line master-key check)

### Added

- `proxy/integrations/kiko_sqlite_logger.py` — SQLite billing logger + models table
- `proxy/kiko_server.py` — minimal FastAPI proxy (280 lines)
- CLI: `--init-db`, `--list-models`, `--add-model`, `--delete-model`,
  `--enable-model`, `--disable-model`, `--discover`, `--costs`
- `kiko_build.py` — Maven-inspired build script (working / release modes)
- `docs/STRIP-MANIFEST.md` — full record of every removed litellm component

### Architecture decisions

- **DB-first model loading**: `kircho.db` is the single source of truth after `--init-db`;
  YAML is the fallback only on first run.
- **No Redis, no PostgreSQL**: all state lives in `~/.kircho/kircho.db`.
- **Python 3.12 only**: no legacy compatibility shims.
- **Host security**: binds to `127.0.0.1` by default; explicit `--host` required for LAN.
