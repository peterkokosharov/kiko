# kiko-lite Test Architecture (3-Tier)

**Date:** 2025-10-31  
**Hardware:** HP EliteBook x360 11 G3 EE (8GB RAM)  
**OS:** Q4OS (Trinity DE / TDE)  
**Python:** 3.13.5  
**Node:** 20.19.2

---

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    kiko-lite Test Suite                     │
├─────────────────┬──────────────────┬────────────────────────┤
│    kikolib/     │    kiko_lite/    │       web-ui/          │
│  (core lib)     │   (proxy core)   │     (admin UI)         │
├─────────────────┼──────────────────┼────────────────────────┤
│  0 tests*       │    237 tests     │      16 tests          │
│  (stdlib)       │    (stdlib)      │     (Node --test)      │
└─────────────────┴──────────────────┴────────────────────────┘
Total: **253 tests** — all stdlib / zero external deps
```

*kikolib tests are integrated into kiko_lite tests via `kikolib` module exposure*

---

## Tier 1: kikolib (Core Library)

**Location:** `/home/petark/kiko_lite/kikolib/`  
**Tests:** Integrated into `kiko_lite/tests/test_keystore.py` (54 tests)

| Module | Description | Test Coverage |
|--------|-------------|---------------|
| `server.py` | Threaded HTTP/1.1 server + router | Via integration tests |
| `http_client.py` | HTTP client with retries/streaming | Via integration tests |
| `keystore.py` | TDE Wallet (DCOP), KWallet, secret-tool, Keychain, WCM | **54 dedicated tests** |
| `retry.py` | Retry policies with backoff | Via integration tests |
| `factory.py` | Provider factory registration | Via integration tests |
| `dotenv.py` | .env file loader | Via integration tests |
| `sse.py` | Server-Sent Events parser | Via integration tests |
| `tokens.py` | Token counting | Via integration tests |
| `yaml_lite.py` | Minimal YAML parser | Via integration tests |
| `posix_utils.py` / `native_utils.py` | Platform helpers | Via integration tests |

### Keystore Tests (Priority for Q4OS/TDE)

The **keystore tests** are the most important for Q4OS/TDE. `test_keystore.py` has **10 dedicated tests** for `DcopStore`:

| Test | Description |
|------|-------------|
| `test_dcop_open_success_kded` | Primary target: `dcop kded kwalletd open kdewallet 0` |
| `test_dcop_open_fails_kded_tries_kwalletd` | Fallback: `dcop kwalletd open kdewallet 0` |
| `test_dcop_both_targets_fail` | Both targets fail → returns `None` |
| `test_dcop_open_returns_invalid_handle` | Non-numeric handle → tries next target |
| `test_dcop_read_returns_minus_one` | `-1` from `readPassword` = not found |
| `test_dcop_read_returns_empty` | Empty string from `readPassword` = not found |
| `test_dcop_close_called_even_on_read_failure` | `close()` in `try/finally` for both targets |
| `test_dcop_set_success` | `writePassword` returns `"0"` = success |
| `test_dcop_set_fails_returns_nonzero` | Non-zero return = failure |
| `test_dcop_delete_success` | `removeEntry` returns `"0"` = success |

**Key implementation details tested:**
- Keys are uppercased (`TEST_KEY` not `test_key`)
- Subprocess calls use list args (no shell), 3s timeout
- Errors swallowed, secrets never logged
- Both DCOP targets tried: `["kded", "kwalletd"]` then `["kwalletd"]`

---

## Tier 2: kiko_lite (Proxy Core)

**Location:** `/home/petark/kiko_lite/`  
**Tests:** `/home/petark/kiko_lite/tests/` (237 tests, 6 files)

| File | Tests | Module Under Test |
|------|-------|-------------------|
| `test_keystore.py` | **54** | `kikolib.keystore` — TDE Wallet (DCOP), KWallet, secret-tool, Keychain, WCM |
| `test_resolver.py` | 43 | `kiko_lite.resolver` — model resolution, key validation, billing, cap gate |
| `test_db.py` | 36 | `kiko_lite.db` — SQLite schema, models, aliases, settings, billing |
| `test_providers.py` | 60 | `kiko_lite.providers` — OpenAI-compat & Gemini translation, streaming |
| `test_app.py` | 32 | `kiko_lite.app` — gateway, auth, SSE, failover, cooldowns |
| `test_health.py` | 12 | `kiko_lite.health` — background prober, backoff logic |

### Resolver (43 tests)
- Model resolution: aliases → class keywords → env fallback → direct name
- `key_name_problem()`: detects key VALUES vs NAMES (gsk_, sk-, AIza, nvapi- prefixes)
- `map_row()`: provider prefix mapping, custom endpoints, bare OpenAI models
- Billing writer: costs, errors, disable_billing setting
- Cap gate: monthly spending cap enforcement

### Database (36 tests)
- Schema migration to version 2 (model_classes, prices, billing extras)
- CRUD for models, aliases, settings
- Billing with extra fields (attempts, latency_ms, upstream_model, error)
- Model classes: add, duplicate detection, keyword overlap prevention
- Case-insensitive alias collision detection

### Providers (60 tests)
- **OpenAI-compat** (NVIDIA, Groq, DashScope, OpenAI, Mistral):
  - Request translation: system, tools, tool_choice, temperature, streaming
  - Response parsing: text, tool_calls, usage, stop_reason
  - Streaming: content_block_delta, thinking, tool_use, message_delta
- **Gemini** (AI Studio native):
  - Request: systemInstruction, contents, tools, thinkingConfig
  - Thought signatures in tool_use IDs (`toolu_...__thought__<sig>`)
  - Schema sanitization: refs, enum, anyOf, nullable
  - Streaming: functionCall, thought, text parts

### App/Gateway (32 tests)
- `valid_model_id()`: path traversal prevention, Gemini single-segment rule
- `SimpleResolver`: alias globs, class keywords, env vars
- Error mapping: 429→rate_limit, 500→api_error (not in OVERLOADED), 401/403→502
- Cooldowns: mark, clear, expiry, mark_ok/since_ok
- SSE encoding, ping, error events

### Health Prober (12 tests)
- Interval parsing: default 300s, min 10s, 0/negative = off
- Probe filtering: skip OK, disabled, health_no_probe, paid+unknown, cooling, recent live success
- Backoff: interval → 2× → max 6h on failure, reset on success
- None verdict = backoff only, no status change

---

## Tier 3: web-ui (Admin UI)

**Location:** `/home/petark/kiko_lite/web-ui/`  
**Tests:** `/home/petark/kiko_lite/web-ui/static/tests/api.test.js` (16 tests, Node.js)

| Test Suite | Tests | Coverage |
|------------|-------|----------|
| `auth` | 2 | login, logout, session validation |
| `models` | 4 | CRUD operations |
| `classes` | 3 | CRUD operations |
| `billing` | 1 | Summary query |
| `settings` | 2 | Get/set operations |
| `aliases` | 3 | CRUD operations |
| `key-status` | 1 | Key source detection |

**Technology:** Vanilla JS ES modules, `node --test` (no framework), runs against live server on port 4008.

---

## Running Tests

### All Tiers (from kiko_lite root)

```bash
# Tier 1 + 2: Python stdlib tests
cd /home/petark/kiko_lite
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest discover -s tests -v

