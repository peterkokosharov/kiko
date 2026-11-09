# kikolib - kiko-proxy status and dependency inventory

Date: 2026-10-28. Branch `kiko-main`, HEAD `9997220`. Measured on Windows 11, Python 3.14 dev venv.

## 1. Status of kiko-proxy

| Item | State |
|---|---|
| What it is | Fork of LiteLLM v1.104.2, version 0.2.0. FastAPI app in `litellm/proxy/kiko_server.py` (~1600 lines), SQLite billing in `litellm/proxy/integrations/kiko_sqlite_logger.py`. |
| Routes | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/messages`, `/v1/messages/count_tokens`, `/v1/responses` (with `previous_response_id`), `/v1/models`, `/v1beta/models/{id}:generateContent / :streamGenerateContent / :countTokens`, `/health`, `/kiko/models`, `/kiko/models/{name}/class`, `/kiko/shutdown`. |
| Start | `kiko` console script -> `run_server()` (argparse `--config --port --host`, uvicorn, default `127.0.0.1:4000`). Portable: `kiko_start.py` puts `lib/` on `sys.path` and calls `run_server()`. |
| Secrets | `MASTER_KEY` and provider keys from env or OS store: Windows Credential Manager via ctypes; secret-tool / dcop kwalletd elsewhere (already stdlib-only). |
| Build | `kiko_build.py working / release / linux / portable`. Portable mode: `uv build` the wheel, `pip download` all runtime wheels, embed Python 3.12.7 (amd64), unpack every wheel into `kiko/lib/`. No pip at runtime, but every dependency wheel is shipped. |
| Tests | `tests/kiko/` 33 files. Run 2026-10-28: 227 passed, 7 skipped (day smoke tests deselected; they need a live server). |
| Recent work | Day smoke tests + DB verification, portable install, `/kiko/shutdown`, `/kiko/models` class endpoints, SQLite WAL, `previous_response_id`, 429 Retry-After, local price table. |
| Import cost | `import litellm.proxy.kiko_server` takes ~7 s cold and loads ~45 third-party top-level modules. |

## 2. Third-party packages on the real code path

Method: imported `litellm.proxy.kiko_server`, called `create_app()`, loaded a uvicorn
`Config`, then listed non-stdlib top-level modules in `sys.modules` and which litellm
module imported each first. Transitive counts are the dependency closure from `uv.lock`
(or installed metadata where `uv.lock` is stale). Whole runtime closure in `uv.lock`: 53
packages; the dev venv has 89.

Note: `uv.lock` is stale. pyproject declares `tokenizers` but the lock does not list it;
`cryptography` is imported at start but declared nowhere (it arrives in the venv indirectly).

Verdicts: (a) stdlib, (b) small kikolib module, (c) OS native lib via ctypes/subprocess,
(d) keep / optional for now, (e) drop the feature or keep it off the kiko path.

### Hard runtime (declared and loaded at start)

| Package | Used for in kiko | Transitive | Verdict |
|---|---|---|---|
| tokenizers | HF tokenizer for Claude/Llama token counts (`litellm_core_utils/tokenizer.py`), loaded at import | ~10 (huggingface-hub, fsspec, hf-xet, tqdm, filelock, click, httpx, packaging, pyyaml, typing-extensions) | (e) then (b) `tokens.py`. Need mostly disappears: billing uses provider `usage`; count_tokens forwards to provider / local `/tokenize`. |
| tiktoken | cl100k_base encoding at import (`default_encoding.py`), `litellm.token_counter` behind the two count_tokens routes and stream-usage fallback | 6 (regex, requests, urllib3, charset-normalizer, idna, certifi) | (e) then (b) `tokens.py` (BPE over the vocab files already bundled in litellm). |
| httpx[http2] | Upstream HTTP client and its types/exceptions across ~530 litellm files | 9 (httpcore, h11, anyio, sniffio, idna, certifi, h2, hpack, hyperframe) | (d) now; drop the `http2` extra at once (a); (b) `http.py` long term - biggest, riskiest port. |
| aiohttp | litellm's default async transport (`custom_httpx/http_handler.py`) | 9 (aiohappyeyeballs, aiosignal, attrs, frozenlist, multidict, propcache, yarl, idna, typing-extensions) | (a) set `disable_aiohttp_transport = True` so httpx is used; then drop. |
| openai | Request/response types, exceptions, SDK clients across ~70 files | 16 (incl. jiter, distro, tqdm, colorama, httpx, pydantic) | (d) keep; deeply woven into litellm. |
| pydantic (+pydantic-core) | litellm types/models everywhere | 4 (pydantic-core, annotated-types, typing-inspection, typing-extensions) | (d) keep; no realistic small replacement. |
| fastapi | kiko app, routing, Request/JSONResponse/StreamingResponse, auth dependency | 9 (starlette, annotated-doc, anyio, pydantic, ...) | (d) now; (b) `server.py` last. |
| uvicorn | ASGI server in `run_server()` | 2 (click, h11); also loads websockets | (d) now; (b) `server.py` together with fastapi. |
| python-multipart | fastapi form parsing (no kiko route uses forms) | 0 | (e) drop once fastapi allows / with `server.py`. |
| pyyaml | `kiko-config.yaml` loading in `_load_config()` / `run_server()` | 0 (native wheel) | (b) `yaml_lite.py`, or (a) switch config to TOML (`tomllib`). |
| python-dotenv | `load_dotenv()` at import of litellm, main, utils | 0 | (b) `dotenv.py` (~40 lines) or (e) drop .env loading. |
| jinja2 (+markupsafe) | HF chat templates in `prompt_templates/factory.py`, prompt-manager integrations | 1 | (e) keep off the kiko path (lazy import); kiko routes do not render templates. |
| jsonschema | `json_validation_rule.py`, lazy, only for response_format validation | 5 (attrs, referencing, jsonschema-specifications, rpds-py, typing-extensions) | (e) drop dependency; (b) `jsonschema_lite.py` only if needed. |
| pydantic-settings | OpenTelemetry integration config only, but loaded at start | 6 (python-dotenv, pydantic, ...) | (e) drop with otel. |
| fastuuid | `litellm/_uuid.py` | 0 (native wheel) | (a) stdlib `uuid`. |
| importlib-metadata | `litellm/_version.py` | 1 (zipp) | (a) stdlib `importlib.metadata`. |
| packaging | langfuse / lunary integrations, CLI `claude_settings` | 0 | (e) not on kiko path; (a) tuple compare if ever needed. |
| filelock | CLI `claude_settings` command only | 0 | (e) not on kiko path; drop. |
| click | litellm CLI; uvicorn dependency | 0 | (e) leaves with uvicorn; kiko uses argparse. |

### Not declared, but loaded at start

| Package | Used for | Transitive | Verdict |
|---|---|---|---|
| cryptography | OCI request signing (try-import at start), JWT auth, license check, AES-GCM for SQS logger | 2 (cffi, pycparser) | (e) keep off the start path; (c) `crypto.py` via BCrypt / libcrypto / CommonCrypto if a feature needs it. |
| requests | tiktoken BPE download, otel TLS plumbing | 4 (urllib3, charset-normalizer, idna, certifi) | (e) leaves with tiktoken and otel; (a) `urllib.request` if ever needed. |
| certifi | CA bundle in `http_handler.py` | 0 | (a)/(c) OS trust store through `ssl.create_default_context` (`http.ca_context`). |
| opentelemetry-api, pygments, websockets | otel integration; pulled by imported libs; uvicorn ws support | 1 / 0 / 0 | (e) not used by kiko. |

### Optional extras and dev

| Package | Group | Verdict |
|---|---|---|
| boto3 (+botocore, s3transfer, jmespath, ...) | `aws` extra (Bedrock / Sagemaker) | (d) optional; not in default install. |
| orjson, uvloop | `perf` extra | (d) optional; stdlib `json` / default loop are fine. |
| pytest, pytest-asyncio, pytest-mock, respx, httpx, psutil | dev group | (d) dev only; allowed via venv. |

## 3. Suggested porting order

Biggest dependency win and least risk first. Steps 1-4 need little or no kikolib code.

1. **Free wins (stdlib):** `fastuuid` -> `uuid`, `importlib-metadata` -> `importlib.metadata`,
   drop the `httpx` `http2` extra, set `disable_aiohttp_transport` and drop `aiohttp`.
   About 14 packages gone.
2. **Keep unused integrations off the start path:** make the OCI, JWT, otel, prompt-manager
   and JSON-validation imports lazy so `cryptography`, `pydantic-settings`, `jinja2`,
   `jsonschema`, `requests`, `opentelemetry`, `filelock`, `packaging` are no longer loaded;
   then remove them from `pyproject.toml`. About 15 packages, no new code.
3. **Tokens (`tokens.py`):** forward count_tokens to the provider / local `/tokenize`, add an
   estimate fallback, then remove `tiktoken` and `tokenizers` (~16 packages incl.
   huggingface-hub and three Rust wheels). Optional pure-Python BPE later.
4. **Small loaders:** `dotenv.py`, `yaml_lite.py` (or TOML config). Removes two packages and
   one native wheel.
5. **certifi -> OS trust store** (`http.ca_context`), verified on each platform.
6. **HTTP client (`http.py`):** replace httpx behind litellm's HTTP handler. Large surface
   (httpx types across litellm); plan it as its own project.
7. **Server (`server.py`):** replace fastapi / starlette / uvicorn / anyio. Last; tests
   rely on the fastapi test client.

`openai` and `pydantic` stay until a later decision; they are woven through litellm's
types and are the remaining floor after steps 1-7.

Sources: `pyproject.toml`, `uv.lock`, `.venv` installed metadata, runtime import trace of
`litellm.proxy.kiko_server` + `create_app()` + uvicorn `Config.load()`, `kiko_build.py`,
`git log`, `pytest tests/kiko` - all read 2026-10-28 in this repository.

## 4. Update 2026-10-28 (kiko-lite, kikolib 0.1.0)

Direction changed: kiko-lite uses threads (no asyncio), standard library only, Python 3.11 floor.
Each pluggable part has an interface plus a factory (`kikolib/factory.py`) that reports its choice
for `kiko doctor`. Current layout of `kikolib/`:

- `factory.py`, `posix.py`, `dotenv.py`, `tokens.py` (estimate only), `keystore.py` - done.
- `keystore.py`: keys are read env var first, then the OS store (Windows Credential Manager,
  macOS Keychain, secret-tool, kwallet-query, dcop). No third-party keyring.
- `yaml_lite.py`: read-only subset, used only for a one-time import of an old `kiko.yaml`; settings
  live in SQLite, there is no config file.
- `native.py`: removed (it was an empty placeholder for a libcurl http backend).
- `http.py`, `server.py`: still stubs (steps B/C).
- `crypto.py`, `jsonschema_lite.py`: removed (not needed by kiko-lite).
