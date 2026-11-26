# libkiko - Изисквания (преносимо C ядро)

> Превод на LIBKIKO-REQUIREMENTS.md (английският оригинал е водещ), 2026-11-25.

Статус: изисквания, стъпка 1 от 2 (2026-11-25). Този документ казва **какво трябва да прави libkiko**, а не
кои компоненти го реализират; стъпка 2 е търсенето на компоненти (раздел 6). Където се различава от
[PLAN.md](PLAN.md) (който е само за OpenWrt и е вързан за uloop / uclient / ustream-ssl), този документ има
предимство за libkiko; PLAN.md продължава да описва стъпките на прокси-то kiko-wrt.

## 1. Цел и потребители

libkiko е C братът на Python `kikolib` + заявъчния конвейер на `kiko_lite`: приема chat заявка в известен
wire формат, избира доставчик, превежда заявката, изпраща я по HTTPS и връща (или стриймва) отговора във
wire формата на извикващия. Той е **преносим**: едно чисто C ядро без I/O плюс един малък I/O backend за
всяка платформа, избиран при компилация.

| Потребител | Как използва libkiko | От какво има най-голяма нужда |
|---|---|---|
| **Layka** (C++ терминален coding агент, Windows + Linux) | линква libkiko в процеса (стъпка 2 от миграцията му: едно приложение вместо приложение + Python rex процес) | неблокиращи извиквания от UI нишката, стриймване чрез callback, cancel, OS хранилище за ключове, OS TLS / proxy |
| **lib_rex** (по-късно, C++ цикъл на агента) | вика libkiko за всеки ход на модела, често няколко паралелно (подагенти) | същото като Layka + едновременни заявки върху един gateway handle |
| **kiko-wrt** (OpenWrt демон под procd) | линква libkiko статично зад собствения си HTTP сървър | малък размер, равен RSS, интеграция с еднонишков event loop, ключове в 0600 файлове |

Архитектура, фиксирана от собственика (не се решава наново тук): чисто C ядро без I/O; I/O backend-и за
отделните платформи зад един малък интерфейс; преизползване на зряло пермисивно open-source C там, където
съществува; C11; `zig cc` локално, release компилациите в CI.

## 2. Функционални изисквания

Правило за обхвата: **v1 = това, което kikolib / kiko_lite правят днес и от което се нуждае потребител по-горе.**
Всичко друго е отбелязано като "not in v1". Препратките към файлове са спрямо `gateway/`, освен ако не е
казано друго.

### 2.1 Модел на заявката / отговора

- **R1** Публичният формат на заявката е **OpenAI chat completions** (`messages`, `tools`, `tool_choice`,
  `max_tokens` / `max_completion_tokens`, `temperature`, `top_p`, `stop`, `stream`,
  `stream_options.include_usage`), същата повърхност като `kiko_runtime_lib.Gateway.chat`
  (`kiko_runtime_lib/gateway.py`) и `kiko_lite/chat_front.py`.
- **R2** Тялото на **Anthropic Messages** е вътрешният опорен (pivot) формат, точно както в kiko_lite
  (docstring на `kiko_lite/app.py`: всеки front конвертира към Messages, всеки доставчик конвертира от него).
  Затова Anthropic Messages front-ът (семантика на `POST /v1/messages`, със и без stream) също е публичен във
  v1 без допълнителна цена за превод. Решение: запазваме pivot-а на kiko_lite, така че C портът да е 1:1 с
  тестваното Python и golden fixtures да могат да се споделят.
- **R3** Заявките и отговорите пресичат API-то като **JSON текст** (UTF-8 байтове + дължина), а не като C
  обектен модел. Извикващият (C++ Layka, C kiko-wrt) държи собствена JSON библиотека; libkiko сам си
  парсира.
- **R4** Резултат без stream: един пълен `chat.completion` (или Anthropic `message`) JSON с `usage`; съпоставка
  на `finish_reason` както в `chat_front._FINISH` (`end_turn->stop`, `max_tokens->length`,
  `tool_use->tool_calls`, `refusal->content_filter`).
- **R5** Тялото на клиента никога не се препраща както е: тялото към upstream се изгражда от **whitelist** на
  полета (docstring на `providers/openai_compat.py`); неизвестните полета се игнорират; `n > 1` е 400
  (`chat_front.py`).
