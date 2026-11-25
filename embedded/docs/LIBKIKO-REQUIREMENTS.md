# libkiko - Requirements (portable C core)

Status: requirements, step 1 of 2 (2026-11-25). This document says **what libkiko must do**, not which
components implement it; step 2 is the component search (section 6). Where it differs from
[PLAN.md](PLAN.md) (which is OpenWrt-only, tied to uloop / uclient / ustream-ssl), this document wins for
libkiko; PLAN.md still describes the kiko-wrt proxy steps.

## 1. Purpose and consumers

libkiko is the C sibling of the Python `kikolib` + the request pipeline of `kiko_lite`: it accepts a chat
request in a known wire format, picks a provider, translates the request, sends it over HTTPS, and returns
(or streams) the answer in the caller's wire format. It is **portable**: one pure-C core with no I/O, plus
one small I/O backend per platform, chosen at build time.

| Consumer | How it uses libkiko | What it needs most |
|---|---|---|
| **Layka** (C++ terminal coding agent, Windows + Linux) | links libkiko in-process (step 2 of its migration: one app instead of app + Python rex process) | non-blocking calls from the UI thread, streaming by callback, cancel, OS key store, OS TLS / proxy |
| **lib_rex** (later, C++ agent loop) | calls libkiko for every model turn, often several in parallel (subagents) | same as Layka + concurrent requests on one gateway handle |
| **kiko-wrt** (OpenWrt daemon under procd) | links libkiko statically behind its own HTTP server | small size, flat RSS, single-threaded event-loop integration, 0600-file keys |

Architecture fixed by the owner (not re-decided here): pure-C core without I/O; per-platform I/O backends
behind one small interface; reuse mature permissive open-source C where it exists; C11; `zig cc` locally,
release builds in CI.

## 2. Functional requirements

Scope rule: **v1 = what kikolib / kiko_lite does today and a consumer above needs.** Everything else is
marked "not in v1". File references are to `gateway/` unless stated.

### 2.1 Request / response model

- **R1** The public request format is **OpenAI chat completions** (`messages`, `tools`, `tool_choice`,
  `max_tokens` / `max_completion_tokens`, `temperature`, `top_p`, `stop`, `stream`,
  `stream_options.include_usage`), the same surface as `kiko_runtime_lib.Gateway.chat`
  (`kiko_runtime_lib/gateway.py`) and `kiko_lite/chat_front.py`.
- **R2** The **Anthropic Messages** body is the internal pivot format, exactly as in kiko_lite
  (`kiko_lite/app.py` docstring: every front converts to Messages, every provider converts from it). The
  Anthropic Messages front (`POST /v1/messages` semantics, stream and non-stream) is therefore also public in
  v1 at no extra translation cost. Decision: keep kiko_lite's pivot so the C port is 1:1 with the tested
  Python and golden fixtures can be shared.
- **R3** Requests and responses cross the API as **JSON text** (UTF-8 bytes + length), not as a C object
  model. The caller (C++ Layka, C kiko-wrt) keeps its own JSON library; libkiko owns its parsing.
- **R4** Non-stream result: one complete `chat.completion` (or Anthropic `message`) JSON with `usage`;
  `finish_reason` mapping as `chat_front._FINISH` (`end_turn->stop`, `max_tokens->length`,
  `tool_use->tool_calls`, `refusal->content_filter`).
- **R5** The client body is never forwarded as-is: the upstream body is built from a **whitelist** of fields
  (`providers/openai_compat.py` docstring); unknown fields are ignored; `n > 1` is a 400 (`chat_front.py`).
- **R6** Model resolution: the caller passes a table `client model name (glob) -> "provider/upstream-id"`
  and an optional class table (opus / sonnet / haiku / fable keyword -> target), as
  `kiko_lite/app.py SimpleResolver` and `Gateway.from_config(models=, classes=)`. DB-backed resolution
  (`resolver.py DBResolver`) is **not in v1** (the app owns its storage).
