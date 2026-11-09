# Архитектура

kiko-lite има два Python пакета. **kikolib** е общ набор инструменти на стандартна библиотека, който не знае нищо за
модели. **kiko_lite** е приложението, построено върху него.

## Компоненти

| Пакет | Модул | Задача |
|---|---|---|
| kikolib | `http.py` | HTTP клиент върху `http.client` + `ssl`: keep-alive пул, поточни редове, proxy CONNECT, хранилище за доверие на ОС |
| kikolib | `server.py` + `sse.py` | многонишков HTTP сървър, router, streaming отговори, SSE кодиране/ping/парсер, `serve()` със сигнали и drain |
| kikolib | `retry.py` | политика за повторни опити: backoff + jitter, `Retry-After`, мек времеви бюджет, парчета `on_wait` (ползват се за SSE ping) |
| kikolib | `keystore.py` | API ключове: първо средата, после хранилищата на ОС (wcm, keychain, secret-tool, kwallet, dcop) |
| kikolib | `factory.py` | регистър, който избира имплементация по проба и приоритет (keystore, http) |
| kikolib | `posix.py` | сигнали, RSS, systemd notify/watchdog, частни файлове |
| kikolib | `yaml_lite.py` | YAML подмножество само за четене, само за еднократния внос на стар `kiko.yaml` |
| kikolib | `dotenv.py` | четец на `.env` |
| kikolib | `tokens.py` | резервна оценка на токени за `count_tokens` |
| kiko_lite | `app.py` | gateway: автентикация, план, изпращане с повторни опити, stream/non-stream, преобразуване на грешки, маршрути |
| kiko_lite | `resolver.py` | резолюция на клас/alias, снимка на каталога от базата, писач на фактуриране, портал за месечен лимит |
| kiko_lite | `providers/` | таблица с доставчици + `gemini.py` (роден преводач) + `openai_compat.py` (преводач на OpenAI chat) |
| kiko_lite | `db.py` | SQLite: models, model_classes, aliases, settings, billing; миграции |
| kiko_lite | `cli.py` | `python -m kiko_lite <command>` |

### Диаграма на компонентите

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
        GEM["providers/gemini (роден преводач)"]
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
    APP -->|"ред за фактуриране"| DB
    CLI --> DB
    CLI --> DOT
```

## Основни класове

Прочетени от реалния код (`kiko_lite/app.py`, `kiko_lite/resolver.py`, `kiko_lite/db.py`, `kikolib/`).

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

И двата преводача се казват `StreamTranslator` в собствения си модул (`providers/openai_compat.py` и
`providers/gemini.py`); диаграмата им дава различни имена само за да се различават. Таблицата с доставчици са
обикновени данни (`providers/__init__.py`, по един dict на доставчик), не класове.

## Една заявка със streaming

```mermaid
sequenceDiagram
    autonumber
    participant CC as Claude Code
    participant S as server (SSE)
    participant G as _Gateway
    participant R as DBResolver
    participant W as работна нишка
    participant T as преводач
    participant U as Upstream доставчик
    CC->>S: POST /v1/messages (stream=true)
    S->>G: messages(req)
    G->>R: resolve(име на модел)
    R-->>G: доставчик и идентификатор на модела при него
    G->>G: _plan - ключ от keystore, to_openai или to_gemini
    G-->>CC: SSE ping веднага
    G->>W: стартира работник
    loop send_with_retry
        W->>U: POST (stream)
        U-->>W: 429, 5xx или грешка при връзка
        W->>G: on_wait слага ping в опашката
        G-->>CC: SSE ping (keep-alive по време на изчакване)
    end
    U-->>W: 200 и поток от събития
    W->>G: опашка - open, после data събития
    loop всяко upstream събитие
        G->>T: feed(event)
        T-->>G: Anthropic събития
        G-->>CC: message_start, content_block_delta ...
    end
    G-->>CC: message_delta, message_stop
    G->>G: on_result записва ред за фактуриране в SQLite
```

Ако `ping_interval` секунди (по подразбиране 10) не пристигне нищо, генераторът сам изпраща ping. Ако повторните опити
се изчерпат, клиентът получава `error` събитие в стила на Anthropic, а не увиснала връзка.

## Внедряване

```mermaid
flowchart TB
    subgraph NOW["Сега: домашен компютър (Windows 11, Linux Mint, Q4OS)"]
        CC1["Claude Code, roni"] -->|"127.0.0.1:4000"| K1["kiko-lite: python -m kiko_lite serve"]
        K1 --- DB1[("~/.kiko/kiko.db")]
        K1 --- KS1["хранилище за ключове на ОС"]
    end
    subgraph LATER["По-късно, планирано: домашен рутер с OpenWrt"]
        LAN["Клиенти в домашната LAN"] -->|"LAN порт 4000"| K2["kiko-lite под procd"]
        K2 --- DB2[("kiko.db на USB или флаш")]
        K2 --- ENV2["файл .env до базата"]
    end
    K1 -->|HTTPS| NET(("Интернет: доставчици на модели"))
    K2 -->|HTTPS| NET
```

Вариантът с рутер е план, вж. [ROADMAP](ROADMAP.md). На Linux без десктоп хранилище за ключове ключовете идват от
средата или от файл `.env` до файла на базата.