- **R6** Разрешаване на модел: извикващият подава таблица `име на клиентски модел (glob) ->
  "provider/upstream-id"` и по избор таблица с класове (ключова дума opus / sonnet / haiku / fable -> цел),
  както `kiko_lite/app.py SimpleResolver` и `Gateway.from_config(models=, classes=)`. Разрешаване през база
  данни (`resolver.py DBResolver`) **не е във v1** (приложението владее собственото си хранилище).
- **R7** Failover: за една заявка до 3 подредени кандидата (`app.py MAX_ATTEMPTS`); преминава към следващия
  кандидат само при 429 / 408 / 5xx / timeout / грешка при връзка, **никога след първия байт от upstream
  тялото**, никога при други 4xx. Per-(provider, model) **cooldown** в паметта според Retry-After / Gemini
  `RetryInfo`, иначе 60 s, максимум 3600 s (`app.py Cooldowns`, `cooldown_seconds`). kiko-wrt може да
  конфигурира един кандидат (SPEC 9).
- **R8** Списък на конфигурираните имена на клиентски модели (във формата на списъка на OpenAI `/v1/models`)
  от таблицата на resolver-а (`app.py gw.models`). Извличане на собствения списък с модели на upstream:
  **не е във v1**.
- **R9** Оценка на токените, когато upstream не върне usage: `ceil(utf8_bytes/4)` + 4 на съобщение + 8 на
  tool (`kikolib/tokens.py`); може да се ползва и като самостоятелно извикване `count_tokens`. Не е
  токенизатор.

### 2.2 Доставчици и превод

- **R10** Таблицата на доставчиците е обикновени данни, един запис на доставчик: `base_url`, `kind`
  (`openai` | `gemini`), `key_names` (пробват се по ред), `auth` (`bearer` | `header:<Name>`), `flags`
  (`max_tokens_cap`, `max_tokens_field`, `stream_usage`, `mistral`) - точно `kiko_lite/providers/__init__.py
  PROVIDERS`. Вградени стойности по подразбиране за nvidia, groq, dashscope, openai, mistral, gemini (+
  псевдоними qwen, google, nvidia_nim); извикващият може да добавя / замества / премахва записи (както
  `provider_table(overrides)`).
- **R11** Вид `openai`: Messages -> chat completions и обратно (`providers/openai_compat.py`): двупосочно
  преобразуване на префикса `toolu_` на id, невалидни JSON аргументи -> `{"_raw": text}` и обратно,
  tool_result -> съобщения `role: tool`, изображения в tool results -> текстова бележка, `reasoning_content` /
  `reasoning` / водещ `<think>` -> thinking блок, засичане на stop-последователности, особености на Mistral
  (tool_choice `any`, премахване на `$id`/`$schema`, без `stream_options`), `max_completion_tokens` за OpenAI.
- **R12** Вид `gemini`: Messages -> native `generateContent` / `streamGenerateContent` и обратно
  (`providers/gemini.py`): суфикс на id с thought-signature `toolu_<hex>__thought__<b64>` и фиктивна
  signature за Gemini 3+, `thinking` -> `thinkingConfig`, почистване на tool схеми (разрешаване на `$ref`,
  дълбочина 32, 5000 възела, само формати `enum` / `date-time`), safety finish -> `refusal`, header
  `x-goog-api-key`, блокирани prompt-ове.
- **R13** Извиквания на tool / function в двете посоки за двата вида, включително със stream (делти на
  аргументите за извикването, което се изгражда; делти извън ред за вече затворено извикване се отхвърлят,
  както в `openai_compat.StreamTranslator`).
- **R14** Входни изображения: `data:` URL -> base64 източници; http(s) URL се предават като URL само към вид
  `openai`. Gemini получава само inline base64. libkiko **никога не изтегля** отдалечен URL сам.
- **R15** Toolshim (`kiko_lite/toolshim.py`): по модел, по избор (opt-in), превръща tool call, записан като
  текст (`<tool_call>`, `<TOOLCALL>`, `[TOOL_CALLS]`, `<|python_tag|>`, чист / ограден JSON), в истински tool
  call, само за имена от tools на заявката. Във v1, защото Layka ползва безплатни модели, които се нуждаят
  от това.
- **R16** Не във v1: Responses front (`responses_front.py`) и Gemini front (`gemini_front.py`) - kiko
  обслужва codex / Gemini CLI с тях, никой потребител на libkiko не ги иска; embeddings; генериране на
  изображения; аудио; файлове; batches; `n > 1`; logprobs; Anthropic като upstream вид; цени / billing /
  месечни лимити (`prices.py`, `db.py`); management API (`management.py`); WebSockets
  (`kikolib/websocket.py`).

