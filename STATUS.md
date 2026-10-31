# kiko-lite Project Status (2026-10-31)

## Repository
- **GitHub**: https://github.com/peterkokosharov/kiko (public, read-only for others)
- **Structure**: `kiko_lite/` (main), `kikolib/` (stdlib lib), `web-ui/` (admin UI)

## Running Services

| Service | Port | Command | Status |
|---------|------|---------|--------|
| kiko-lite proxy | 4002 | `/home/petark/kiko-lite` | ✅ Works - 6 models |
| web-ui admin | 4008 | `/home/petark/kiko_lite/start-web.sh` | ✅ Works - all endpoints |

## Configured Models (in ~/.kiko/kiko.db)
- gemini-flash-lite (Gemini, $0.30/$2.50/Mtok)
- gemini-flash (Gemini, $0.75/$3.75/Mtok) 
- nemotron-ultra (NVIDIA, no price)
- nemotron-super (NVIDIA, no price)
- groq-llama (Groq, no price)
- mistral-small (Mistral, no price)

## Keys
- Stored in `/home/petark/.kiko/.env` (8 keys, loaded via dotenv)
- Key names: GEMINI_GEMINI_FLASH_LITE_API_KEY, etc.

## web-ui (port 4008) - Complete
**Done:**
- Auth system: login/logout/change-password, cookie + Bearer token, default admin/admin
- Modular JS: `core/api.js`, `core/auth.js`, `core/utils.js`, `components/modal.js`, `components/table.js`
- Modules: `models.js`, `classes.js`, `billing.js`, `settings.js`, `aliases.js` (lazy-loaded)
- Templates: login modal, change-password modal, 5 tabs
- Tests: 16 API integration tests passing

**Fixed:**
- Static file serving (/static/*.js, /static/*.css) - router extended to support `{path:path}` syntax
- DELETE /api/classes endpoint - added `delete_class` method to Database

## Test Architecture (3 Tiers)

**Total: 253 tests** — all stdlib / zero external deps

| Tier | Location | Tests | Framework |
|------|----------|-------|-----------|
| **1. kikolib** | `kikolib/` + `tests/test_keystore.py` | 54 (keystore) | Python `unittest` |
| **2. kiko_lite** | `kiko_lite/tests/` | 237 | Python `unittest` |
| **3. web-ui** | `web-ui/static/tests/` | 16 | Node.js `--test` |

### Tier 1: kikolib (Core Library)
- Integrated via `kikolib` module exposed from `kiko_lite/__init__.py`
- Priority: `test_keystore.py` - 54 tests for TDE Wallet (DCOP), KWallet, secret-tool, Keychain, WCM
- DCOP tests cover: kded+kwalletd → kwalletd fallback, open/readPassword/writePassword/removeEntry/close

### Tier 2: kiko_lite (Proxy Core)
| File | Tests | Module |
|------|-------|--------|
| `test_keystore.py` | 54 | `kikolib.keystore` |
| `test_resolver.py` | 43 | `kiko_lite.resolver` |
| `test_db.py` | 36 | `kiko_lite.db` |
| `test_providers.py` | 60 | `kiko_lite.providers` |
| `test_app.py` | 32 | `kiko_lite.app` |
| `test_health.py` | 12 | `kiko_lite.health` |

### Tier 3: web-ui (Admin UI)
| Suite | Tests | Coverage |
|-------|-------|----------|
| `auth` | 2 | login, logout, session |
| `models` | 4 | CRUD |
| `classes` | 3 | CRUD |
| `billing` | 1 | Summary |
| `settings` | 2 | Get/set |
| `aliases` | 3 | CRUD |
| `key-status` | 1 | Key source detection |

## Running Tests

```bash
# All tiers
/home/petark/kiko_lite/run_all_tests.sh all

# Individual tiers
/home/petark/kiko_lite/run_all_tests.sh kiko_lite
/home/petark/kiko_lite/run_all_tests.sh web-ui

# Manual
# Tier 1+2
cd /home/petark/kiko_lite
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest discover -s tests -v

# Tier 3 (requires web-ui running on 4008)
cd /home/petark/kiko_lite/web-ui/static/tests
node --test api.test.js
```

## Documentation
- `docs/TEST_ARCHITECTURE.md` - Full 3-tier test documentation
- `docs/TEST_REPORT.md` - Historical test report (237 tests on Q4OS)

## Quick Start
```bash
# Proxy (port 4002)
/home/petark/kiko-lite

# Web UI (port 4008) - from anywhere
/home/petark/kiko_lite/start-web.sh

# Run all tests
/home/petark/kiko_lite/run_all_tests.sh all
```

## Hardware / Environment Log
| Date | OS | Python | Node | Hardware | Result |
|------|-----|--------|------|----------|--------|
| 2025-10-31 | Q4OS Trinity | 3.13.5 | 20.19.2 | HP EliteBook x360 11 G3 EE (8GB RAM) | 253 pass / 0 fail |

*Next: Ubuntu test run (document results here)*