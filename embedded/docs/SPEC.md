# kiko embedded - Specification (draft v0, before Phase 0)

Status: draft. Phase 0 measures the real kiko and may cut or add items here. Anything marked **[P0]** must
be confirmed against kiko's actual usage before implementation starts.

## 1. Purpose and scope

kiko embedded (binary `kiko-wrt`) is a single-process, event-driven HTTP proxy that accepts OpenAI Chat Completions requests from
LAN clients and forwards them to configured upstream providers:

- **openai** type: OpenAI or any OpenAI-compatible endpoint (vLLM, llama.cpp server, Ollama `/v1`,
  OpenRouter, Mistral, Groq, ...). The body is passed through with only the `model` field rewritten.
- **gemini** type: Google Gemini API (`generativelanguage.googleapis.com`), with full request/response
  translation, including streaming and function calling.

Clients only ever see the OpenAI dialect.

## 2. Library choices (decided)

Language: plain C (C11), final (owner decision 2026-11-23; C++ was considered and rejected).
All libraries below are C APIs.

| Need | Choice | Why |
|---|---|---|
| Event loop, sockets, timers, lists, AVL | **libubox** (`uloop`, `ustream`, `usock`, `avl`, `ulog`) | Present on every OpenWrt image; the same base procd/ubus/uhttpd use. Zero flash cost. |
| TLS | **libustream-ssl** (with the image's backend: mbedTLS by default, or wolfSSL/OpenSSL) | Thin abstraction already used by uclient-fetch and uhttpd; we do not hard-wire a TLS library, so the package works with whatever `libustream-*` variant the router has. Default on OpenWrt 24.10/25.x is mbedTLS. |
| HTTP client (upstream) | **libuclient**, behind libkiko's `kiko/http.h` (the proxy's small `upstream.h` sits on top) | Already on the image (uclient-fetch), uloop-native, does HTTPS via ustream-ssl, chunked decoding, custom headers, POST bodies and incremental reads (needed for SSE). Risk: small API, few knobs. The wrapper isolates it; if the L7 spike (PLAN.md) shows a blocker (e.g. missing per-request timeout or header limits), we replace it with an own ~600-line HTTP/1.1 client on `ustream-ssl` without touching callers. |
| HTTP server (LAN side) | **Own minimal HTTP/1.1 server** on `uloop` + `ustream-fd`, in libkiko (`kiko/server.h`) | uhttpd is not a library and has no streaming-proxy hook. We need only: request line, headers, `Content-Length` bodies, keep-alive off by default, chunked/SSE responses. Small and fully under our bounds checks. |
| JSON | **json-c** (`libjson-c`) | Already on every default image (ubus -> libblobmsg-json -> json-c), so it costs no flash, unlike a vendored cJSON (~25 KB). It has a configurable nesting depth (`json_tokener_new_ex`), incremental tokenizer and reference counting. blobmsg is not used for the API JSON: it is not designed for arbitrary nested user JSON (mixed arrays, large strings). |
| Config | **libuci** | Native `/etc/config/kiko-wrt`; LuCI can edit it later. |
| Logging | `syslog(3)` via libubox `ulog` | Goes to logd (`logread`) on OpenWrt, to journald/syslog on desktop. |
| Desktop build | CMake + system packages of libubox, ustream-ssl, uclient, json-c, uci (built from OpenWrt git if the distro lacks them) | Same code path as on the router. |
| Shared C code | **libkiko** (`embedded/libkiko/`, static library, linked into kiko-wrt) | The C port of kikolib: HTTP client and server, SSE, retry, keystore (0600 files), posix helpers, token estimate, json-c helpers, buffers. Module decisions and size budget in PLAN.md. |

Not used: Python, Lua, libcurl (too big), OpenSSL directly, threads (single-threaded uloop).

## 3. Endpoints

All endpoints are under the listen address (default: LAN IP, port 4000). Paths are matched exactly.

