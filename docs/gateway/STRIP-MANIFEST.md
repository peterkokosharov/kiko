# KIKO Strip Manifest

**Base:** litellm v1.104.2  
**Fork:** kiko-0.1.0  
**Purpose:** Record every directory and file category removed from the upstream so that future
litellm upgrades can be applied surgically — without accidentally re-introducing stripped
dependencies or losing KIKO-specific replacements.

---

## 1. Entire directories removed from the repository

| Path (relative to repo root) | Reason removed | Replacement in KIKO |
|------------------------------|---------------|---------------------|
| `litellm/proxy/enterprise/` | Enterprise-only features (SSO, SCIM, key rotation, advanced audit). Requires separate enterprise license and cloud backend. | Deleted — no stub. Enterprise column in `schema.prisma` references remain but are ignored at runtime. |
| `litellm/proxy/db/` | PostgreSQL/Prisma ORM layer (`PrismaClient`, spend-log writers, migration helpers). Hard dependency on `prisma` Python package and a live Postgres server. | Stub package (`__init__.py` re-exports `None` for transitive imports). Billing replaced by `kiko_sqlite_logger.py`. |
| `litellm/proxy/management_endpoints/` | REST endpoints for multi-tenant key/team/user management (`/key/generate`, `/team/`, `/user/`, etc.). Required Postgres and enterprise auth. | Stub package. These routes return 404 in `kiko_server.py`. |
| `litellm/proxy/guardrails/` | Guardrail framework (Lakera, Aporia, Presidio integrations). Required external guardrail services and Postgres for policy storage. | Stub package. |
| `litellm/proxy/ui_crud_endpoints/` | Web-UI CRUD REST endpoints used by the LiteLLM dashboard SPA. Required Postgres and the upstream React build. | Stub package. No UI in KIKO. |
| `litellm/proxy/_experimental/` | Experimental / unstable features not yet promoted to stable API. | Stub package. |
| `litellm-rust/` | Rust Cargo crate (`litellm-rust`) compiled via `maturin`. Used for performance-critical helpers. | Deleted. Build backend switched to `hatchling` (pure-Python). `litellm/rust_bridge/` Python shims remain but the native extension is never loaded. |

---

## 2. Files replaced (not just deleted)

| Original file | What it did | KIKO replacement |
|---------------|------------|------------------|
| `litellm/proxy/proxy_server.py` (~17 k lines) | Full proxy server: multi-tenant auth, Postgres startup, APScheduler background jobs, UI routes, spend management, 50+ route handlers. | `litellm/proxy/kiko_server.py` (~280 lines) — minimal FastAPI server: health, models, chat/completions only. |
| `litellm/proxy/auth/user_api_key_auth.py` (original, ~800 lines) | Multi-tenant API key auth against Postgres; JWT, OAuth2, SCIM. | `litellm/proxy/auth/user_api_key_auth.py` (~45 lines) — single `MASTER_KEY` env-var check only. |

---

## 3. Files excluded from the wheel (present in source, not shipped)

These files exist in the source tree but must be excluded from the wheel because they are
developer-facing artefacts with no runtime value.

| Pattern | Files matched | Size | Reason |
|---------|--------------|------|--------|
| `litellm/proxy/example_config_yaml/**` | 33 files | ~52 KB | Sample proxy YAML configs for documentation; no runtime code. |
| `litellm/proxy/test_prompts/**` | 1 file | <1 KB | Test prompt fixture (`test_hello_world_prompt.prompt`). |
| `litellm/integrations/test_httpx.py` | 1 file | <1 KB | Integration test file accidentally placed under the package tree. |
| `litellm/proxy/start.sh` | 1 file | <1 KB | Upstream shell launcher; superseded by `kircho-proxy` entry point. |
| `litellm/integrations/azure_sentinel/example_standard_logging_payload.json` | 1 file | ~5 KB | Developer reference payload. |
| `litellm/proxy/hooks/example_presidio_ad_hoc_recognizer.json` | 1 file | <1 KB | Developer reference JSON. |