# Tier 3: Node.js tests (requires web-ui server running on 4008)
cd /home/petark/kiko_lite/web-ui/static/tests
node --test api.test.js
```

### Individual Modules

```bash
# Python modules
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest tests.test_keystore -v
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest tests.test_resolver -v
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest tests.test_db -v
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest tests.test_providers -v
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest tests.test_app -v
PYTHONPATH="/home/petark:/home/petark/kiko_lite/kikolib" python3 -m unittest tests.test_health -v
```

### Quick Test (Core Only)

```bash
# Run just the core test script
cd /home/petark/kiko_lite && ./tests/run_tests.sh
```

---

## CI / Automation Notes

- **No external dependencies** — all tests use stdlib only
- **No pytest** required — pure `unittest`
- **No network calls** — all pure unit tests with mocks
- **TDE/DCOP untested on live system** — tests verify the *logic* matches documented DCOP protocol
- **Node tests require running server** — start with `/home/petark/kiko_lite/start-web.sh` first

---

## Hardware / Environment Log

| Date | OS | Python | Node | Hardware | Result |
|------|-----|--------|------|----------|--------|
| 2025-10-31 | Q4OS Trinity | 3.13.5 | 20.19.2 | HP EliteBook x360 11 G3 EE (8GB RAM) | 253 pass / 0 fail |

*Next: Ubuntu test run (document results here)*