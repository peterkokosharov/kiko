# kikolib

Small, standard-library-only library for kiko-lite (a stdlib proxy so Claude Code can talk to
NVIDIA / Groq / Gemini / Qwen / OpenAI). Version 0.1.0. Python >= 3.11 (import fails earlier).
Primary target Linux (Debian 12, Q4OS, Mint); Windows also works.

Inventory of the old third-party dependencies: [`docs/kikolib/INVENTORY.md`](../docs/kikolib/INVENTORY.md).

## Rules

1. **Standard library only.** A module imports only the stdlib and other kikolib modules.
2. **Threads, no asyncio.** No module uses asyncio (the guard test still tolerates it in `http.py` / `server.py`).
3. **Pluggable parts = interface + factory.** `factory.register(kind, name, probe=, load=, priority=)`;
   `factory.choose(kind)` takes the override argument, else env `KIKO_<KIND>_IMPL`, else the first
   probe-ok by priority. `factory.report()` feeds `kiko doctor`.
4. **Native code only from the OS** (ctypes on system libraries, subprocess on system binaries).
5. **No tests inside kikolib**; tests live in `tests/kiko/test_kikolib_*.py`.
6. **Headers**: every file starts with `# SPDX-License-Identifier: MIT` and
   `# Author: Peter Kokosharov <peter.kokosharov@gmail.com>`.

## Modules

| Module | Purpose | Status |
|---|---|---|
| `factory.py` | thread-safe implementation registry and chooser | done |
| `keystore.py` | API keys: env var first, then OS store (wcm, keychain, secret-tool, kwallet, dcop) | done (dcop unverified) |
| `posix.py` | signals, RSS, sd_notify, watchdog, private files | done |
| `dotenv.py` | `.env` reader | done |
| `tokens.py` | fallback token estimate (not a tokenizer) | done |
| `yaml_lite.py` | read-only YAML subset; only for the one-time import of an old `kiko.yaml` | done |
| `native.py` | reserved for a libcurl (ctypes) http backend, only if a soak test needs it | empty |
| `http.py` | HTTP client on `http.client` + `ssl`: keep-alive pool, streamed SSE lines, proxy CONNECT, OS trust store | done |
| `retry.py` | retry policy: exponential backoff + jitter, `Retry-After` within a soft budget, `on_wait` slices for SSE pings | done |
| `server.py` | threaded HTTP server, router, `serve()` (signals, systemd, RSS limit, drain) | done |
| `sse.py` | SSE encode / ping / parser | done |

Known limits of `http.Response.abort()` (wakes a reader blocked on a silent upstream from another thread):
it is a no-op once the response is finished or closed; on Linux `shutdown()` wakes the read; on Windows a
plain socket is also handle-closed, but a TLS socket is only shut down (closing the handle under a thread in
`SSL_read` risks a handle-reuse race), so there the worker may live until the read timeout; a worker still
blocked in `getresponse()` (no response head yet) cannot be aborted and ends at the timeout. The caller's
own thread is never blocked by any of these. No TLS abort test exists (no stdlib way to make a test cert).

No config file long term: settings live in SQLite. `crypto.py` and `jsonschema_lite.py` were removed.