| Method + path | Auth | Description |
|---|---|---|
| `POST /v1/chat/completions` | token | Main endpoint, streaming and non-streaming. |
| `GET /v1/models` | token | Lists client-visible models from UCI (`object: "list"`, entries `{id, object:"model", created:0, owned_by:<provider name>}`). No upstream call. |
| `GET /v1/models/{id}` | token | One entry or 404. |
| `GET /health` | none | `200 {"status":"ok"}`; no details, no versions of upstreams. |
| anything else | - | `404` in OpenAI error format. |

**[P0]** Check whether any client also uses `/v1/completions` (legacy), `/v1/embeddings` or
`/v1/responses`. Planned: not in v1 (see non-goals); embeddings is the first candidate for v1.1 if kiko's
logs show use.

### 3.1 Client authentication

- Header `Authorization: Bearer <token>`. Tokens come from `config client` sections.
- Comparison is constant-time over the full length.
- A client may be limited to a list of models (`list allow_model`).
- Missing/wrong token: `401`, error type `authentication_error`. Logged with client IP only, never the
  token value.

## 4. Request handling

1. Read headers (max 8 KiB) and body (max `max_body`, default 1 MiB, `413` above).
2. Require `Content-Type: application/json` for POST.
3. Parse JSON (depth limit 64). Read `model`; look up `config model` by client-visible name -> provider +
   upstream model id. Unknown model: `404 model_not_found`.
4. Check client token and model allow-list, then rate limits.
5. Dispatch by provider type (openai passthrough or gemini translation).
6. Stream or buffer the response back (buffered non-stream bodies max 8 MiB, else `502`).

### 4.1 OpenAI passthrough

- Rewrite `model` to the upstream model id; everything else is forwarded unchanged (unknown fields
  included), so new OpenAI parameters work without code changes.
- Replace `Authorization` with the provider's key; drop all other client headers except
  `Content-Type`, `Accept`; add `User-Agent: kiko-wrt/<ver>`. Optional `config provider` `list header`
  for extra upstream headers (e.g. `OpenRouter` attribution).
- Optional per-model caps: `max_tokens_cap` clamps `max_tokens` / `max_completion_tokens`.
- Response: status and body passed through. In the response JSON (non-stream) and every stream chunk,
  `model` is rewritten back to the client-visible name. **[P0]** check whether clients care; if not, skip
  the rewrite in streaming (saves one parse per chunk).

### 4.2 Gemini translation

Upstream URLs:

- non-stream: `POST {base_url}/v1beta/models/{upstream_model}:generateContent`
- stream: `POST {base_url}/v1beta/models/{upstream_model}:streamGenerateContent?alt=sse`
- key in header `x-goog-api-key` (never in the URL, so it can never appear in a logged URL).

#### Request mapping (OpenAI -> Gemini)

