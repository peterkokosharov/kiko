# kiko и CLI инструменти

kiko е локален gateway на `127.0.0.1:4000`. CLI инструментите говоря с него вместо директно с API на доставчик.
Клюовете на доставчиците остават в GNOME Keyring (`service=/peterk`, `key=<KEY>`, `username=<login>`), а management токенът на gateway-а — в `~/.kiko/api-token` (0600). Стойност на ключ не се пише в скрипт, конфиг или в този документ.

Gateway-ът работи в `--no-auth` home режим на loopback. Клиентите въпреки това изискват токен, защото SDK-тата им го искат, но той е заглушка (`dummy` / `sk-dummy-key`), която kiko игнорира.

## Използвани endpoint-и

| Endpoint | Формат | Ползва се от |
|---|---|---|
| `POST /v1/messages` | Anthropic Messages | Claude Code |
| `POST /v1/chat/completions` | OpenAI chat | aider, rex, OpenCode |
| `POST /v1/responses` | OpenAI Responses | Codex CLI |
| `POST /v1beta/models/{model}:generateContent` | Google Gemini | Gemini CLI |
| `POST /v1beta/models/{model}:streamGenerateContent` | Google Gemini (SSE) | Gemini CLI |
| `GET /v1beta/models` | `models[].name` | Gemini CLI |
| `GET /v1/models` | `data[].id` | OpenAI клиенти, aider |
| `GET /model/info` | `data[].model_name` | goose LiteLLM provider |
| `GET /health` | `{"status":"ok"}` | проверка преди старт |
| `GET /ui` | HTML статус страница | браузър |

**Ползвай aliases от базата на kiko, не имената на доставчика отгоре по веригата нито LiteLLM имената.** Например `nemotron-ultra`,
`llama-nemotron`, `gemini-lite`, `muse-glimmer`. upstream id като `nvidia/nemotron-ultra` връща 404.
Провереният каталог е в [`MODELS.md`](MODELS.md).

Първо проверка:

```bash
curl -fsS http://127.0.0.1:4000/health
systemctl --user status kiko-lite --no-pager
```

Gateway-ът е systemd **user** сервиз (`~/.config/systemd/user/kiko-lite.service`, enable-нат в
`default.target.wants`). GTK3 tray-то е XDG autostart запис (`~/.config/autostart/kiko-tray.desktop`).
И двете се връщат при login.

## Claude Code

Launcher: `claude-in-kiko` (и `claude-in-kiko.sh`) в `~/.local/bin`, сочи към
`~/ai_runtime/run-claude-kiko.sh`. Задава `ANTHROPIC_BASE_URL=http://127.0.0.1:4000`,
`ANTHROPIC_AUTH_TOKEN=sk-dummy-key` и следните aliases:

| Променлива | kiko alias |
|---|---|
| `ANTHROPIC_MODEL` | `nemotron-ultra` |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `gemini-lite` |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `nemotron-ultra` |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `llama-nemotron` |
| `ANTHROPIC_DEFAULT_FABLE_MODEL` | `muse-glimmer` |

`CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` остава включено: Claude Code не познава нашите
aliases и не бива да гаси граница за compaction.

```bash
claude-in-kiko
claude-in-kiko -p 'Reply with exactly: OK'
claude-in-kiko --resume <session-id>
```

Обикновеното `claude` отива директно при Anthropic, не през kiko.

## aider

Launcher: `aider-in-kiko` (и `aider-in-kiko.sh`) в `~/.local/bin`. Задава
`OPENAI_API_BASE=http://127.0.0.1:4000/v1`, `OPENAI_API_KEY=dummy`, основен модел
`openai/nemotron-ultra`, слаб модел `openai/gemini-lite` и изключва предупрежденията за непознати aliases.

```bash
cd /path/to/project
aider-in-kiko
aider-in-kiko --model openai/gemini-lite
AIDER_MODEL=openai/gemini-lite aider-in-kiko --yes --no-auto-commits -m "..."
```

Пускай го от папка на проект. `--no-auto-commits` е полезно за първи тест.

## goose

Ползва вградения LiteLLM provider. Неговата проверка за модели вика `GET /model/info`, който kiko
обслужва от същия списък aliases като `/v1/models`.

```bash
goose configure
# Provider: LiteLLM
# LITELLM_API_KEY: dummy
# LITELLM_HOST:  http://127.0.0.1:4000
# Разширени настройки: No
# Модел: nemotron-ultra
```

Ако проверката се провали, goose кешира грешката за 60 секунди — повтори след това. Не слагай реален ключ
в `LITELLM_API_KEY` — kiko го игнорира.

## rex

`rex` ползва същия OpenAI-съвместим route с `RUNTIME_LITELLM__BASE_URL=http://127.0.0.1:4000` и dummy ключ.
Моделът по подразбиране е `nemotron-ultra`. Launcher-ите в `~/.local/bin` са:

| Команда | Предназначение |
|---|---|
| `rex` | интерактивен CLI |
| `rex-local` | CLI без permission prompts, само за локални демове |
| `rex-acp` | ACP адаптер за IntelliJ (`~/.jetbrains/acp.json`) |

Инструментите вървят в опционалния sandbox `runtime/guard:latest` през `sg docker`; ползвай относителни
пътища като `src/Main.java` (абсолютни се отхвърлят). Docker Guard е sandbox за инструменти, не продуктова база.

## OpenCode

OpenCode не е превключен по подразбиране. В `~/.config/opencode/opencode.json` е добавен provider `kiko`
като OpenAI-съвместим с `baseURL: http://127.0.0.1:4000/v1`; корневият `model` не е пипан.