### 2.3 Streaming / SSE

- **R17** SSE парсер с правилата на WHATWG както в `kikolib/sse.py Parser` (`feed` / `finish`: `event`,
  съединяване на многоредов `data`, коментарите се игнорират, dispatch при празен ред), захранван с **всяко
  разделяне на байтове** с идентични събития.
- **R18** SSE енкодер: `encode(event, data)` и `ping` като SSE коментарен ред (`: ping`), както в `sse.py`.
- **R19** Стриймваният изход към извикващия е последователност от JSON обекти `chat.completion.chunk` (или
  Anthropic stream събития), доставяни **по един на callback**; маркерът `[DONE]` се консумира, не се
  доставя (`kiko_runtime_lib` `_iter_chunks`). Chunk-овете съдържат само `id`, `object`, `created`, `model`,
  `choices`; всеки фрагмент на tool-call носи `id` и `type`; незадължителен usage chunk с празен `choices`
  (`chat_front`).
- **R20** Грешка, след като стриймването е започнало, се доставя като терминално събитие за грешка (OpenAI:
  error chunk / Anthropic: събитие `error`), никога като тих край.
- **R21** Keep-alive: докато чака повторен опит или мълчащ upstream, ядрото може да излъчва ping тикове, за
  да може прокси (kiko-wrt) да пише SSE коментари (`retry.py on_wait` интервали, `PING_INTERVAL` 10 s).

### 2.4 Повторни опити, timeout-и, cancel

- **R22** Политика за повторни опити както `kikolib/retry.py RetryPolicy`: опити 5, база 1 s, максимум 20 s,
  мек бюджет 90 s, статуси {408, 409, 425, 429, 500, 502, 503, 504, 529}, пълен jitter с **инжектируем rng и
  часовник**.
- **R23** Парсиране на `Retry-After`: секунди-делта и HTTP-date (`parse_retry_after`), ограничено от
  оставащия бюджет.
- **R24** Повторни опити само **преди първия байт от тялото** да стигне до извикващия.
- **R25** Timeout-и: connect (по подразбиране 10 s), първи байт / четене (по подразбиране 60 s, с
  припокриване по модел както `app.py _model_timeout`) и stream idle; всички се задават от извикващия.
- **R26** **Cancel** по всяко време от всяка нишка: спира повторните опити и failover, прекратява upstream
  заявката, доставя точно едно терминално завършване "cancelled", после повече callback-ове. Поправя
  известното ограничение на Python (`kikolib/README.md`: TLS четене на Windows не може да се прекрати до
  timeout): в C cancel трябва да върне управлението на извикващия **веднага**, дори backend нишката още да
  се забави.

### 2.5 Грешки

- **R27** Един модел на грешките: enum вид (`config`, `bad_request`, `auth`, `not_found`, `rate_limited`,
  `upstream`, `connect`, `tls_verify`, `connection_lost`, `timeout`, `cancelled`, `too_large`, `internal`) +
  HTTP-подобен статус + кратко съобщение + по избор `retry_after` секунди. Огледало на класовете грешки в
  `http.py` и `app.py map_upstream_error`.
- **R28** Upstream 401 / 403 -> 502 с общо съобщение (иначе биха изглеждали като грешен ключ на клиента);
  останалите upstream съобщения се почистват от API ключа и се съкращават (`app.py _scrub`,
  `_upstream_message`).
- **R29** Телата на грешките във wire формата на извикващия (OpenAI `{"error": {...}}` или формата на
  Anthropic), както `chat_front.error_body` / `app.error_body`.

### 2.6 Тайни / хранилище за ключове

- **R30** Тайните се получават чрез **callback, подаден от извикващия**, `secret(name) -> value` (както
  `Gateway.from_config(secret=)`); ядрото никога само не чете env и не съхранява.
- **R31** По избор помощник за платформено хранилище на ключове (отделен модул, в backend слоя) с реда на
  kikolib: първо environment, после OS хранилище. Windows: Credential Manager (както `keystore.py WcmStore`).
  Linux desktop: Secret Service (както `SecretToolStore`); KWallet / DCOP / Keychain не са във v1. OpenWrt:
  обикновен файл 0600, не symlink, собственост на потребителя на демона (ред keystore в PLAN). Имена
  `[A-Za-z0-9_]`.