| OpenAI | Gemini |
|---|---|
| `messages[role=system]` / `developer` (all, concatenated with `\n\n`) | `systemInstruction: {parts:[{text}]}` |
| `messages[role=user]`, content string | `contents[]: {role:"user", parts:[{text}]}` |
| user content array: `{type:"text"}` | `{text}` part |
| user content array: `{type:"image_url", image_url:{url:"data:<mime>;base64,<b64>"}}` | `{inlineData:{mimeType, data}}` |
| image_url with `http(s)://` URL | `400` (no fetching on the router) |
| `messages[role=assistant]`, content | `{role:"model", parts:[{text}]}` |
| assistant `tool_calls[]` `{id, function:{name, arguments}}` | `{functionCall:{name, args:<parsed arguments object>}}` parts in the same `model` turn (plus `thoughtSignature`, see below) |
| `messages[role=tool]` `{tool_call_id, content}` | `{role:"user", parts:[{functionResponse:{name, response}}]}`; `name` looked up from the earlier assistant tool_call with that id; `response` = content parsed as JSON object if it is one, else `{"content": "<string>"}`. Consecutive tool messages are merged into one `user` turn. |
| consecutive messages with the same Gemini role | merged into one turn (Gemini requires alternation) |
| `tools[] {type:"function", function:{name, description, parameters}}` | `tools:[{functionDeclarations:[{name, description, parameters}]}]`; `parameters` is copied after removing JSON-Schema keys Gemini rejects (`$schema`, `additionalProperties`, `strict`, ... - list fixed in S6 and checked by the H2 contract tests) |
| `tool_choice: "auto"/"none"/"required"` | `toolConfig.functionCallingConfig.mode: AUTO / NONE / ANY` |
| `tool_choice: {type:"function", function:{name}}` | mode `ANY`, `allowedFunctionNames:[name]` |
| `temperature`, `top_p` | `generationConfig.temperature`, `topP` |
| `max_tokens` / `max_completion_tokens` | `generationConfig.maxOutputTokens` |
| `stop` (string or array) | `generationConfig.stopSequences` |
| `n` | only `1` accepted; other values `400` |
| `response_format: {type:"json_object"}` | `generationConfig.responseMimeType: "application/json"` |
| `response_format: {type:"json_schema", json_schema:{schema}}` | `responseMimeType` + `responseSchema` (same cleaning as tool parameters) **[P0]** |
| `seed`, `presence_penalty`, `frequency_penalty` | `seed`, `presencePenalty`, `frequencyPenalty` |
| `reasoning_effort` | `generationConfig.thinkingConfig` (mapping fixed in S3 per model family) **[P0]** |
| `stream`, `stream_options.include_usage` | choose endpoint; emit usage chunk |
| `user`, `logprobs`, `logit_bias`, `parallel_tool_calls`, other unknown | ignored (logged at debug, name only) |

**Thought signatures.** Gemini 2.5+/3 models return `thoughtSignature` on `functionCall` parts and require
it back in the next turn. kiko-wrt is stateless, so it encodes the signature into the OpenAI tool call id:
`id = "call_" + <random 8 bytes hex> + "." + <base64url(signature)>`. On the way back, the signature is
decoded from `tool_call_id`/`tool_calls[].id`. Ids without the suffix are sent without a signature.
**[P0]** check id length limits in the clients (opencode etc.); if a client truncates ids, fall back to a
small in-memory LRU map (id -> signature, 256 entries, 1 h TTL).

#### Response mapping (Gemini -> OpenAI, non-stream)

