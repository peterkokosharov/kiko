# kiko embedded - Build plan

Status: plan (directory `embedded/` of the kiko monorepo; binary and package name `kiko-wrt`). Agents work
during the day on up to 4 machines (lanes A-D); the owner reviews in the evening. Spec: [SPEC.md](SPEC.md).
Team and review rules: [TEAM.md](TEAM.md). Tests: [TESTING.md](TESTING.md).

Owner decision 2026-11-23: **Phase 1 is libkiko**, a C rewrite of kikolib (the Python library under
`gateway/kikolib/`). The kiko-wrt proxy comes after, built on libkiko. Calendar: **11 working days on
4 machines** (one day more than the earlier two-week budget; that day is the price of a reusable library),
about **14 working days on 3 machines**. See "Schedule" and "Risks".

## Daily rhythm

- **One step = one agent-day + one evening review by the owner.** No step is planned for more than a day;
  if it does not fit, it is split before it starts.
- Morning: the lead (Big Pickle) hands each lane one step with its "done when" and the files it may touch.
- Day: the implementer writes code + tests; a different model reviews the diff (TEAM.md rule 1).
- End of day: each lane leaves a **review card** in `docs/log/YYYY-MM-DD-<step>.md`: what changed (5 lines),
  one command to check it (`make test`, a `curl` line, a size number), the result, open questions.
- Evening: the owner reads the cards, runs the check commands, says go / redo. A step counts as done only
  after the owner's go. Merge to `main` happens after that, one step per commit.
- Lanes work on separate branches `step/<id>`; the lead merges in the order of the schedule below.

## Language: plain C (C11) - final

Owner decision 2026-11-23: the language is plain C (C11). C++ was considered and rejected (libstdc++ not on
default images, size). The project is expected to grow to about 20,000+ lines of C, so structure matters:

- one module = one `.c` + one `.h`; the header is the module's only public face (types, function
  prototypes, ownership rules); other modules include headers, never `.c` files;
- two parts with a hard border: **libkiko** (`embedded/libkiko/`, static library `libkiko.a`, public
  headers `include/kiko/*.h`) and the **kiko-wrt proxy** (`embedded/src/`), which uses libkiko only through
  those headers; libkiko never includes a proxy header;
- a deliberately small set of readable helpers, now inside libkiko:
  - `buf.h` - growable byte buffer with a hard cap (`kiko_buf_append`, `kiko_buf_printf`, `kiko_buf_reset`),
    the only place that does `realloc`;
  - `str.h` - non-owning string slices (`kiko_str_eq`, `kiko_str_startswith`) for parsing without copies;
  - one ownership comment per struct;
  - `goto out` cleanup pattern, no macros that hide control flow.

Comment standard (TEAM.md rule 8) applies to every code step: the "done when" of every step that produces
code (L1-L12, S1-S12, H1-H4) also requires that the code meets it (file header, function comments, spec
links); a reviewer rejects code a C reader cannot follow from the comments.

## libkiko - the C sibling of kikolib

libkiko is the single source for C projects, as kikolib stays the single source for Python projects. The
public names mirror kikolib so the owner can read both side by side: Python `kikolib.<module>.<name>`
becomes C `kiko_<module>_<name>` in `include/kiko/<module>.h` + `src/<module>.c` (for example
`sse.Parser.feed` -> `kiko_sse_parser_feed`, `retry.parse_retry_after` -> `kiko_retry_parse_retry_after`,
`keystore.get_secret` -> `kiko_keystore_get_secret`). Where the shape must differ it is because Python
kikolib uses threads and blocking calls while libkiko runs on one `uloop` event loop: blocking calls become
callbacks or small state machines (`Response.iter_lines` -> an `on_line` callback, `send_with_retry` -> a
retry state machine on uloop timers). Each such difference is listed in `libkiko/README.md` in a
Python <-> C name table.

### Module decisions (kikolib module -> libkiko)