- **R7** Failover: for one request, up to 3 ordered candidates (`app.py MAX_ATTEMPTS`); move to the next
  candidate only on 429 / 408 / 5xx / timeout / connection error, **never after the first upstream body
  byte**, never on other 4xx. Per-(provider, model) in-memory **cooldown** from Retry-After / Gemini
  `RetryInfo`, else 60 s, max 3600 s (`app.py Cooldowns`, `cooldown_seconds`). kiko-wrt may configure one
  candidate (SPEC 9).
- **R8** List the configured client model names (OpenAI `/v1/models` list shape) from the resolver table
  (`app.py gw.models`). Fetching the upstream's own model list: **not in v1**.
- **R9** Token estimate when the upstream returns no usage: `ceil(utf8_bytes/4)` + 4 per message + 8 per
  tool (`kikolib/tokens.py`); also usable as a standalone `count_tokens` call. Not a tokenizer.

### 2.2 Providers and translation

- **R10** Provider table = plain data, one entry per provider: `base_url`, `kind` (`openai` | `gemini`),
  `key_names` (tried in order), `auth` (`bearer` | `header:<Name>`), `flags` (`max_tokens_cap`,
  `max_tokens_field`, `stream_usage`, `mistral`) - exactly `kiko_lite/providers/__init__.py PROVIDERS`.
  Built-in defaults for nvidia, groq, dashscope, openai, mistral, gemini (+ aliases qwen, google,
  nvidia_nim); the caller may add / replace / remove entries (as `provider_table(overrides)`).
- **R11** `openai` kind: Messages -> chat completions and back (`providers/openai_compat.py`): `toolu_`
  id prefix round trip, invalid JSON arguments -> `{"_raw": text}` and back, tool_result -> `role: tool`
  messages, images in tool results -> text note, `reasoning_content` / `reasoning` / leading
  `<think>` -> thinking block, stop-sequence detection, Mistral quirks (tool_choice `any`, `$id`/`$schema`
  removal, no `stream_options`), `max_completion_tokens` for OpenAI.
- **R12** `gemini` kind: Messages -> native `generateContent` / `streamGenerateContent` and back
  (`providers/gemini.py`): thought-signature id suffix `toolu_<hex>__thought__<b64>` and dummy signature on
  Gemini 3+, `thinking` -> `thinkingConfig`, tool schema cleaning (`$ref` resolution, depth 32, 5000 nodes,
  only `enum` / `date-time` formats), safety finish -> `refusal`, `x-goog-api-key` header, blocked prompts.
- **R13** Tool / function calls in both directions for both kinds, streaming included (argument deltas for
  the call being built; out-of-order deltas for a closed call dropped, as `openai_compat.StreamTranslator`).
- **R14** Input images: `data:` URLs -> base64 sources; http(s) URLs passed as URL to `openai` kind only.
  Gemini gets inline base64 only. libkiko **never fetches** a remote URL itself.
- **R15** Toolshim (`kiko_lite/toolshim.py`): per model, opt-in, turn a tool call written as text
  (`<tool_call>`, `<TOOLCALL>`, `[TOOL_CALLS]`, `<|python_tag|>`, bare / fenced JSON) into a real tool call,
  only for names in the request's tools. In v1 because Layka runs free models that need it.
- **R16** Not in v1: Responses front (`responses_front.py`) and Gemini front (`gemini_front.py`) - kiko
  serves codex / Gemini CLI with them, no libkiko consumer needs them; embeddings; image generation; audio;
  files; batches; `n > 1`; logprobs; Anthropic as an upstream kind; prices / billing / monthly caps
  (`prices.py`, `db.py`); management API (`management.py`); WebSockets (`kikolib/websocket.py`).

### 2.3 Streaming / SSE

- **R17** SSE parser with WHATWG rules as `kikolib/sse.py Parser` (`feed` / `finish`: `event`, `data`
  multi-line join, comments ignored, dispatch on blank line), fed **any byte split** with identical events.
- **R18** SSE encoder: `encode(event, data)` and `ping` as an SSE comment line (`: ping`), as `sse.py`.
- **R19** Streamed output to the caller is a sequence of `chat.completion.chunk` JSON objects (or Anthropic
  stream events) delivered **one per callback**; the `[DONE]` marker is consumed, not delivered
  (`kiko_runtime_lib` `_iter_chunks`). Chunks contain only `id`, `object`, `created`, `model`, `choices`;
  every tool-call fragment carries `id` and `type`; optional usage chunk with empty `choices` (`chat_front`).