| Gemini | OpenAI `chat.completion` |
|---|---|
| `candidates[0].content.parts[].text` (concatenated; parts with `thought:true` dropped) | `choices[0].message.content` (null if empty and tool calls exist) |
| `parts[].functionCall {name, args}` | `message.tool_calls[] {id, type:"function", function:{name, arguments: <args serialized>}}` |
| `finishReason STOP` | `stop`, or `tool_calls` if any functionCall present |
| `MAX_TOKENS` | `length` |
| `SAFETY`, `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, `IMAGE_SAFETY` | `content_filter` |
| `MALFORMED_FUNCTION_CALL`, other / missing | `stop` (malformed call also logged at warning) |
| `promptFeedback.blockReason` with no candidates | `200` with empty content and `finish_reason: content_filter` |
| `usageMetadata.promptTokenCount`, `candidatesTokenCount` (+`thoughtsTokenCount`), `totalTokenCount` | `usage.prompt_tokens`, `completion_tokens`, `total_tokens`; thoughts also in `completion_tokens_details.reasoning_tokens` |
| `modelVersion` | ignored; `model` = client-visible name |
| - | `id: "chatcmpl-<random hex>"`, `object`, `created: time(NULL)`, `system_fingerprint: null` |

#### Streaming mapping

Gemini SSE (`alt=sse`) sends `data: <GenerateContentResponse JSON>` events, each holding a delta.
kiko-wrt emits `chat.completion.chunk` events:

1. First chunk: `delta: {role:"assistant", content:""}`.
2. For each Gemini event: text parts -> `delta.content`; each `functionCall` part -> one
   `delta.tool_calls[{index:i, id, type:"function", function:{name, arguments:<full JSON string>}}]`
   (Gemini delivers calls whole, so no argument fragments are needed). `i` counts calls across the
   whole response.
3. When an event carries `finishReason`: chunk with empty delta and mapped `finish_reason`.
4. If `stream_options.include_usage`: one more chunk with `choices: []` and `usage` from the last
   `usageMetadata`.
5. `data: [DONE]`.

All chunks share one `id` and `created`. Response headers to the client: `Content-Type:
text/event-stream`, `Cache-Control: no-cache`, `Connection: close`; body sent with chunked
transfer-encoding (HTTP/1.1) or close-delimited (HTTP/1.0 clients).

### 4.3 SSE passthrough (openai type)

Upstream SSE is parsed by event (lines up to `max_sse_line`, default 1 MiB) only to rewrite `model`
(if enabled) and to detect `[DONE]`; otherwise bytes are forwarded as they arrive. No buffering of the
whole stream. Backpressure: if the client socket's write buffer exceeds 256 KiB, kiko-wrt stops reading
upstream until it drains.

### 4.4 Disconnects and timeouts

- Client disconnect: abort the upstream request immediately (saves tokens).
- Upstream connect timeout 10 s, first-byte timeout `timeout` (default 120 s), idle-between-chunks
  timeout 120 s. Timeout before headers sent -> `504`; after streaming started -> an SSE error event
  (see 5) then close.

## 5. Errors

Always OpenAI format: `{"error":{"message":"...","type":"...","param":null,"code":"..."}}`.

| Situation | Status | type / code |
|---|---|---|
| bad JSON, missing `model`/`messages`, unsupported value | 400 | `invalid_request_error` |
| no/wrong client token | 401 | `authentication_error` |
| model not allowed for this client | 403 | `permission_error` |
| unknown model or path | 404 | `not_found_error` / `model_not_found` |
| body or headers too large | 413 / 431 | `invalid_request_error` / `request_too_large` |
| local rate limit | 429 | `rate_limit_error` + `Retry-After` |
| upstream 400 | 400 | upstream message passed through (truncated to 1 KiB, scrubbed of key-like strings) |
| upstream 401/403 (bad router key) | 502 | `upstream_error` / `upstream_auth` (client's token is fine; the router's key is not) |
| upstream 429 | 429 | `rate_limit_error`, upstream `Retry-After` kept |
| upstream 5xx, TLS failure, DNS failure, bad JSON | 502 | `upstream_error` |
| upstream timeout | 504 | `timeout_error` |
| too many concurrent requests | 503 | `overloaded_error` |

Errors after an SSE stream started: `data: {"error":{...}}` event, then the connection is closed
(no `[DONE]`), which is what OpenAI clients expect.

## 6. Limits (defaults, all configurable)

| Limit | Default |
|---|---|
| request header size | 8 KiB |
| request body (`max_body`) | 1 MiB (images via base64 need more: per-model `max_body` override up to 16 MiB) |
| JSON nesting depth | 64 |
| buffered upstream non-stream body | 8 MiB |
| SSE line | 1 MiB |
| concurrent client connections | 32 |
| concurrent upstream requests | 8 (global), per provider `max_inflight` |
| idle client connection | 30 s before request complete |
| rate limit | 60 requests/min per client token, burst 10 (token bucket); 0 = off |

Memory budget: idle < 8 MB RSS (target ~2 MB); each active request is bounded by body + buffers, so 8
concurrent non-streaming requests with 1 MiB bodies stay under ~30 MB worst case. Routers with 64 MB RAM
should set `max_inflight 2`.

## 7. Configuration (UCI `/etc/config/kiko-wrt`)

```
config kiko_wrt 'main'
	option enabled '1'
	list listen 'lan'              # interface name (resolved via ubus network) or 'IP:port'
	option port '4000'
	option allow_wan '0'           # refuse to bind 0.0.0.0/WAN addresses unless 1
	option log_level 'info'        # error|warn|info|debug
	option max_body '1048576'
	option max_conns '32'
	option max_inflight '8'
	option rate_rpm '60'
	option rate_burst '10'

