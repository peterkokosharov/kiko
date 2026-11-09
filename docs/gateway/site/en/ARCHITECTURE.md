# Architecture

kiko-lite has two Python packages. **kikolib** is a generic standard-library toolkit that knows nothing about
models. **kiko_lite** is the application built on it.

## Components

| Package | Module | Job |
|---|---|---|
| kikolib | `http.py` | HTTP client on `http.client` + `ssl`: keep-alive pool, streamed lines, proxy CONNECT, OS trust store |
| kikolib | `server.py` + `sse.py` | threaded HTTP server, router, streaming responses, SSE encode/ping/parser, `serve()` with signals and drain |
| kikolib | `retry.py` | retry policy: backoff + jitter, `Retry-After`, soft time budget, `on_wait` slices (used for SSE pings) |
| kikolib | `keystore.py` | API keys: env first, then OS stores (wcm, keychain, secret-tool, kwallet, dcop) |
| kikolib | `factory.py` | registry that picks an implementation by probe and priority (keystore, http) |
| kikolib | `posix.py` | signals, RSS, systemd notify/watchdog, private files |
| kikolib | `yaml_lite.py` | read-only YAML subset, only for the one-time import of an old `kiko.yaml` |
| kikolib | `dotenv.py` | `.env` reader |
| kikolib | `tokens.py` | fallback token estimate for `count_tokens` |
| kiko_lite | `app.py` | gateway: auth, plan, send with retry, stream/non-stream, error mapping, routes |
| kiko_lite | `resolver.py` | class/alias resolution, DB catalog snapshot, billing writer, monthly cap gate |
| kiko_lite | `providers/` | provider table + `gemini.py` (native translator) + `openai_compat.py` (OpenAI chat translator) |
| kiko_lite | `db.py` | SQLite: models, model_classes, aliases, settings, billing; migrations |
| kiko_lite | `cli.py` | `python -m kiko_lite <command>` |

### Component diagram

```mermaid
flowchart LR
    CC["Claude Code / roni"] -->|"POST /v1/messages (Anthropic API)"| SRV
    subgraph kikolib
        SRV["server + sse"]
        HTTP["http (http.client + ssl)"]
        RETRY["retry"]
        KS["keystore"]
        FAC["factory"]
        DOT["dotenv / yaml_lite / posix / tokens"]
    end
    subgraph kiko_lite
        APP["app: _Gateway"]
        RES["resolver: DBResolver + Catalog"]
        GEM["providers/gemini (native translator)"]
        OAI["providers/openai_compat"]
        DB[("db: SQLite")]
        CLI["cli"]
    end
    SRV --> APP
    APP --> RES
    RES --> DB
    APP --> GEM
    APP --> OAI
    APP --> RETRY
    RETRY --> HTTP
    APP --> KS
    KS --> FAC
    HTTP --> FAC
    HTTP --> UP["Upstream: Gemini, NVIDIA, Groq, Qwen, OpenAI"]
    APP -->|"billing row"| DB
    CLI --> DB
    CLI --> DOT
```

## Main classes

Read from the real code (`kiko_lite/app.py`, `kiko_lite/resolver.py`, `kiko_lite/db.py`, `kikolib/`).