- **R20** Error after streaming started is delivered as a terminal error event (OpenAI: error chunk /
  Anthropic: `error` event), never as a silent end.
- **R21** Keep-alive: while waiting for a retry or a silent upstream, the core can emit ping ticks so a
  proxy (kiko-wrt) can write SSE comments (`retry.py on_wait` slices, `PING_INTERVAL` 10 s).

### 2.4 Retry, timeouts, cancel

- **R22** Retry policy as `kikolib/retry.py RetryPolicy`: attempts 5, base 1 s, max 20 s, soft budget 90 s,
  statuses {408, 409, 425, 429, 500, 502, 503, 504, 529}, full jitter with an **injectable rng and clock**.
- **R23** `Retry-After` parsing: delta seconds and HTTP-date (`parse_retry_after`), capped by remaining
  budget.
- **R24** Retries only **before the first body byte** reaches the caller.
- **R25** Timeouts: connect (default 10 s), first byte / read (default 60 s, per-model override as
  `app.py _model_timeout`), and stream idle; all set by the caller.
- **R26** **Cancel** at any time from any thread: stops retries and failover, aborts the upstream request,
  delivers exactly one terminal "cancelled" completion, then no more callbacks. Fixes the known Python limit
  (`kikolib/README.md`: a TLS read on Windows cannot be aborted until timeout): in C, cancel must return
  control to the caller **at once** even if the backend thread lingers.

### 2.5 Errors

- **R27** One error model: an enum kind (`config`, `bad_request`, `auth`, `not_found`, `rate_limited`,
  `upstream`, `connect`, `tls_verify`, `connection_lost`, `timeout`, `cancelled`, `too_large`, `internal`) +
  HTTP-like status + short message + optional `retry_after` seconds. Mirrors `http.py` error classes and
  `app.py map_upstream_error`.
- **R28** Upstream 401 / 403 -> 502 with a generic message (they would look like a bad client key); other
  upstream messages scrubbed of the API key and truncated (`app.py _scrub`, `_upstream_message`).
- **R29** Error bodies in the caller's wire shape (OpenAI `{"error": {...}}` or Anthropic shape), as
  `chat_front.error_body` / `app.error_body`.

### 2.6 Secrets / key store

- **R30** Secrets are obtained through a **caller-supplied callback** `secret(name) -> value` (as
  `Gateway.from_config(secret=)`); the core never reads env or stores by itself.
- **R31** Optional platform key-store helper (separate module, in the backend layer) with the kikolib order:
  environment first, then OS store. Windows: Credential Manager (as `keystore.py WcmStore`). Linux desktop:
  Secret Service (as `SecretToolStore`); KWallet / DCOP / Keychain not in v1. OpenWrt: 0600 regular file,
  not a symlink, owned by the daemon user (PLAN keystore row). Names `[A-Za-z0-9_]`.
- **R32** Secret memory is zeroed when freed (`explicit_bzero` or volatile loop) - SPEC 8.

### 2.7 Logging hook

- **R33** One log callback `(level, component, message)`; default = silent. No log files, no global
  logger. Never logs keys, prompts, responses or header values (`app.py` docstring, SPEC 8).
- **R34** One result record per request through a callback (`app.py on_result` / `RequestResult`):
  model, provider, status, duration, token usage, attempts. No content.

### 2.8 Config input

- **R35** All configuration is passed by the embedding app as **in-memory structs / JSON text**:
  provider table, model table, class table, retry policy, timeouts, limits, TLS options (CA file override,
  proxy). **No config-file format in the core** (no TOML / UCI / YAML parsing; `config.py`,
  `tomlwrite.py`, `yaml_lite.py`, `dotenv.py` stay out).
- **R36** A gateway can be **reconfigured** atomically (kiko-wrt SIGHUP reload) without dropping in-flight
  requests: new requests use the new config, running ones finish on the old.

### 2.9 Threading / event model

- **R37** Requests are **asynchronous**: `start` returns at once with a request handle; results arrive
  through callbacks; `cancel` per R26. A **blocking convenience call** exists for simple callers and tests.
