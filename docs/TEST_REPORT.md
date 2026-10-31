# kiko-lite Test Report

**Date:** 2025-10-31
**OS:** Q4OS (Trinity DE / TDE)
**Python:** 3.13.5
**Project:** kiko-lite - Anthropic-to-provider proxy (stdlib only)

---

## Summary

| Metric | Value |
|--------|-------|
| **Total Tests** | **237** |
| **Passed** | **237** |
| **Failed** | 0 |
| **Errors** | 0 |
| **Test Files** | 6 |
| **Coverage** | Core modules: app, db, resolver, providers (OpenAI/Gemini), health, keystore |

All tests use **Python stdlib only** (`unittest`, `unittest.mock`) — no external dependencies.

---

## Test Files

| File | Tests | Module Under Test |
|------|-------|-------------------|
| `tests/test_keystore.py` | **54** | `kikolib.keystore` — **TDE Wallet (DCOP)**, KWallet, secret-tool, Keychain, WCM |
| `tests/test_resolver.py` | 43 | `kiko.resolver` — model resolution, key validation, billing, cap gate |
| `tests/test_db.py` | 36 | `kiko.db` — SQLite schema, models, aliases, settings, billing |
| `tests/test_providers.py` | 60 | `kiko.providers` — OpenAI-compat & Gemini translation, streaming |
| `tests/test_app.py` | 32 | `kiko.app` — gateway, auth, SSE, failover, cooldowns |
| `tests/test_health.py` | 12 | `kiko.health` — background prober, backoff logic |

---

## TDE Wallet / DCOP Coverage (Priority)

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

## Other Critical Areas

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

## Running Tests

```bash
cd /home/petark/kiko/kiko
PYTHONPATH=/home/petark/kiko/kiko python3 -m unittest discover -s tests -v
```

Or individual modules:
```bash
PYTHONPATH=/home/petark/kiko/kiko python3 -m unittest tests.test_keystore -v
PYTHONPATH=/home/petark/kiko/kiko python3 -m unittest tests.test_resolver -v
# etc.
```

---

## Notes

- **No external dependencies** — all tests use stdlib `unittest` + `unittest.mock`
- **No pytest** required
- **No network calls** — all pure unit tests with mocks
- **TDE/DCOP untested on live system** — tests verify the *logic* matches the documented DCOP protocol (kded kwalletd → kwalletd fallback, open/readPassword/writePassword/removeEntry/close sequence)
- Q4OS Trinity DE uses KDE3-era DCOP; `dcop` binary must be on PATH for `DcopStore` probe to pass