```mermaid
classDiagram
    class App {
        +route(method, path)
        +before(fn)
        +after(fn)
        +match(method, path)
    }
    class Server {
        +serve_forever()
        +start()
        +drain(timeout)
        +stop()
    }
    class _Gateway {
        +messages(req)
        +count_tokens(req)
        +authenticate(req)
        -_plan(body)
        -_sender(plan, calls, cancel)
        -_stream(plan, body, res, calls, t0)
        -_nonstream(plan, body, res, calls)
    }
    class _Plan {
        provider
        upstream_model
        url
        stream
        new_stream()
        parse()
    }
    class RequestResult {
        request_id
        client_model
        provider
        upstream_model
        attempts
        status
        usage
    }
    class DBResolver {
        +__call__(model)
    }
    class Catalog {
        +snapshot()
        +invalidate()
    }
    class Snapshot {
        aliases
        classes
        models
        +best(cls)
    }
    class Database {
        +classes()
        +add_class(name, keyword, role)
        +models()
        +upsert_model(alias, provider, api_key_env)
        +add_billing(model, tokens_in, tokens_out, cost_usd)
        +month_spent()
    }
    class BillingWriter {
        +__call__(result)
    }
    class RetryPolicy {
        attempts
        base_delay
        max_delay
        budget
        +delay_for(attempt, retry_after)
    }
    class StdlibBackend {
        +request(method, url, headers, body)
        +close()
    }
    class OpenAIStreamTranslator {
        +feed(chunk)
        +finish()
    }
    class GeminiStreamTranslator {
        +feed(event)
        +finish()
    }
    Server --> App
    App --> _Gateway : routes
    _Gateway --> DBResolver
    _Gateway --> _Plan
    _Gateway --> RetryPolicy
    _Gateway --> StdlibBackend
    _Gateway --> RequestResult
    _Gateway ..> BillingWriter : on_result
    _Plan --> OpenAIStreamTranslator
    _Plan --> GeminiStreamTranslator
    DBResolver --> Catalog
    Catalog --> Snapshot
    Catalog --> Database
    BillingWriter --> Database
```

Both translators are called `StreamTranslator` in their own module (`providers/openai_compat.py` and
`providers/gemini.py`); the diagram gives them distinct names only to tell them apart. The provider table is plain
data (`providers/__init__.py`, one dict per provider), not classes.

## One streaming request

```mermaid
sequenceDiagram
    autonumber
    participant CC as Claude Code
    participant S as server (SSE)
    participant G as _Gateway
    participant R as DBResolver
    participant W as worker thread
    participant T as translator
    participant U as Upstream provider
    CC->>S: POST /v1/messages (stream=true)
    S->>G: messages(req)
    G->>R: resolve(model name)
    R-->>G: provider and upstream model id
    G->>G: _plan: key from keystore, to_openai or to_gemini
    G-->>CC: SSE ping at once
    G->>W: start worker
    loop send_with_retry
        W->>U: POST (stream)
        U-->>W: 429, 5xx or connect error
        W->>G: on_wait puts a ping in the queue
        G-->>CC: SSE ping (keep-alive while waiting)
    end
    U-->>W: 200 and event stream
    W->>G: queue: open, then data events
    loop every upstream event
        G->>T: feed(event)
        T-->>G: Anthropic events
        G-->>CC: message_start, content_block_delta ...
    end
    G-->>CC: message_delta, message_stop
    G->>G: on_result writes a billing row to SQLite
```

If nothing arrives for `ping_interval` seconds (default 10), the generator sends a ping by itself. If the retries
run out, the client gets an Anthropic-style `error` event, not a hung connection.

## Deployment

```mermaid
flowchart TB
    subgraph NOW["Now: home PC (Windows 11, Linux Mint, Q4OS)"]
        CC1["Claude Code, roni"] -->|"proxy port, 4000 by default"| K1["kiko-lite: python -m kiko_lite serve"]
        K1 --- DB1[("kiko.db and kiko.toml in the config folder")]
        K1 --- KS1["OS key store"]
    end
    subgraph LATER["Later, planned: home router with OpenWrt"]
        LAN["Home LAN clients"] -->|"LAN, proxy port"| K2["kiko-lite under procd"]
        K2 --- DB2[("kiko.db on USB or flash")]
        K2 --- ENV2[".env file next to the DB"]
    end
    K1 -->|HTTPS| NET(("Internet: model providers"))
    K2 -->|HTTPS| NET
```

The router variant is a plan, see [ROADMAP](ROADMAP.md). On Linux without a desktop key store, keys come from env or
a `.env` file next to the DB file.
