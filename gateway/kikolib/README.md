# kikolib

Small, standard-library-only library for kiko-lite (a stdlib proxy so Claude Code can talk to
NVIDIA / Groq / Gemini / Qwen / OpenAI). Version 0.2.0. Python >= 3.11 (import fails earlier).
Primary target Linux (Debian 12, Q4OS, Mint); Windows also works (launcher `kiko-lite.cmd`).

The wider role of kikolib as a shared base for kiko, rex, kircho and an agent system is described in
[`docs/STDLIB-2.md`](../../docs/STDLIB-2.md) (proposal, 2026-11-22).

## Rules

1. **Standard library only.** A module imports only the stdlib and other kikolib modules.
2. **Threads, no asyncio.** No module uses asyncio (the guard test still tolerates it in `http.py` / `server.py`).
3. **Pluggable parts = interface + factory.** `factory.register(kind, name, probe=, load=, priority=)`;
   `factory.choose(kind)` takes the override argument, else env `KIKO_<KIND>_IMPL`, else the first
   probe-ok by priority. `factory.report()` feeds `kiko doctor`.
4. **Native code only from the OS** (ctypes on system libraries, subprocess on system binaries).
5. **No tests inside kikolib**; tests live in the source repository (`tests/kiko/test_kikolib_*.py`, not in the kiko-lite zip).
6. **Headers**: every file starts with three lines: `# SPDX-License-Identifier: MIT`,
   `# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>` and
   `# LLM: <model names, comma separated>`.

## How other projects take kikolib

kikolib has one home: this repository (`gateway/kikolib`). Never fork or edit it elsewhere.

1. Copy the whole `kikolib/` folder, unmodified, from the release tag `kikolib-v0.2.0` (version = `kikolib.__version__`).
2. Keep its `SHA256SUMS` in the copy (sha256 of every file, LF endings, sorted; `.gitattributes` forces `eol=lf`).
   Add `<dir>/kikolib/** text eol=lf` to the consumer's own `.gitattributes`, otherwise Windows `autocrlf`
   checkouts change the bytes and the hash check fails.
3. Add a test in the consumer that runs the check: `python gateway/tools/kikolib_manifest.py check <copy>/kikolib`
   (copy that script too, it is stdlib only), and assert `kikolib.__version__` equals the pinned version.
4. Never edit the copy. To change anything: fix it here, bump the version, add a `CHANGES.md` entry, run
   `python gateway/tools/kikolib_manifest.py write`, release a new tag, re-copy.

The test `gateway/tests/kiko/test_kikolib_manifest.py` keeps the manifest from going stale.

### Choosing the HTTP backend (consumers)

`http.client()` takes no argument; it calls `factory.choose("http")`. On Windows 10/11 the `winhttp`
backend (priority 5) beats `stdlib` (priority 10). To pin the stdlib backend either:

- set the environment variable before the first `client()` call: `KIKO_HTTP_IMPL=stdlib`, or
- in code: `factory.choose("http", override="stdlib")` once at start-up (the choice is cached; `client()` then returns it).

`KIKO_CA_BUNDLE` set also makes winhttp unavailable (stdlib is used). The default is unchanged.

## Modules

| Module | Purpose | Status |
|---|---|---|
| `factory.py` | thread-safe implementation registry and chooser | done |
| `keystore.py` | API keys: env var first, then OS store (wcm, keychain, secret-tool, kwallet, dcop) | done (dcop verified on live TDE/Q4OS by the owner, 2026-11) |
| `posix.py` | signals, RSS, sd_notify, watchdog, chmod 0o600, atomic private write | done |
| `config.py` | the one source of paths, ports (base_port) and typed settings (one table); `kiko.toml` via tomllib, read on demand, never at import | done |
| `tomlwrite.py` | TOML writer: update / insert / remove a key, keeps comments, atomic, refuses what it cannot represent | done |
| `logs.py` | `[logs] file` handler and the `[logs] retention_days` pruner | done |
| `dotenv.py` | `.env` reader | done |
| `tokens.py` | fallback token estimate (not a tokenizer) | done |
| `yaml_lite.py` | read-only YAML subset; only for the one-time import of an old `kiko.yaml` | done |
| `http.py` | HTTP client on `http.client` + `ssl`: keep-alive pool, streamed SSE lines, proxy CONNECT, OS trust store | done |
| `retry.py` | retry policy: exponential backoff + jitter, `Retry-After` within a soft budget, `on_wait` slices for SSE pings | done |
| `winhttp.py` | HTTP backend "winhttp" (factory kind `http`, priority 5): WinHTTP via ctypes on Windows 10/11, Windows proxy / PAC / SChannel; stdlib stays the fallback (also when `KIKO_CA_BUNDLE` is set) | done |
| `server.py` | threaded HTTP server, router, `serve()` (signals, systemd, RSS limit, drain) | done |
| `sse.py` | SSE encode / ping / parser | done |

Known limits of `http.Response.abort()` (wakes a reader blocked on a silent upstream from another thread):
it is a no-op once the response is finished or closed; on Linux `shutdown()` wakes the read; on Windows a
plain socket is also handle-closed, but a TLS socket is only shut down (closing the handle under a thread in
`SSL_read` risks a handle-reuse race), so there the worker may live until the read timeout; a worker still
blocked in `getresponse()` (no response head yet) cannot be aborted and ends at the timeout. The caller's
own thread is never blocked by any of these. No TLS abort test exists (no stdlib way to make a test cert).

Size limits: only `iter_lines` is capped (`MAX_LINE`, 16 MiB). Non-streaming response bodies (`http.py`,
`winhttp.py`) and `sse.Parser` data lines are not size-capped; kikolib is meant for trusted upstreams.

`server.App.browser_guard` is off by default. A consumer serving HTTP must turn it on (kiko does, in
`kiko_lite/app.py`).

No config file long term: settings live in SQLite. `crypto.py` and `jsonschema_lite.py` were removed.