**Add these lines to `[tool.hatch.build.targets.wheel] exclude` in `pyproject.toml`:**

```toml
"litellm/proxy/example_config_yaml/**",
"litellm/proxy/test_prompts/**",
"litellm/integrations/test_httpx.py",
"litellm/proxy/start.sh",
"litellm/integrations/azure_sentinel/example_standard_logging_payload.json",
"litellm/proxy/hooks/example_presidio_ad_hoc_recognizer.json",
```

---

## 4. Files excluded by hatchling automatically (source exists, not in wheel)

These three files are in source but hatchling silently drops them (likely matched by
`.gitignore` patterns):

| File | Why excluded |
|------|-------------|
| `litellm/proxy/_new_secret_config.yaml` | `_*secret*` matched by `.gitignore` |
| `litellm/proxy/_new_new_secret_config.yaml` | Same |
| `litellm/proxy/_super_secret_config.yaml` | Same |

These are local dev/test configs containing personal model and key references. They must
**not** be committed to any branch intended for distribution.

---

## 5. Features unavailable in KIKO as a result of stripping

Future developers porting a new litellm feature should check this table first. If the
feature touches any of the listed areas, the stripped layer must be partially restored
before the port can work.

| Feature area | Unavailable in KIKO | Restore path |
|-------------|---------------------|-------------|
| **PostgreSQL persistence** | Key/team/user storage, spend logs, budget resets, batch/responses cost tracking | Restore `proxy/db/` and add `prisma` dep; wire `PrismaClient` back into server startup. |
| **Redis caching** | LLM response cache, rate-limit counters, TPM/RPM tracking, deployment cooldowns, spend-increment queue | Add `redis` dep; restore `DualCache` initialisation in server startup. |
| **Multi-tenant auth** | `/key/generate`, team budgets, per-user rate limits, JWT/OAuth2/SCIM | Restore `proxy/management_endpoints/` and original `user_api_key_auth.py`. |
| **Enterprise features** | SSO, key rotation, advanced audit logs, SCIM provisioning | Restore `proxy/enterprise/` (requires upstream enterprise source access). |
| **Web UI** | LiteLLM dashboard SPA, CRUD endpoints for keys/models | Restore `proxy/ui_crud_endpoints/` and rebuild the React frontend. |
| **Guardrails** | Lakera, Aporia, Presidio content filtering | Restore `proxy/guardrails/` and add guardrail service deps. |
| **APScheduler background jobs** | Spend flush, budget reset, deployment sync, health checks | Restore scheduler initialisation from `proxy_server.py` startup block. |
| **Rust performance extensions** | Any optimisation provided by the `litellm-rust` crate | Restore `litellm-rust/`, switch build backend back to `maturin`, install Rust toolchain. |
| **Batch / Responses cost tracking** | `check_batch_cost` and `check_responses_cost` enterprise background jobs | Restore `enterprise/litellm_enterprise/proxy/common_utils/`. |

---

## 6. KIKO additions (not in upstream litellm)

These files were written for KIKO and have no upstream equivalent. They must be preserved
or forward-ported when upgrading the litellm base.

| File | Purpose |
|------|---------|
| `litellm/proxy/kiko_server.py` | Minimal FastAPI server entry point |
| `litellm/proxy/integrations/kiko_sqlite_logger.py` | SQLite billing logger → `kircho.db` |
| `litellm/proxy/auth/user_api_key_auth.py` | Single-key auth shim |
| `tests/kiko/test_kiko_sqlite_logger.py` | 9 unit tests for billing logger |
| `tests/kiko/test_kiko_config_loader.py` | 6 unit tests for config loader |
| `tests/kiko/test_kiko_proxy.py` | 6 integration tests for proxy |
| `tests/kiko/conftest.py` | Integration test fixtures |
| `kiko-config.yaml` | Seed YAML for initial DB population |
| `docs/STRIP-MANIFEST.md` | This file |

---

*Last updated: 2026-10-08 — KIKO session 25-26.*