| kikolib module | Decision | Reason |
|---|---|---|
| `http.py` | **port** -> `http.c` | Upstream HTTPS client over libuclient + libustream-ssl (own HTTP/1.1 client on ustream-ssl if the L7 spike fails): `Headers`, `Response` (status, headers, `read`, `iter_lines` as callback, `json`, `abort`, `close`), error kinds `ConnectError` / `TLSVerifyError` / `ConnectionLost` / `HttpTimeout` as an enum, CA file, timeouts, secrets never in error text. Dropped: keep-alive pool (one connection per request in v1), `HTTPS_PROXY` / `NO_PROXY` (a router is the network edge), macOS trust store export. |
| `server.py` | **port** -> `server.c` | HTTP/1.1 server on `uloop` + `ustream-fd` instead of threads: `App` (`route`, `before`, `after`, `match`), `Request`, `JsonResponse`, `StreamResponse` (chunked), `HTTPError` with a pluggable error shape (OpenAI-shaped in kiko-wrt, Anthropic-shaped in kikolib), `host_allowed`, `Server` (`start`, `request_stop`, `drain`), `serve`. Dropped: `Takeover` (only for WebSockets). |
| `sse.py` | **port** -> `sse.c` | `encode`, `ping`, `Parser` (`feed`, `finish`) with the same WHATWG rules; the parser is shared by both stream paths of the proxy. `ping` sends an SSE comment line (`: ping`), which OpenAI clients ignore. |
| `retry.py` | **port** -> `retry.c` | `RetryPolicy` (attempts, base/max delay, soft time budget, retry statuses, full jitter, injectable rng), `delay_for`, `parse_retry_after` (delta seconds and HTTP date), `send_with_retry` as a uloop-timer state machine that retries only before the first body byte and calls `on_wait` per slice (SSE keep-alive pings). Whether the proxy uses it is a Phase 0 answer (SPEC [P0]). |
| `keystore.py` | **port, reduced** -> `keystore.c`, source = the kiko-identity C library | Owner decision 2026-11-23: kikolib.keystore is the single Python home; the C basis is kiko-identity's `c/kiko_identity.c/.h` (MIT, same author), adapted for OpenWrt. Its API: `kiko_identity_init/set/get/delete/list/backend/cleanup`; backends secret-service > gpg > openssl, encrypted file `~/.kiko/identity/store.kv.gpg`. Encryption there is **not** linked code: it runs the `gpg` or `openssl enc -aes-256-cbc -pbkdf2` command line tool through fork/exec, so it depends on one of them being installed. On OpenWrt gpg is not in the default image (a `gnupg` package may exist; openssl-util is optional) - **to verify** on a real 24.10 / 25.x image. Router default is therefore plain files, encryption optional (used only if a tool is present and a passphrase is configured). Kept: names `[A-Za-z0-9_]`, get returns an owned string, list/delete. Dropped: secret-service, getpass prompt (no tty in a daemon). Added: `kiko_keystore_get_secret` (environment first, then file `/etc/kiko-wrt/keys/<name>`, mode 0600, user `kiko-wrt`) as below. Original row: `get_secret` (environment first, then file `/etc/kiko-wrt/keys/<name>`; refused unless mode 0600, regular file, not a symlink, owned by the daemon user) and `set_secret` (via `write_private`). No OS stores: Windows Credential Manager, Keychain, secret-tool, KWallet and DCOP do not exist on OpenWrt; the factory chooser goes with them. |
| `posix.py` | **port** -> `posix.c` | `install_signal_handlers` (SIGTERM/SIGINT stop, SIGHUP reload, through uloop), `rss_bytes` (`/proc/self/status`), `make_private`, `write_private` (O_EXCL temp file, fsync, rename). Replaced: `sd_notify` / `watchdog_interval` -> procd (it starts, respawns and stops the daemon; no notify socket). |
| `tokens.py` | **port, adapted** -> `tokens.c` | `estimate_text` (ceil(UTF-8 bytes / 4)) as is; `estimate_messages` reads an OpenAI chat body (kikolib's reads an Anthropic Messages body). Used only when an upstream returns no usage, for the request log line. |
| (stdlib `json`) | **new** -> `json.c` | Helpers over json-c: `loads` with size and depth limits, typed getters with defaults, `dumps` (compact) into a `buf`. Python has this in the standard library; C needs it in one checked place. |
| (new helpers) | **new** -> `buf.c`, `str.c` | Bounded buffer and string slices (see above). |
| `config.py` | **replace** by UCI | `/etc/config/kiko-wrt` via libuci, read in the proxy (S2); no `kiko.toml`, no base-port scheme. |
| `logs.py` | **replace** by syslog | `ulog` -> logd (`logread`); retention is logd's ring buffer. No log file, no pruner. |
| `factory.py` | **drop** | One implementation per part, chosen at build time (uclient or own client); no runtime registry needed in C. |
| `dotenv.py` | **drop** | No `.env` on a router: keys live in 0600 files, environment comes from the procd init script. |
| `yaml_lite.py` | **drop** | Only for a one-time import of an old `kiko.yaml`; never present on a router. |
| `tomlwrite.py` | **drop** | Config is written with `uci` / LuCI. |
| `websocket.py` | **drop** | WebSockets are a non-goal (SPEC 9). |
| `winhttp.py` | **drop** | Windows only. |

### libkiko rules

- Separate static library with its own `CMakeLists.txt`; `cmake -S embedded/libkiko` builds and tests it
  with no proxy file present (a CI test does exactly this in a clean copy).
- Dependencies: libubox, libustream-ssl, libuclient, json-c only. No libuci (config belongs to the proxy).
- Own unit tests (`libkiko/tests/`), ASan/UBSan preset, fuzz targets `fuzz_http_request`,
  `fuzz_http_response`, `fuzz_sse`, `fuzz_json` (`libkiko/fuzz/`).
- Only what kiko-wrt needs or kikolib already has goes in; no speculative features.
- Public headers are owned by the lead (TEAM.md rule 3); after L13 an API change needs the lead's go.

### Size targets

- kiko-wrt (proxy + libkiko, stripped, `-Os`): **< 300 KB**; idle RSS **< 8 MB** (unchanged).
- libkiko's own budget: **<= 80 KB** of stripped code when linked into the L12 example program
  (`examples/echo_proxy`), idle RSS of that example **< 2 MB**. Measured in L1 (empty) and L12 (full); the
  proxy gets the rest of the 300 KB. Over budget = the lead trims before L13 passes.

## Phase 0 - Measure the real kiko (at home, before day 1)

Owner + lead, about one evening, on the home machine where kiko lives. Not an agent step on the build lanes.

Collect (no keys, no prompt contents):

1. Which endpoints clients call (from kiko's logs, last 30 days): chat/completions, models, embeddings,
   responses, completions; counts per endpoint.
2. Providers and models in kiko's config; aliases; fallbacks/retries used or not.
3. Request features in use: streaming share, tools, images, `response_format`, `reasoning_effort`,
   `stream_options`, typical and max body size, max output tokens.
4. Clients and their quirks (rex, Layka, Sisi, Lassie, opencode): tool call id length, whether they read
   `model` in responses, whether they need `/v1/models`.
5. kiko's footprint: RSS idle and under load, start time, install size - the baseline kiko-wrt must beat.
6. Record sanitized request/response pairs (OpenAI and Gemini, stream and non-stream, with tools) as
   contract fixtures for TESTING.md (keys and personal text replaced).

**Done when:** `docs/phase0-findings.md` lists the numbers above, every **[P0]** mark in SPEC.md is
resolved (kept, changed or dropped) and the fixtures are committed under `tests/fixtures/`.

## Steps

Each step is one agent-day. Every code step's "done when" also includes: comment standard met (TEAM.md
rule 8: file header, function comments with ownership and errors, protocol lines commented with a SPEC
link). "Lane" is the planned machine; "after" lists hard dependencies.

### Phase 1 - libkiko (13 steps)

| Id | Step | Lane | After | Done when |
|---|---|---|---|---|
| L1 | **libkiko skeleton**: layout (`include/kiko/`, `src/`, `tests/`, `fuzz/`, `examples/`), standalone CMake (presets debug/asan/release/fuzz), tiny test harness, `buf` + `str` with unit tests, all public headers drafted by the lead, `scripts/check.sh`, build-alone test. | A | P0 | `cmake -S libkiko` + `ctest` green in a clean copy without `src/`; ASan clean; empty library size noted in the card; comment standard met. |
| L2 | **Mock upstream server** (tests only, Python stdlib): OpenAI and Gemini endpoints, stream/non-stream, scripted errors, 429 + `Retry-After`, slow/partial responses, replay of fixtures, optional TLS with test CA. | B | P0 | `tests/mock_upstream.py --scenario X` serves every fixture; scenario list documented. |
| L3 | **posix + keystore**: `kiko_posix_install_signal_handlers`, `_rss_bytes`, `_make_private`, `_write_private`; `kiko_keystore_get_secret` / `_set_secret` (env first, then 0600 files under `/etc/kiko-wrt/keys`; adapted from the kiko-identity C library, encryption optional and only if gpg/openssl exist). | B | L1 | Tests: 0644 file, symlink, other owner and directory are refused; atomic write leaves no temp file on error; secret values never in any message; comment standard met. |
| L4 | **json helpers** over json-c: `kiko_json_loads` (size + depth caps), typed getters, `kiko_json_dumps` into `buf`; `fuzz_json`. | D | L1 | Unit tests for caps (exact and +1), wrong types, refcount ownership; `fuzz_json` 30 min clean under ASan/UBSan; comment standard met. |
| L5 | **sse**: `kiko_sse_encode`, `kiko_sse_ping`, `kiko_sse_parser_feed` / `_finish` (WHATWG rules as kikolib); `fuzz_sse`. | B | L1 | kikolib's `test_kikolib_sse.py` cases ported and passing; input split at every byte boundary gives identical events; `fuzz_sse` 30 min clean; comment standard met. |
| L6 | **retry**: `kiko_retry_policy`, `_delay_for`, `_parse_retry_after` (delta + HTTP date), send-with-retry state machine on uloop timers with `on_wait` slices. | B | L1 | kikolib's `test_kikolib_retry.py` cases ported (fake clock + fixed rng); budget and attempts limits hit exactly; no retry after a body byte; comment standard met. |
| L7 | **HTTP client spike** over libuclient: HTTPS POST with custom headers, TLS verification on, CA file, connect timeout. | C | L1 headers | Calls the mock (http) and a real https host with verification; self-signed and wrong-hostname certs rejected; decision "uclient OK / own client" written in the card. |
| L8 | **HTTP client I** (`http.c`): `kiko_http_request`, headers, status, capped body `read`, `kiko_response_json`, error kinds, timeouts; response head handling; `fuzz_http_response`. | C | L2, L7 | Mock scenarios ok/400/429/500/invalid JSON/huge body pass; keys never in error text (grep test); `fuzz_http_response` 30 min clean; comment standard met. |
| L9 | **HTTP client II - streaming**: incremental reads, `on_line` (as `iter_lines`, `MAX_LINE` cap), `kiko_response_abort`, first-byte / idle timeouts, backpressure hook. | C | L8 | Mock "slow stream", "stalled stream", "cut stream" end with the right error; abort stops the upstream request (seen in mock log); comment standard met. |
| L10 | **HTTP server I** (`server.c`): listen, `kiko_app_route` / `_match`, request line + headers + `Content-Length` body parser with limits, `JsonResponse`, `HTTPError` with pluggable error shape, `host_allowed`; `fuzz_http_request`. | A | L1 | `curl` gets 404/405/411/413/431/400 as specified; parser unit tests incl. malformed input; `fuzz_http_request` 30 min clean; comment standard met. |
| L11 | **HTTP server II**: `StreamResponse` (chunked / SSE), `before` / `after` hooks, connection cap, idle timeout, write backpressure (256 KiB), `kiko_server_request_stop` / `_drain`, `kiko_serve` (signals, procd-friendly exit codes). | A | L10 | 32 idle connections handled, 33rd gets 503; SIGTERM drains a running stream; comment standard met. |
| L12 | **tokens + example + name table**: `kiko_tokens_estimate_text` / `_estimate_messages` (OpenAI body); `examples/echo_proxy.c` forwards one route to the mock using server + http + sse + retry; `libkiko/README.md` with the Python <-> C name table; size measured. | D | L3, L6, L9, L11 | Example streams a mock answer end to end under ASan; size and idle RSS within the libkiko budget (table in README); comment standard met. |
| L13 | **libkiko gate**: full ASan/UBSan + valgrind over libkiko tests, every libkiko fuzz target 30 min, review of all libkiko code by a model that wrote none of it; public API frozen. | B | L12 | Checklist in TEAM.md ticked for libkiko with evidence; findings fixed or accepted by the owner. |

### Phase 2 - kiko-wrt proxy on libkiko (12 steps)

The old S2 (uclient spike), S3 (mock), S5 and S9 (HTTP server), S6 (upstream streaming) and S12 (SSE parser
+ fuzz I) are now L7, L2, L10/L11, L9 and L5/L10; the remaining steps are renumbered.

| Id | Step | Lane | After | Done when |
|---|---|---|---|---|
| S1 | **Proxy skeleton**: `src/` daemon linking `libkiko.a`, `main.c` on `kiko_serve`, `ulog` setup, `/health`, CMake that adds libkiko as a subdirectory. | A | L11 | Daemon starts, answers `/health`, exits cleanly on SIGTERM under ASan; comment standard met. |
| S2 | **Config**: in-memory config model + UCI loader + validation; keys and tokens via `kiko_keystore_get_secret`; reload on SIGHUP. | C | L3 | Unit tests for valid/invalid configs; keys never appear in any log line at `debug`; comment standard met. |
| S3 | **Gemini request translation** (pure function, JSON in -> JSON out, on libkiko json): messages, system, images, generation config, merging of turns. No tools yet. | D | L4 | Unit tests for every row of SPEC 4.2 request table (except tools) pass; comment standard met. |
| S4 | **Gemini response translation** (pure): non-stream response, finish reasons, usage, blocked prompts. | D | S3 | Unit tests for every row of the SPEC response table; P0 fixtures translate correctly; comment standard met. |
| S5 | **Auth, routing, `/v1/models`**, model allow-lists, token bucket rate limiter, inflight caps. | B | S1, S2 | `curl` tests: 401/403/404/429/503 and models list match SPEC 3 and 5; comment standard met. |
| S6 | **Tools translation** (pure): tools, tool_choice, assistant tool_calls, tool results, schema cleaning, thought-signature encoding in ids. | D | S3, S4 | Unit tests incl. multi-call turns, tool results merged, signature round-trip; P0 tool fixtures pass; comment standard met. |
| S7 | **OpenAI passthrough (non-stream)**: routing, model rewrite, key injection, header filtering, upstream error mapping and scrubbing; retry if P0 keeps it. | A | S5, L6, L9 | End-to-end via mock: success, 400, 401->502, 429, 500->502, timeout->504; comment standard met. |
| S8 | **Gemini streaming translation**: state machine over `kiko_sse` events -> `chat.completion.chunk`s, tool call chunks, finish, usage chunk, `[DONE]`. | D | L5, S4, S6 | Recorded Gemini streams split at every byte boundary give identical output; comment standard met. |
| S9 | **procd + UCI integration**: init script, `kiko-wrt` user, `service_triggers` reload, ujail params, keep.d, default config, `uci-defaults`. | C | S2 | In the OpenWrt Docker rootfs: `service kiko-wrt start/reload/stop` work; process runs as `kiko-wrt`. |
| S10 | **OpenAI SSE passthrough**: streaming forward with optional model rewrite, backpressure, disconnect abort, mid-stream error event. | A | S7 | Mock stream reaches client byte-identical (except `model`); disconnect aborts upstream; 1 GB stream keeps RSS flat; comment standard met. |
| S11 | **Gemini end-to-end wiring**: Gemini provider path (non-stream + stream + tools) in the daemon. | B | S6, S7, S8 | All Gemini fixtures pass end-to-end through the daemon against the mock; comment standard met. |
| S12 | **Fuzz harnesses II**: Gemini request/response/stream translators and OpenAI rewrite path. | D | S6, S8 | Each target 30 min clean; overnight run started on lane D. |

### Phase 3 - hardening, packaging, release (8 steps)

| Id | Step | Lane | After | Done when |
|---|---|---|---|---|
| H1 | **Packaging**: final OpenWrt `Makefile` (libkiko built and linked statically, not a separate package); SDK builds `.ipk` (24.10) and `.apk` (25.x) for x86_64 + one real router arch; Docker integration script. | C | S9 | Packages install in `openwrt/rootfs` containers of both versions; `curl` smoke passes inside. |
| H2 | **Contract tests**: replay of P0-recorded real responses through the daemon; client compatibility checks (opencode config, tool call id lengths). | B | S10, S11 | Contract suite green; no real keys anywhere in the repo (`scripts/secret-scan.sh` clean). |
| H3 | **Hardening**: ASan/UBSan/valgrind over all tests, key scrubbing tests, log audit, limit tests, integer-overflow review of all size math (proxy; libkiko was done in L13). | D | S10, S11 | Checklist in TEAM.md ticked with evidence per item. |
| H4 | **Size and memory**: binary/package size, idle RSS, RSS under 8 streams; libkiko share re-measured; trim (flags, dead code). | A | H1 | Stripped binary < 300 KB, libkiko <= 80 KB, idle RSS < 8 MB; numbers table in README. |
| H5 | **Docs**: README install/config, config reference, client setup (opencode, rex, Layka, Sisi, Lassie), troubleshooting, security notes. | C | H1 | A person with only the README installs it in Docker and gets a chat answer. |
| H6 | **Audit 1** (strongest-class auditor, all code since start incl. libkiko) + fixes by implementers. | A (+all for fixes) | L13, H2-H5 | Audit report in `docs/audits/`; every finding fixed or accepted with a reason by the owner. |
| H7 | **Audit 2** (independent second auditor) + real-router smoke list (TESTING.md section 7). | A | H6 | Second report clean or fixed; router smoke list all green. |
| H8 | **Release v0.1.0**: changelog, tag, packages archived with checksums. | A | H7 | Tag `v0.1.0`; `.ipk` and `.apk` with SHA-256 sums in `dist/`. |

Total: 33 agent-days (13 libkiko + 12 proxy + 8 hardening/release; before: 26) + Phase 0 evening + fix
work after audits.

## Schedule (4 lanes, 11 working days)

| Day | Lane A (lead machine) | Lane B | Lane C | Lane D |
|---|---|---|---|---|
| 1 | L1 libkiko skeleton | L2 mock upstream | L7 HTTP client spike | (P0 leftovers, fixture sanitizing, review L1) |
| 2 | L10 server I | L3 posix + keystore | L8 HTTP client I | L4 json helpers |
| 3 | L11 server II | L5 sse | L9 HTTP client II | S3 Gemini request |
| 4 | S1 proxy skeleton | L6 retry | S2 config | S4 Gemini response |
| 5 | (merge, integration) | S5 auth/routing/models | S9 procd/UCI | L12 tokens + example + size |
| 6 | S7 OpenAI passthrough | L13 libkiko gate | H1 packaging | S6 tools translation |
| 7 | S10 OpenAI SSE | (L13 fixes) | H5 docs | S8 Gemini streaming |
| 8 | H4 size/memory | S11 Gemini wiring | (25.x apk check, fixes) | S12 fuzz II |
| 9 | (merge) | H2 contract tests | fixes | H3 hardening |
| 10 | H6 audit 1 | fixes | fixes | fixes |
| 11 | H7 audit 2 + router smoke, then H8 release | fixes | buffer | long fuzz review |

Phase 1 is done after day 6 (L13); the proxy steps S1-S6 start earlier on frozen-by-the-lead headers so the
lanes are not idle. H7 and H8 share day 11 only if audit 2 finds nothing serious; otherwise H8 moves to
day 12.

With 3 machines: lane D's steps (json, tokens, translators, fuzz) move to B and C; about 33 step-days over
3 lanes plus audits and fixes gives **about 14 working days** (just under three weeks).

## Risks

1. **uclient fit (L7)** - the riskiest step. If the spike fails, lane C writes an own HTTP/1.1 client over
   ustream-ssl (two extra steps); L8/L9 shift by two days, S7 by two days, release moves to day 12-13.
2. **Schedule over budget** - 11 days on 4 machines is one day over the earlier two weeks, with almost no
   buffer; audit fixes can push the release to day 12-13. On 3 machines about 14 days.
3. **Two shapes of one API** - kikolib is blocking and threaded, libkiko is event-driven; names match but
   some calls do not (callbacks, state machines). Reading side by side can mislead; the README name table
   lists every difference.
4. **Library creep** - a "general" C library invites features kiko-wrt does not need, which cost days and
   bytes. Rule: only what kiko-wrt needs or kikolib already has; the lead rejects the rest.
5. **API churn before the freeze** - S1-S6 use libkiko headers before L13. The lead fixes the headers in
   L1 and alone may change them; a change after L13 needs the lead's go and a note in the card.
6. **Size** - general code can be bigger than code written for one program. libkiko has its own budget
   (80 KB), measured in L1, L12 and H4.
7. **Retry with streams** - a retry after bytes reached the client would corrupt the stream; libkiko
   retries only before the first body byte (as kikolib), tested in L6.
8. **Fuzz time** - 4 libkiko + 4 proxy targets at 30 min each per parser change; long runs overnight on
   lane D only.

## Parallel rules

- Pure translation steps (S3, S4, S6, S8) depend only on json-c, libkiko `json`/`sse` and fixtures, so they
  run in parallel with libkiko's server and client work.
- Only one lane edits a given module per day; libkiko public headers and the proxy's shared headers
  (`upstream.h`, `config.h`, `translate.h`) are fixed by the lead and changed only by the lead.
- Every merge runs the full desktop test suite (libkiko alone, then the proxy); a red `main` stops new
  merges until fixed.

## After v0.1.0 (not planned)

LuCI page, embeddings, fallbacks across providers, Anthropic upstream, response caching, a keep-alive pool
in `kiko_http` - only if Phase 0 or real use shows a need.