- **R38** Desktop (Layka, lib_rex): callable from the app's UI thread without blocking it; **several
  requests in flight on one gateway handle** from several threads; callbacks run on a backend thread by
  default, and the app may choose **pump mode** instead (callbacks only inside `kiko_gateway_pump()` called
  by the app thread) so a single-threaded C++ TUI needs no locks.
- **R39** Router (kiko-wrt): single-threaded, no library-created threads; the backend integrates into the
  daemon's existing event loop (procd world: uloop) through fd + timer hooks.
- **R40** Decision: the core is **sans-I/O** - state machines fed with bytes and time ("feed / next output /
  next deadline"); it never blocks, sleeps, allocates threads or reads the clock (clock and rng injected).
  Backends own sockets, TLS, threads and timers. Therefore **no general event-loop library is needed**:
  desktop backends use one worker per in-flight request (bounded), the router backend uses the platform's
  loop.
- **R41** Backpressure: the stream callback may return "pause"; the core stops reading from upstream until
  resumed (needed for kiko-wrt's 256 KiB write cap, PLAN L11).

### 2.10 HTTP server part (kiko-wrt only)

- **R42** The inbound HTTP/1.1 server (router, request parsing with limits, chunked / SSE responses,
  connection cap, drain on stop - PLAN L10 / L11) is **not part of the portable core**; it is a separate
  optional module built only for kiko-wrt. Desktop consumers do not link it. A desktop "local server" mode:
  **not in v1**.

## 3. Non-functional requirements

- **N1 Size.** Router: libkiko <= 80 KB stripped in the example program, kiko-wrt total < 300 KB, idle RSS
  < 8 MB (PLAN size targets). Desktop: libkiko core + backend <= 1 MB added to the executable when TLS comes
  from the OS (Windows); <= 2 MB when a TLS library is linked (Linux); idle RSS added <= 5 MB.
- **N2 Flat memory.** RSS under a 1 GB stream stays flat (PLAN S10); per-request memory bounded by limits.
- **N3 Portability matrix (v1):** Windows 10/11 x64 (MSVC-compatible ABI for Layka; built with zig cc),
  Linux x64 glibc, OpenWrt 24.10 / 25.x musl on x86_64, MIPS (mipsel 24kc) and ARM (aarch64, arm_cortex-a7).
  Not in v1: macOS, Windows ARM64, Linux aarch64 desktop (no consumer yet; code must not prevent them).
- **N4 Language.** C11, no compiler extensions in the core; public headers compile as C and as C++17
  (`extern "C"`, no `_Generic`, no VLAs, no `_Atomic` in headers).
- **N5 No global state.** Everything hangs off handles; any number of gateways per process; fully
  re-entrant; thread-safety per R38 documented per function. No `atexit`, no static mutable data.
- **N6 Memory safety.** One growable buffer type with a hard cap is the only place that reallocs (PLAN
  `buf.h`); all size arithmetic overflow-checked; every allocation fallible and checked; ownership comment
  on every struct; caller-supplied allocator optional.
- **N7 Limits (defaults, all configurable):** request body 32 MiB desktop (`kikolib/server.py
  DEFAULT_MAX_BODY`) / 1 MiB router (SPEC 6); JSON depth 64; buffered non-stream upstream body 8 MiB; SSE
  line 1 MiB (kikolib uses 16 MiB `MAX_LINE`; 1 MiB is the SPEC value and enough); in-flight requests per
  gateway 8. Exceeding a limit is an error, never a truncation.
- **N8 Security.** TLS verification **on by default**, hostname checked, no "skip verify" switch (plain
  http only per provider with an explicit `insecure_http` flag, SPEC 8); OS trust store on Windows and Linux
  desktop, CA bundle file on OpenWrt; HTTPS proxy support on desktop (`HTTPS_PROXY` / `NO_PROXY`, Windows
  system proxy as `winhttp.py`); secrets never in errors, logs, URLs; upstream error text scrubbed (R28).
  SSRF: provider base URLs come only from the app's config, client bodies cannot set them (R5), and
  libkiko never fetches URLs found in content (R14) - no further SSRF control needed.
- **N9 Licence.** libkiko is MIT (author Peter Kokosharov); every linked or vendored component must be
  permissive (MIT, BSD, ISC, Apache-2.0, zlib, public domain). GPL / AGPL / LGPL code is never linked or
  vendored. OS-provided libraries reached through their system API (WinHTTP, SChannel, Credential Manager)
  are fine.
- **N10 Readability.** One module = one `.c` + one `.h`; names `kiko_<module>_<name>` mirroring kikolib /
  kiko_lite; a Python <-> C name table in `libkiko/README.md` (PLAN rules).
- **N11 Determinism.** Same input bytes + same injected clock / rng = same output bytes (needed for golden
  tests).

## 4. API shape sketch (shape only)

```c
/* kiko/kiko.h - opaque handles, JSON text in/out, callbacks for streams. */
typedef struct kiko_gateway kiko_gateway;            /* one per config; shared across threads */
typedef struct kiko_request kiko_request;            /* one in-flight call */
typedef struct { int kind; int status; const char *message; double retry_after; } kiko_error;

typedef struct {                                     /* all text is UTF-8 JSON */
  const char *providers_json;   /* overrides of the built-in provider table (R10), may be NULL */
  const char *models_json;      /* {"client-name-glob": "provider/upstream-id"} (R6) */
  const char *classes_json;     /* {"opus": "...", ...}, may be NULL */
  kiko_retry_policy retry;      /* R22, zero = defaults */
  kiko_limits limits;           /* N7, zero = defaults */
  kiko_timeouts timeouts;       /* R25 */
  const char *(*secret)(void *ctx, const char *name);         /* R30, owned copy freed via kiko_secret_free */
  void (*log)(void *ctx, int level, const char *component, const char *msg);  /* R33 */
  void (*on_result)(void *ctx, const kiko_result *r);                         /* R34 */
  int  delivery;                /* KIKO_DELIVER_THREAD | KIKO_DELIVER_PUMP (R38) */
  void *ctx;
} kiko_config;

kiko_gateway *kiko_gateway_new(const kiko_config *cfg, const kiko_backend *io, kiko_error *err);
int   kiko_gateway_reconfigure(kiko_gateway *gw, const kiko_config *cfg, kiko_error *err);  /* R36 */
int   kiko_gateway_pump(kiko_gateway *gw, int timeout_ms);  /* PUMP mode: runs pending callbacks */
char *kiko_gateway_models(kiko_gateway *gw);                /* R8, caller frees with kiko_free */
void  kiko_gateway_free(kiko_gateway *gw);                  /* cancels and waits for all requests */

typedef struct {
  int  (*on_chunk)(void *ctx, const char *json, size_t len); /* one chunk/event; return KIKO_PAUSE to pause */
  void (*on_done)(void *ctx, const char *json, size_t len, const kiko_error *err); /* exactly once */
  void *ctx;
} kiko_callbacks;

/* wire = KIKO_WIRE_OPENAI_CHAT | KIKO_WIRE_ANTHROPIC_MESSAGES (R1, R2) */
kiko_request *kiko_chat_start(kiko_gateway *gw, int wire, const char *body, size_t len,
                              const kiko_callbacks *cb, kiko_error *err);
void  kiko_request_cancel(kiko_request *rq);                /* R26, any thread, returns at once */
void  kiko_request_resume(kiko_request *rq);                /* after KIKO_PAUSE (R41) */
void  kiko_request_free(kiko_request *rq);
char *kiko_chat(kiko_gateway *gw, int wire, const char *body, size_t len, kiko_error *err); /* blocking */

/* pure helpers, usable without a gateway (mirror kikolib) */
size_t kiko_tokens_estimate_messages(const char *body, size_t len, kiko_error *err);   /* R9 */
int    kiko_retry_parse_retry_after(const char *value, double now, double *seconds);   /* R23 */
kiko_sse_parser *kiko_sse_parser_new(void);  /* kiko_sse_parser_feed / _finish / _free (R17) */
```

The backend interface (`kiko/backend.h`, internal to platform code) is a small table: `http_start`
(method, URL, headers, body), `http_cancel`, `timer_set`, `now`, `rand`, plus "bytes arrived / head
arrived / finished(error)" entry points into the core. One implementation each: `backend_win.c`,
`backend_linux.c`, `backend_wrt.c`, and `backend_mock.c` (tests).

## 5. Test requirements (definition of done)

The owner's rule: **many tests so we do not fix later.** A module counts as done only when all of these
pass in CI on every platform of N3 (OpenWrt via SDK cross-build + qemu-user or the Docker rootfs).

- **T1 Golden pairs shared with Python.** Request/response translations are tested against language-neutral
  fixture files (`fixtures/<case>/{in.json, out.json, stream.sse, expected.jsonl}`) that **both** the Python
  kiko tests and the C tests read. Today the Python cases are inline in `tests/kiko/test_kiko_lite_*.py`; step
  one of the port is extracting them to these files (Python tests then read the files too). Coverage:
  every case in `test_kiko_lite_openai_compat.py`, `test_kiko_lite_gemini.py`, `test_kiko_lite_chat_front.py`,
  `test_kiko_lite_toolshim.py`, `test_kikolib_sse.py`, `test_kikolib_retry.py`, `test_kikolib_tokens.py`,
  plus the PLAN Phase 0 recorded real pairs (sanitised).
- **T2 Byte-split streams.** Every recorded stream fed split at every byte boundary gives identical output.
- **T3 Fuzz targets** (libFuzzer via clang / zig, 30 min each per parser change, overnight long runs):
  `fuzz_json`, `fuzz_sse`, `fuzz_http_response`, `fuzz_openai_translate`, `fuzz_gemini_translate`,
  `fuzz_gemini_stream`, `fuzz_toolshim`, `fuzz_retry_after`; kiko-wrt adds `fuzz_http_request`.
- **T4 Sanitisers.** All tests under ASan + UBSan on Linux; valgrind or equivalent leak check once per
  release; TSan for the desktop backend's concurrent requests and cancel.
- **T5 Limits.** Every limit in N7 tested at exactly the limit and limit + 1.
- **T6 Secret hygiene.** A test injects known key values and greps every log line, error message, result
  record and error body produced by the whole suite: zero hits.
- **T7 Mock upstream.** One scripted mock (PLAN L2): ok / 400 / 401 / 429 + Retry-After / 500 / invalid JSON
  / huge body / slow / stalled / cut stream / TLS with test CA, wrong host, self-signed. Each backend runs the
  same scenario list.
- **T8 Cancel and timeouts.** Cancel before head, during head wait, mid-stream, during retry wait: one
  terminal callback, no callback after, no leak; mock log shows the upstream connection closed.
- **T9 Determinism.** Fixed clock + rng produce identical output across platforms (same fixture files).
- **T10 Size / RSS gates.** N1 and N2 numbers measured in CI, failure over budget.
- **T11 C++ consumer test.** A small C++17 program includes the public headers and runs one stream against
  the mock on Windows and Linux.
- **T12 Independent review.** Each finished module reviewed by a model that did not write it; release
  only after an Opus / Fable audit (owner's standing rule).

## 6. Search checklist for step 2 (components to FIND)

Hard requirement in every row: permissive licence (N9), C (or C with a C API), maintained (release in the
last 2 years, real users), builds with `zig cc`, no global mutable state or it can be isolated.

| Capability | Hard requirements | Nice to have | Write ourselves if nothing fits? |
|---|---|---|---|
| JSON parse / build | depth + size limits, no recursion blow-up, allocation failure handled, UTF-8 validation, all N3 targets, <= 30 KB | streaming / SAX mode, used on OpenWrt already | no (a mature one surely exists) |
| HTTP/1.1 response parser (client side) | incremental, any byte split, chunked transfer, header limits, fuzzed upstream | also parses requests (kiko-wrt), no allocations | yes, small, if no fuzzed one fits |
| HTTPS client, Windows | OS TLS (SChannel) + system proxy + OS trust store, async or cancellable | WinHTTP via plain Win32 API (what `kikolib/winhttp.py` uses) | glue only (OS API) |
| HTTPS client, Linux desktop | cancellable, HTTPS proxy, OS trust store, multi-request | HTTP/1.1 keep-alive | yes, over a TLS library + own parser |
| HTTPS client, OpenWrt | fits 80 KB budget with what the image already ships | uses the image's TLS lib (no extra package) | yes (PLAN risk 1 already plans that) |
| TLS, Linux desktop | permissive, hostname verify, system CA path, <= ~1 MB static | TLS 1.3, small | no (never write TLS) |
| TLS, OpenWrt | already on default images or one small package, permissive | same library as Linux desktop | no |
| SSE parser | WHATWG rules, byte-split safe | - | yes (about 100 lines in kikolib) |
| Event loop | **not needed** in the core (R40); router backend uses the platform loop | - | n/a |
| Embedded HTTP server (kiko-wrt only) | event-driven, limits, chunked responses, fits budget | runs on the platform loop | yes, if nothing small and permissive fits |
| Key store, Windows | Credential Manager via OS API | - | glue only |
| Key store, Linux desktop | Secret Service over D-Bus without LGPL linking | permissive minimal D-Bus client | yes (or call `secret-tool` like kikolib does) |
| Key store, OpenWrt | 0600 file checks | - | yes (small; kiko-identity C library as basis, PLAN) |
| Base64 (std + url-safe) | constant buffer bounds, decode validation | - | yes (tiny) |
| UTF-8 validate / count | bounded, fast | - | yes (tiny) |
| HTTP-date parsing (Retry-After) | IMF-fixdate + obsolete forms, no locale | - | yes (small) |
| Glob match (model table) | `fnmatch` semantics as kiko_lite | - | yes (tiny) |
| Whole existing C/C++ OpenAI / Gemini client or proxy | permissive, C or C-API, streaming + tools, no heavy deps | Gemini translation included | yes - libkiko itself is that; take parts only if a candidate is clearly better |

## 7. Open questions for the owner

1. **Licence policy:** may libkiko on Linux desktop call an LGPL system library **at run time only**
   (loaded with `dlopen`, never shipped or linked, e.g. libsecret for the key store)? yes / no
   (Default if no: call the `secret-tool` program as kikolib does today.)
2. **Scope of fronts:** v1 exposes only OpenAI chat + Anthropic Messages; the Responses (codex) and Gemini
   (Gemini CLI) fronts stay Python-only in kiko for now. Agreed? yes / no
3. **Platforms:** is macOS needed in v1 (Layka on a Mac, e.g. a colleague's)? yes / no
4. **Priority:** which consumer gets libkiko first? 1) Layka (desktop backends first), 2) kiko-wrt (router
   backend first, as PLAN), 3) both in parallel.
5. **Ownership of fixtures:** may the shared golden fixtures (T1) live in the kiko repo and be the single
   source for both Python kikolib and C libkiko tests (Python tests change to read them)? yes / no

Technical decisions taken here (not questions): Anthropic Messages stays the internal pivot (R2); the core
is sans-I/O and needs no event-loop library (R40); JSON text crosses the API (R3); the inbound HTTP server
is kiko-wrt-only (R42); TLS is never written by us (section 6).

## Sources

All read locally on 2026-11-25.

- `%USERPROFILE%\kiko-src\embedded\docs\PLAN.md` (libkiko section, module decisions, size targets, steps)
- `%USERPROFILE%\kiko-src\embedded\docs\SPEC.md` (sections 6 Limits, 8 Security, 9 Non-goals)
- `%USERPROFILE%\kiko-src\gateway\kiko_runtime_lib\README.md`, `gateway.py`
- `%USERPROFILE%\kiko-src\gateway\kikolib\README.md`; function lists of `http.py`, `sse.py`,
  `retry.py`, `tokens.py`, `keystore.py`, `logs.py`, `winhttp.py`, `factory.py`; limits in `server.py`
- `%USERPROFILE%\kiko-src\gateway\kiko_lite\app.py` (docstring, routes, failover, cooldowns)
- `%USERPROFILE%\kiko-src\gateway\kiko_lite\providers\__init__.py`, `openai_compat.py`, `gemini.py`
- `%USERPROFILE%\kiko-src\gateway\kiko_lite\chat_front.py`, `gemini_front.py`, `responses_front.py`,
  `toolshim.py`, `resolver.py` (docstrings and function lists)
- `%USERPROFILE%\kiko-src\gateway\tests\kiko\` (file list only)