- **R32** Паметта с тайни се нулира при освобождаване (`explicit_bzero` или volatile цикъл) - SPEC 8.

### 2.7 Hook за логване

- **R33** Един log callback `(level, component, message)`; по подразбиране = мълчи. Без лог файлове, без
  глобален logger. Никога не логва ключове, prompt-ове, отговори или стойности на header-и (docstring на
  `app.py`, SPEC 8).
- **R34** Един резултатен запис на заявка чрез callback (`app.py on_result` / `RequestResult`): модел,
  доставчик, статус, продължителност, използвани токени, опити. Без съдържание.

### 2.8 Входна конфигурация

- **R35** Цялата конфигурация се подава от вграждащото приложение като **структури в паметта / JSON текст**:
  таблица на доставчиците, таблица на моделите, таблица на класовете, политика за повторни опити,
  timeout-и, лимити, TLS опции (припокриване на CA файл, proxy). **Без формат на конфигурационен файл в
  ядрото** (без парсиране на TOML / UCI / YAML; `config.py`, `tomlwrite.py`, `yaml_lite.py`, `dotenv.py`
  остават извън).
- **R36** Gateway може да бъде **преконфигуриран** атомарно (презареждане с SIGHUP в kiko-wrt) без да се
  прекъсват заявките в изпълнение: новите заявки ползват новата конфигурация, текущите завършват със старата.

### 2.9 Нишки / event модел

- **R37** Заявките са **асинхронни**: `start` връща веднага handle на заявката; резултатите идват чрез
  callback-ове; `cancel` според R26. Съществува **блокиращо удобно извикване** за прости извикващи и тестове.
- **R38** Desktop (Layka, lib_rex): може да се вика от UI нишката на приложението без да я блокира;
  **няколко заявки в изпълнение върху един gateway handle** от няколко нишки; callback-овете се изпълняват по
  подразбиране в backend нишка, а приложението може да избере вместо това **pump режим** (callback-ове само
  вътре в `kiko_gateway_pump()`, викан от нишката на приложението), така че еднонишков C++ TUI да не се нуждае
  от заключвания.
- **R39** Рутер (kiko-wrt): еднонишков, без нишки, създавани от библиотеката; backend-ът се интегрира в
  съществуващия event loop на демона (света на procd: uloop) чрез fd + timer hook-ове.
- **R40** Решение: ядрото е **sans-I/O** - автомати на състояния, захранвани с байтове и време ("feed / next
  output / next deadline"); никога не блокира, не спи, не създава нишки и не чете часовника (часовникът и rng
  се инжектират). Backend-ите владеят сокетите, TLS, нишките и таймерите. Затова **не е нужна обща
  библиотека за event loop**: desktop backend-ите ползват по един worker на заявка в изпълнение (ограничен
  брой), router backend-ът ползва цикъла на платформата.
- **R41** Backpressure: stream callback-ът може да върне "pause"; ядрото спира да чете от upstream, докато не
  бъде подновено (нужно за лимита от 256 KiB за запис на kiko-wrt, PLAN L11).

### 2.10 HTTP сървърна част (само kiko-wrt)

- **R42** Входящият HTTP/1.1 сървър (рутер, парсиране на заявки с лимити, chunked / SSE отговори, лимит на
  връзките, drain при спиране - PLAN L10 / L11) **не е част от преносимото ядро**; той е отделен
  незадължителен модул, компилиран само за kiko-wrt. Desktop потребителите не го линкват. Desktop режим
  "локален сървър": **не е във v1**.

## 3. Нефункционални изисквания

- **N1 Размер.** Рутер: libkiko <= 80 KB stripped в примерната програма, kiko-wrt общо < 300 KB, idle RSS
  < 8 MB (целите за размер от PLAN). Desktop: libkiko ядро + backend <= 1 MB добавени към изпълнимия файл,
  когато TLS идва от ОС (Windows); <= 2 MB, когато се линква TLS библиотека (Linux); добавен idle RSS <= 5 MB.
- **N2 Равна памет.** RSS при 1 GB stream остава равен (PLAN S10); паметта на заявка е ограничена от лимитите.
- **N3 Матрица на преносимост (v1):** Windows 10/11 x64 (MSVC-съвместим ABI за Layka; компилирано със zig
  cc), Linux x64 glibc, OpenWrt 24.10 / 25.x musl на x86_64, MIPS (mipsel 24kc) и ARM (aarch64,
  arm_cortex-a7). Не във v1: macOS, Windows ARM64, Linux aarch64 desktop (още няма потребител; кодът не
  бива да ги препятства).
