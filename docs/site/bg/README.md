# kiko-lite

kiko-lite е малък „рутер на модели" (model router) за дома. Написан е на чист Python (само стандартна библиотека,
Python 3.11+), стартира за много под секунда и ползва около 36 MB памет.

Идеята: клиент като **Claude Code** или многоагентната система на собственика **roni** не иска модел на конкретен
доставчик. Той иска **клас** модел (OPUS, SONNET, HAIKU, FABLE, [а по-късно и други](MODEL-CLASSES.md#planned-classes)), а kiko-lite му дава жив модел
от този клас от произволен доставчик (Gemini, NVIDIA, Groq, Qwen, OpenAI-съвместими крайни точки).

- **Без зависимост от един доставчик.** Клиентът винаги говори Anthropic Messages API (`POST /v1/messages`);
  kiko-lite го превежда за реалния доставчик и превежда обратно отговора, който идва на потоци (streaming).
- **Безплатни нива за некритична работа.** Картата на класовете може да насочи SONNET към безплатен модел на NVIDIA,
  а HAIKU към евтин Gemini flash-lite, и да запази скъпия модел за ролята на мениджър.
- **Малък.** Няма външен пакет по време на работа: `http.client`, `ssl`, `http.server`, `sqlite3`, `json`.
- **Тайните не стоят във файлове.** API ключовете идват от променливи на средата или от хранилището за ключове на ОС
  (Windows Credential Manager, macOS Keychain, secret-tool, KWallet). Базата SQLite пази само ИМЕНАТА на ключовете.

Това е малкият събрат на „kiko-full", който е разклонение (fork) на LiteLLM с фактуриране отгоре. Вж. [NUMBERS](NUMBERS.md).

## Бърз старт

Пуска се от корена на експортираното дърво (папката, която съдържа `kiko_lite/` и `kikolib/`):

```
python -m kiko_lite doctor                       # check the installation, never prints a secret
python -m kiko_lite add-model gem-flash --provider gemini/<model-id> --api-key-env GEMINI_API_KEY --class OPUS
python -m kiko_lite list-classes                 # OPUS, SONNET, HAIKU, FABLE ...
python -m kiko_lite list-models
python -m kiko_lite serve --no-auth              # home mode (or env KIKO_AUTH=none), 127.0.0.1:4000 (trusted network only)
```

После насочете Claude Code към него:

```
set ANTHROPIC_BASE_URL=http://127.0.0.1:4000
set ANTHROPIC_DEFAULT_OPUS_MODEL=<alias or provider/model-id>
```

`<model-id>` е заместител: ползвайте идентификатор на модел, който доставчикът ви наистина предлага. Базата е
`~/.kiko/kiko.db`, освен ако `--db` или променливата `KIKO_DB_PATH` не кажат друго. Без `--no-auth` (или env `KIKO_AUTH=none`) сървърът иска
`MASTER_KEY` (от средата или от хранилището за ключове), а клиентите го пращат като `x-api-key`.

## Къде е какво

| Страница | Съдържание |
|---|---|
| [ARCHITECTURE](ARCHITECTURE.md) | компоненти, диаграма на класовете, една заявка със streaming, внедряване |
| [MODEL-CLASSES](MODEL-CLASSES.md) | идеята за класовете, ролите, картата на собственика, редът на резолвъра |
| [ROADMAP](ROADMAP.md) | watchdog и превключване при отказ (failover), обновяеми доставчици, цел: рутер |
| [NUMBERS](NUMBERS.md) | kiko-lite срещу kiko-full на базата на LiteLLM, какво е изрязано |
| [CODE-TOUR](CODE-TOUR.md) | водена обиколка с реални откъси от кода |

Папки с изходен код в експортираното дърво:

| Папка | Съдържание |
|---|---|
| [kikolib/](../../../kikolib/) | малкият набор инструменти на стандартна библиотека (http, server, retry, keystore, ...) |
| [kiko_lite/](../../../kiko_lite/) | приложението: gateway, resolver, доставчици, SQLite, CLI |
| [tests/kiko/](../../../tests/kiko/) | тестове (папката съдържа и по-стари тестове на kiko-full) |
| [tools/](../../../tools/) | помощни скриптове |
| [docs/](../../../docs/) | по-стара документация за разработчици (най-вече за kiko-full) |

Състояние: kiko-lite е млад. Всичко, означено като „планирано", още не е построено.
