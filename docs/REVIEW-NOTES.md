# Code Review Notes — 2026-11-24

Автор: Claude Code (nemotron-ultra)
Дата: 2026-11-24
Контекст: Пълен преглед на `kiko` репото след `git clone` и `kiko install`

---

## Общ вердикт

**Солиден, добро структуриран проект** с чист `stdlib-only` подход в `kikolib`, богат CLI в `kiko_lite`, мултиплатформен tray и коректна systemd интеграция. Готов за продакшън при поправка на критичния keystore bug.

---

## Критични проблеми (трябва да се оправят веднага)

### 1. Keystore attribute mismatch — **BLOCKER**

| Файл | Очаква | Реално записва |
|------|--------|----------------|
| `kikolib/keystore.py` (SecretToolStore.get) | `secret-tool lookup kiko-identity <NAME>` | — |
| `tray/tools/setup-keys.sh` (store) | — | `secret-tool store --label="/peterk/$key" service /peterk key "$key" username "$KEY_USER"` |
| `tray/tools/run-kiko.sh` (get) | — | `secret-tool lookup service /peterk key "$1" username "$KEY_USER"` |
| `tray/tools/kiko` (key_present) | — | `secret-tool lookup service /peterk key "$1" username "$KEY_USER"` |

**Резултат**: Ключовете, въведени чрез `setup-keys.sh`, **не се намират** от `kikolib` при `serve`/`doctor`.

**Предложено решение**: Обединете на `kiko-identity` (по-прост, стандарт за libsecret):
- `setup-keys.sh`: `secret-tool store --label="kiko $key" kiko-identity "$key"`
- `run-kiko.sh`: `secret-tool lookup kiko-identity "$1"`
- `kiko` (key_present): `secret-tool lookup kiko-identity "$1"`
- `keystore.py` вече прави това — останете при него.

---

### 2. Test environment — 2 FAIL-а

| Suite | Грешка | Причина |
|-------|--------|---------|
| `windows tools` | `venv` creation fails | Липсва `python3.12-venv` пакет |
| `windows tray core` | `test_pidfile_naming_other_pid_refused` | Flaky на Linux (PID file logic) |

**Fix**: `apt install python3.12-venv` в CI / dev container. Вторият тест — преглед на `tray/wincore/tests/test_core.py:132`.

---

## Важни проблеми (трябва да се оправят скоро)

### 3. Naming confusion: `kiko` vs `kiko-lite`

| Име | Къде се среща | Какво е |
|-----|---------------|---------|
| `kiko` | `~/.local/bin/kiko` (wrapper), `tray/tools/kiko` (install script) | Тъйнарът |
| `kiko-lite` | `python -m kiko_lite`, `kiko-lite.service`, `kiko-lite-<sha>.zip` | Реалният модул/служба |
| `kiko` | `kiko.service` (старо), `kiko-proxy.service` (старо) | Legacy |

**Препоръка**: Инсталираният binary = `kiko-lite`. Wrapper скриптът = `kiko` (ако е нужен за compat). Актуализирайте `docs/`, `README.md`, `setup-keys.sh` подсказките.

---

### 4. Schema migrations — риск при upgrade

- `db.py`: `SCHEMA_VERSION = 8` (hardcoded)
- `cli.py`: `SCHEMA_VERSION = 8` (duplicated)
- Няма автоматична миграция при `serve`/`import`/`add-model` — само ручно через `fix-key` или нова инсталация.

**Fix**: Един източник на истината (`kikolib.config` или `db.py` експорт) + авто-миграция при отваряне на DB.

---

### 5. `setup-keys.sh` — непълен набор ключове

`setup-models.sh` ползва: `NVIDIA_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `GITHUB_TOKEN`

`setup-keys.sh` пита само: `GEMINI_API_KEY`, `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `GITHUB_TOKEN` — **OK, тук е пълният набор**. (Проблемът е в keystore атрибутите, не в списъка.)

---

### 6. Health check vs auth mode

- `run-kiko.sh` стартира с `--no-auth`
- `doctor` показва: `Master key: NOT FOUND (serve will refuse to start without --no-auth)`
- Потребителят не разбира: "Имам ли ключ или не?"

**Fix**: В `doctor` добавете ред: `Auth mode: --no-auth (home LAN)` ако `KIKO_AUTH=none` или `[proxy] no_auth`.

---

## Средни по важност

### 7. Docs синхронизация

| Файл | Проблем |
|------|---------|
| `docs/gateway/KIKO-LITE-README.md` | Казва `./kiko-lite add-model ...` но инсталираното е `kiko-lite` (wrapper) или `python -m kiko_lite` |
| `.bg.md` файлове | Някои са преведени, други — не; проверете актуалност |
| `README.md` (root) | Сочи към `bash tray/tools/kiko install` — OK, но не посочва че инсталира `kiko-lite` |

---

### 8. Provider key names в DB

`setup-models.sh` създава модели с `api_key_env = "NVIDIA_API_KEY"` etc., но `keystore.py` очаква да намери ключа под **същия име** в OS store. Това е OK, стига keystore атрибутите да са поправени (п.1).

---

### 9. Portable build — липсва `kiko-lite` wrapper в zip

`tools/kiko_lite_bundle.py` пакува `kiko-lite` (sh) и `kiko-lite.cmd` (Windows) — OK. Но не пакува `install-service.sh` за systemd? **Проверете** — в `RENAMED` има `install-service.sh` и `kiko-lite.service`. Добре.

---

## Ниско приоритет / Nice to have

### 10. AI header check — `# LLM:` навсякъде

`tools/check_ai_header.py` валидира — добра практика. Някои стари файлове са `unknown (before 2026-11-24)` — приемливо.

### 11. `pyproject.toml` в `gateway/` — dependencies за `kiko` (full), не за `kiko-lite`

`kiko-lite` е stdlib-only. `pyproject.toml` е за `kiko` (litellm fork). Документирайте ясно: `gateway/` = два продукта в едно repo.

### 12. `embedded/` — plan only

`embedded/README.md` казва "plan only". Ако не е активно — преместете в `docs/ARCHIVE/` или махнете.

---

## План за утре (предложен ред)

1. **Fix keystore attribute mismatch** (p.1) — 30-45 мин
2. **Fix naming** (`kiko` vs `kiko-lite`) (p.3) — 20 мин
3. **Schema migration helper** (p.4) — 30 мин
4. **Doctor auth mode message** (p.6) — 10 мин
5. **Docs sync** (p.7) — 20 мин
6. **Test env: `apt install python3.12-venv`** — 2 мин
7. **Run full test suite** за валидация

---

## Файлове за промяна (за référendum)

| Файл | Защо |
|------|------|
| `tray/tools/setup-keys.sh` | Keystore attributes |
| `tray/tools/run-kiko.sh` | Keystore attributes |
| `tray/tools/kiko` (key_present) | Keystore attributes |
| `gateway/kiko_lite/cli.py` | Doctor message, schema version dedup |
| `gateway/kiko_lite/db.py` | Schema version export |
| `docs/gateway/KIKO-LITE-README.md` | Naming, commands |
| `README.md` (root) | Clarify what gets installed |
| `.github/workflows/` (ако има) | Add `python3.12-venv` |

---

*Генерирано автоматично от Claude Code при code review. За да се актуализира — пуснете пак review след промените.*