config client
	option name 'laptop'
	option token_file '/etc/kiko-wrt/tokens/laptop'   # or: option token '...'
	list allow_model 'gpt-mini'                   # empty = all models

config provider 'openai'
	option type 'openai'
	option base_url 'https://api.openai.com/v1'
	option api_key_file '/etc/kiko-wrt/keys/openai'   # preferred; 'option api_key' also accepted
	option timeout '120'
	option max_inflight '4'
	option ca_file ''                             # default: system CA bundle

config provider 'gemini'
	option type 'gemini'
	option base_url 'https://generativelanguage.googleapis.com'
	option api_key_file '/etc/kiko-wrt/keys/gemini'

config model
	option name 'gpt-mini'          # what clients send / see in /v1/models
	option provider 'openai'
	option upstream 'gpt-4.1-mini'
	option max_tokens_cap '4096'

config model
	option name 'gemini-flash'
	option provider 'gemini'
	option upstream 'gemini-2.5-flash'
```

Validation at start and on `reload` (procd `service_triggers` on config `kiko-wrt`): unknown provider,
duplicate model names, missing key file, non-https `base_url` (allowed only with
`option insecure_http '1'` for LAN upstreams like llama.cpp) -> error logged, invalid section skipped;
if no valid model remains the service does not start.

**[P0]** Model names, providers and aliases are copied from kiko's real config.

## 8. Security

- **TLS verification always on** for https upstreams: CA bundle from `ca-bundle` package (or `ca_file`),
  hostname checked. There is no "skip verify" option. Plain http only with explicit `insecure_http` per
  provider.
- **Keys:** stored in `/etc/kiko-wrt/keys/*` (mode 0600, owner `kiko-wrt`), read once at start/reload into memory.
  Keys in `/etc/config/kiko-wrt` are accepted but warned about (UCI files are readable by LuCI/rpcd).
  Never logged, never in URLs, never in error messages; upstream error bodies are scrubbed
  (`sk-...`, `AIza...` and the configured key values replaced by `***`). Memory holding keys and tokens
  is zeroed on reload/exit (`explicit_bzero` or volatile loop).
- `/etc/kiko-wrt/` is added to sysupgrade keep-list (`/lib/upgrade/keep.d/kiko-wrt`).
- **Process:** runs as unprivileged user `kiko-wrt` (procd `user`), no capabilities needed (port > 1024);
  procd jail (`ujail`) with read-only `/etc/kiko-wrt`, `/etc/config/kiko-wrt`, `/etc/ssl` where supported.
- **Binding:** LAN interface addresses only by default; refuses wildcard/WAN unless `allow_wan '1'`.
  No firewall rule is installed; the package documents that WAN access must not be opened.
- **Client tokens:** required by default (a config with no `client` section and `option open '1'`
  allows token-less use on a trusted LAN; default off).
- **Rate limits and caps:** see section 6; also per-model `max_tokens_cap`.
- **Logs:** one line per request: client name, model, provider, status, duration, token usage. No
  prompts, no responses, no headers. `debug` adds field names, never field values.
- **Parsers** (HTTP request, SSE, JSON mapping) are fuzzed (see TESTING.md).

## 9. Non-goals (v1)

- Anthropic Messages API (upstream or client side), OpenAI Responses API, legacy `/v1/completions`,
  embeddings, images, audio, files, batches, assistants.
- Spend tracking, budgets, a database, virtual-key management UI, caching of responses, retries with
  fallbacks across providers (maybe v1.1 if Phase 0 shows kiko uses fallbacks).
- HTTPS on the LAN listener (use a reverse proxy if needed), HTTP/2, WebSockets.
- Fetching remote image URLs, tokenizing/counting tokens locally.
- `n > 1`, logprobs for Gemini.
- Windows/macOS builds (desktop Linux only for development).