- **N4 Език.** C11, без разширения на компилатора в ядрото; публичните header-и се компилират като C и като
  C++17 (`extern "C"`, без `_Generic`, без VLA, без `_Atomic` в header-и).
- **N5 Без глобално състояние.** Всичко виси на handle-и; произволен брой gateway-и в един процес; напълно
  реентрантно; безопасността за нишки по R38 е документирана за всяка функция. Без `atexit`, без статични
  променливи данни.
- **N6 Безопасност на паметта.** Един растящ тип буфер с твърд лимит е единственото място, което прави
  realloc (PLAN `buf.h`); цялата аритметика на размери е с проверка за препълване; всяко заделяне е
  допустимо да пропадне и се проверява; коментар за собственост (ownership) на всяка структура; по избор
  allocator, подаван от извикващия.
- **N7 Лимити (по подразбиране, всички конфигурируеми):** тяло на заявка 32 MiB desktop (`kikolib/server.py
  DEFAULT_MAX_BODY`) / 1 MiB рутер (SPEC 6); дълбочина на JSON 64; буферирано upstream тяло без stream 8 MiB;
  SSE ред 1 MiB (kikolib ползва 16 MiB `MAX_LINE`; 1 MiB е стойността от SPEC и е достатъчно); заявки в
  изпълнение на gateway 8. Надвишаването на лимит е грешка, никога съкращаване.
- **N8 Сигурност.** TLS проверката е **включена по подразбиране**, име на хост се проверява, няма превключвател
  "skip verify" (чист http само за доставчик с изричен флаг `insecure_http`, SPEC 8); OS trust store на
  Windows и Linux desktop, файл с CA bundle на OpenWrt; поддръжка на HTTPS proxy на desktop (`HTTPS_PROXY` /
  `NO_PROXY`, системен proxy на Windows както в `winhttp.py`); тайните никога в грешки, логове, URL;
  upstream текстът на грешките се почиства (R28). SSRF: базовите URL на доставчиците идват само от
  конфигурацията на приложението, телата на клиентите не могат да ги задават (R5), а libkiko никога не
  изтегля URL, намерени в съдържанието (R14) - не е нужен друг SSRF контрол.
- **N9 Лиценз.** libkiko е MIT (автор Peter Kokosharov); всеки линкван или вендорван компонент трябва да е
  пермисивен (MIT, BSD, ISC, Apache-2.0, zlib, public domain). GPL / AGPL / LGPL код никога не се линква и
  не се вендорва. Библиотеки, предоставени от ОС и достъпвани през системното ѝ API (WinHTTP, SChannel,
  Credential Manager), са допустими.
- **N10 Четимост.** Един модул = един `.c` + един `.h`; имена `kiko_<module>_<name>`, огледални на kikolib /
  kiko_lite; таблица Python <-> C имена в `libkiko/README.md` (правилата от PLAN).
- **N11 Детерминизъм.** Същите входни байтове + същият инжектиран часовник / rng = същите изходни байтове
  (нужно за golden тестове).

## 4. Скица на формата на API (само формата)

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

Интерфейсът на backend-а (`kiko/backend.h`, вътрешен за платформения код) е малка таблица: `http_start`
(метод, URL, header-и, тяло), `http_cancel`, `timer_set`, `now`, `rand`, плюс входни точки "пристигнаха
байтове / пристигна head / приключи(грешка)" към ядрото. По една реализация: `backend_win.c`,
`backend_linux.c`, `backend_wrt.c` и `backend_mock.c` (тестове).

## 5. Изисквания към тестовете (дефиниция на "готово")

Правилото на собственика: **много тестове, за да не поправяме после.** Един модул се счита за готов само
когато всички тези минават в CI на всяка платформа от N3 (OpenWrt чрез SDK кръстосана компилация +
qemu-user или Docker rootfs).