```bash
opencode run --model kiko/gemini-lite 'Reply with exactly: OK'
# или в TUI: /models -> kiko/nemotron-ultra
```

Проверено с `kiko/gemini-lite`: резултатът беше `OK`.

Launcher: `opencode-in-kiko` (и `opencode-in-kiko.sh`) в `~/.local/bin`. Проверява `/health` и подава
`--model kiko/nemotron-ultra` (override с `OPENCODE_MODEL`) към TUI или към `run`/`mini`.

```bash
opencode-in-kiko
opencode-in-kiko run 'Reply with exactly: OK'
OPENCODE_MODEL=kiko/gemini-lite opencode-in-kiko run 'Reply with exactly: OK'
```

Ако в бъдеща версия TUI не поеме флага за модел, избери веднъж `/models` → `kiko/nemotron-ultra`;
сесията запазва избора.

## Codex CLI

Инсталиран в `~/tools/codex-cli` (`codex-cli 0.171.0`) чрез npm с локален prefix; `~/tools/codex` и
`~/.local/bin/codex` сочат към бинарника. Обикновеното `codex` си пази собствения OpenAI provider.

Профилът е в `~/.codex/kiko.config.toml`: provider `kiko`, `wire_api = "responses"`,
`base_url = http://127.0.0.1:4000/v1`, `env_key = CODEX_KIKO_API_KEY` (стойността е заглушка, слага я
launcher-ът, никога реален ключ). Launcher-ът `codex-in-kiko` (и `codex-in-kiko.sh`) в `~/.local/bin`
проверява `/health`, слага dummy ключа и пуска `codex -p kiko`.

```bash
codex-in-kiko exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox \
  'Reply with exactly: OK'
codex-in-kiko                         # интерактивен TUI
CODEX_KIKO_MODEL=gemini-lite codex-in-kiko exec '...'
```

Моделът по подразбиране е `nemotron-ultra`; override с `CODEX_KIKO_MODEL`. Codex показва безобидно
предупреждение „Model metadata for ... not found“ за нашите aliases и ползва fallback метаданни.
Проверено с `gemini-lite`: `provider: kiko`, отговор `OK`.

## Gemini CLI

kiko излага Google Gemini протокола (`gemini_front.py`): `contents`, `systemInstruction`,
`functionDeclarations` и `generationConfig` се превеждат в същия Anthropic pipeline; отговорът се връща като
`candidates[].content.parts`, `finishReason` и `usageMetadata`. `topK`, `thinkingConfig` и други Gemini-специфични
полета се изпускат, не се препращат. Обслужват се и `v1beta`, и `v1alpha`.

Launcher: `gemini-in-kiko` (и `gemini-in-kiko.sh`) в `~/.local/bin`. Проверява `/health`, слага
`GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4000`, слага `GEMINI_API_KEY=dummy`, премахва `GOOGLE_API_KEY`
(за да не пита CLI и да не изтегля реалния ключ от keyring) и пуска `gemini -m gemini-lite`.

```bash
gemini-in-kiko -p 'Reply with exactly: OK'   # еднократно; за нова temp папка добави --skip-trust
gemini-in-kiko                               # интерактивен TUI
GEMINI_KIKO_MODEL=llama-nemotron gemini-in-kiko
```

**Не сменяй модел с `/model` във TUI-то.** Изборът показва родни Google модели като
`gemini-3.5-flash-lite`; те ги няма в базата на kiko и той връща 404. Моделът се фиксира при старт с `-m` или
`GEMINI_KIKO_MODEL`. Рестартирай сесията след промяна. Проверено с `gemini-lite`: еднократно `-p` върна `OK`.

Обикновеното `gemini` продължава да ходи директно в Google с реалния ключ от GNOME Keyring. Обвивката
`~/.local/bin/gemini` чете ключа само ако няма нито `GEMINI_API_KEY`, нито `GOOGLE_API_KEY`.

## rex и Layka в терминал

И двете работят от терминала на IntelliJ (и от всяка обвивка); `~/.local/bin` е в PATH от `~/.profile` / `~/.bashrc`.

| Команда | Какво е |
|---|---|
| `rex`, `rex-local`, `rex-acp` | launchers в `~/.local/bin`; `REX_ROOT` сочи стабилния сорс в `~/tools/rex` (v0.2.1) |
| `layka` | symlink в `~/.local/bin` към `~/Documents/Default Project/project-layka/build/layka` |

Старият `REX_ROOT=/tmp/opencode/rex-native-port` се счупи след почистване на `/tmp`
(„No module named runtime“); вече сочи `~/tools/rex`. rex ползва kiko на `http://127.0.0.1:4000` с dummy ключ,
модел `nemotron-ultra`; инструментите вървят в `runtime/guard:latest` през `sg docker`. Проверено в `~/archive`:
`rex` отговори `OK` и създаде файл с fileops. `rex-local`/`--local-tools` е само за демо.
Layka е Turbo Vision TUI, значи ѝ трябва реален терминал (стартира успешно в терминал).

## Все на собствените си модели

- **Gemini CLI** — обикновеното `gemini` ползва свой ключ и родни модели; за kiko е `gemini-in-kiko`.
- **Codex CLI** — обикновеното `codex` ползва собствените си OpenAI модели; за kiko е `codex-in-kiko`.
- **OpenCode** — пази конфигурирания по подразбиране модел; `kiko/...` е достъпен при избор.

## Самият kiko

```bash
kiko doctor      # само ИМЕНА на клюове, не стойности
kiko models      # опреснява проверения каталог, пази базата
kiko restart     # рестартира user сервиза
```

`kiko install` не иска sudo; sudo е само за `dpkg -i` на пакетите.