- **T1 Golden двойки, споделени с Python.** Преводите на заявки/отговори се тестват срещу езиково-неутрални
  fixture файлове (`fixtures/<case>/{in.json, out.json, stream.sse, expected.jsonl}`), които **и** Python
  kiko тестовете, **и** C тестовете четат. Днес Python случаите са inline в `tests/kiko/test_kiko_lite_*.py`;
  първата стъпка на порта е да се изнесат в тези файлове (Python тестовете също ще ги четат). Покритие: всеки
  случай в `test_kiko_lite_openai_compat.py`, `test_kiko_lite_gemini.py`, `test_kiko_lite_chat_front.py`,
  `test_kiko_lite_toolshim.py`, `test_kikolib_sse.py`, `test_kikolib_retry.py`, `test_kikolib_tokens.py`,
  плюс записаните реални двойки от Phase 0 на PLAN (санирани).
- **T2 Stream-ове, разделени по байтове.** Всеки записан stream, подаден разделен на всяка граница на байт,
  дава идентичен изход.
- **T3 Fuzz цели** (libFuzzer чрез clang / zig, по 30 мин. за всяка промяна на парсер, дълги нощни
  изпълнения): `fuzz_json`, `fuzz_sse`, `fuzz_http_response`, `fuzz_openai_translate`,
  `fuzz_gemini_translate`, `fuzz_gemini_stream`, `fuzz_toolshim`, `fuzz_retry_after`; kiko-wrt добавя
  `fuzz_http_request`.
- **T4 Sanitiser-и.** Всички тестове под ASan + UBSan на Linux; valgrind или еквивалент за проверка на
  течове веднъж на release; TSan за едновременните заявки и cancel на desktop backend-а.
- **T5 Лимити.** Всеки лимит от N7 се тества точно на лимита и на лимита + 1.
- **T6 Хигиена на тайните.** Тест инжектира известни стойности на ключове и претърсва (grep) всеки лог ред,
  съобщение за грешка, резултатен запис и тяло на грешка, произведени от целия набор: нула попадения.
- **T7 Mock upstream.** Един скриптиран mock (PLAN L2): ok / 400 / 401 / 429 + Retry-After / 500 / невалиден
  JSON / огромно тяло / бавен / заседнал / прекъснат stream / TLS с тестов CA, грешен хост, self-signed. Всеки
  backend изпълнява един и същ списък със сценарии.
- **T8 Cancel и timeout-и.** Cancel преди head, по време на чакане на head, по средата на stream, по време на
  чакане за повторен опит: един терминален callback, никакъв callback след него, без теч; логът на mock-а
  показва, че upstream връзката е затворена.
- **T9 Детерминизъм.** Фиксиран часовник + rng дават идентичен изход на всички платформи (същите fixture
  файлове).
- **T10 Прагове за размер / RSS.** Числата от N1 и N2 се измерват в CI, провал при надвишаване на бюджета.
- **T11 Тест с C++ потребител.** Малка C++17 програма включва публичните header-и и пуска един stream срещу
  mock-а на Windows и Linux.
- **T12 Независим преглед.** Всеки завършен модул се преглежда от модел, който не го е писал; release само
  след одит от Opus / Fable (постоянното правило на собственика).

## 6. Контролен списък за търсене в стъпка 2 (компоненти, които да НАМЕРИМ)

Твърдо изискване във всеки ред: пермисивен лиценз (N9), C (или C с C API), поддържан (release през последните
2 години, реални потребители), компилира се със `zig cc`, без глобално променливо състояние или то може да
бъде изолирано.

| Възможност | Твърди изисквания | Желателно | Пишем ли сами, ако нищо не става? |
|---|---|---|---|
| JSON parse / build | лимити за дълбочина + размер, без срив от рекурсия, обработва неуспешно заделяне, UTF-8 валидация, всички цели от N3, <= 30 KB | streaming / SAX режим, вече ползван на OpenWrt | не (със сигурност съществува зрял) |
| HTTP/1.1 парсер на отговори (клиентска страна) | инкрементален, всяко разделяне на байтове, chunked transfer, лимити за header-и, fuzz-нат за upstream | парсира и заявки (kiko-wrt), без заделяния | да, малък, ако не пасва fuzz-нат |
| HTTPS клиент, Windows | OS TLS (SChannel) + системен proxy + OS trust store, async или прекъсваем | WinHTTP през чист Win32 API (каквото ползва `kikolib/winhttp.py`) | само glue (OS API) |
| HTTPS клиент, Linux desktop | прекъсваем, HTTPS proxy, OS trust store, много заявки | HTTP/1.1 keep-alive | да, върху TLS библиотека + собствен парсер |
| HTTPS клиент, OpenWrt | събира се в бюджета от 80 KB с каквото образът вече доставя | ползва TLS библиотеката на образа (без допълнителен пакет) | да (PLAN риск 1 вече го предвижда) |
| TLS, Linux desktop | пермисивен, проверка на хост, системен път към CA, <= ~1 MB статично | TLS 1.3, малък | не (никога не пишем TLS) |
| TLS, OpenWrt | вече в стандартните образи или един малък пакет, пермисивен | същата библиотека като Linux desktop | не |
| SSE парсер | правила на WHATWG, безопасен при разделяне по байтове | - | да (около 100 реда в kikolib) |
| Event loop | **не е нужен** в ядрото (R40); router backend-ът ползва цикъла на платформата | - | n/a |
| Вграден HTTP сървър (само kiko-wrt) | управляван от събития, лимити, chunked отговори, събира се в бюджета | работи върху цикъла на платформата | да, ако няма малък и пермисивен |
| Хранилище за ключове, Windows | Credential Manager през OS API | - | само glue |
| Хранилище за ключове, Linux desktop | Secret Service през D-Bus без LGPL линкване | пермисивен минимален D-Bus клиент | да (или викаме `secret-tool` както kikolib) |
| Хранилище за ключове, OpenWrt | проверки на 0600 файл | - | да (малко; библиотеката kiko-identity на C като основа, PLAN) |
| Base64 (стандартен + url-safe) | константни граници на буфера, валидация при декодиране | - | да (малко) |
| UTF-8 валидиране / броене | ограничено, бързо | - | да (малко) |
| Парсиране на HTTP-date (Retry-After) | IMF-fixdate + остарели форми, без locale | - | да (малко) |
| Glob съвпадение (таблица на моделите) | семантика на `fnmatch` както в kiko_lite | - | да (малко) |
| Цял съществуващ C/C++ OpenAI / Gemini клиент или proxy | пермисивен, C или C-API, streaming + tools, без тежки зависимости | включен превод за Gemini | да - самият libkiko е това; вземаме части само ако кандидат е ясно по-добър |

## 7. Открити въпроси към собственика

1. **Политика за лиценза:** може ли libkiko на Linux desktop да вика LGPL системна библиотека **само по
   време на изпълнение** (зареждана с `dlopen`, никога доставяна или линквана, напр. libsecret за хранилището
   на ключове)? да / не (По подразбиране, ако не: викаме програмата `secret-tool` както kikolib днес.)
2. **Обхват на front-овете:** v1 излага само OpenAI chat + Anthropic Messages; front-овете Responses (codex)
   и Gemini (Gemini CLI) засега остават само на Python в kiko. Съгласни ли сме? да / не
3. **Платформи:** нужен ли е macOS във v1 (Layka на Mac, напр. на колега)? да / не
4. **Приоритет:** кой потребител получава libkiko пръв? 1) Layka (първо desktop backend-ите), 2) kiko-wrt
   (първо router backend-ът, както в PLAN), 3) и двата паралелно.
5. **Собственост на fixtures:** може ли споделените golden fixtures (T1) да живеят в kiko репото и да са
   единственият източник за тестовете и на Python kikolib, и на C libkiko (Python тестовете се променят да
   ги четат)? да / не

Технически решения, взети тук (не въпроси): Anthropic Messages остава вътрешният pivot (R2); ядрото е
sans-I/O и не се нуждае от библиотека за event loop (R40); през API-то минава JSON текст (R3); входящият
HTTP сървър е само за kiko-wrt (R42); TLS никога не го пишем ние (раздел 6).

## Източници

All read locally on 2026-11-25.

- `embedded/docs/PLAN.md` (libkiko section, module decisions, size targets, steps)
- `embedded/docs/SPEC.md` (sections 6 Limits, 8 Security, 9 Non-goals)
- `gateway/kiko_runtime_lib/README.md`, `gateway.py`
- `gateway/kikolib/README.md`; function lists of `http.py`, `sse.py`,
  `retry.py`, `tokens.py`, `keystore.py`, `logs.py`, `winhttp.py`, `factory.py`; limits in `server.py`
- `gateway/kiko_lite/app.py` (docstring, routes, failover, cooldowns)
- `gateway/kiko_lite/providers/__init__.py`, `openai_compat.py`, `gemini.py`
- `gateway/kiko_lite/chat_front.py`, `gemini_front.py`, `responses_front.py`,
  `toolshim.py`, `resolver.py` (docstrings and function lists)
- `gateway/tests/kiko/` (file list only)
