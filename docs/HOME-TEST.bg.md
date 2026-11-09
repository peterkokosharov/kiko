# KIKO — Domashen test (Step 15)

**Avtor:** Peter Kokosharov  
**Versiya:** 0.3.0

Tozi spisak pokriva zhiv end-to-end test na trite klientski integratsii:
Claude Code, Gemini CLI i Codex. Izpylni vseki razdel po red; proverkata na
bilingat potvyrzhava, che vseko izvikvane e zapisano pravilno.

---

## Predposylki

Predi da zapochnes:

1. kiko raboti:
   ```
   kiko --config kiko-config.yaml --port 4000
   ```
   Ochakvan izkhod: `KIKO running on http://127.0.0.1:4000`

2. `MASTER_KEY` e zadaden v environment ili Windows Credential Manager
   (`KIRCHO/MASTER_KEY`). Tova e edinstveniia klyuch, koito vsichki klienti
   izpolzvat.

3. Pone edin model e zasyan v baza dannite:
   ```
   kiko --list-models
   ```

4. Billing DB sashtestvuva (syvzdava se avtomatichno pri pyrvia povik):
   `~/.kircho/kircho.db`

V tozi dokument `$MASTER_KEY` oznachava stoynostta, koyato si izbral za MASTER_KEY.

---

## 1. Test na Claude Code

Claude Code izpolzva Anthropic Messages API (`/v1/messages`).

### 1.1 Zadai environment promenlivi

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=$MASTER_KEY
```

Na Windows (Git Bash ili cmd):
```
set ANTHROPIC_BASE_URL=http://127.0.0.1:4000
set ANTHROPIC_AUTH_TOKEN=<stoynostta na tvoqta MASTER_KEY>
```

### 1.2 Prosta komanda (model 1)

Startiray Claude Code i izprati prosta komanda:
```
> Kaji "hello from kiko" i nicho drugo.
```

Ochakvan rezulat: otgovorat sydarezha "hello from kiko".

### 1.3 Promenya na model (model 2)

V nastroikite na Claude Code (ili chrez `/model`), premini kum vtor alias
ot konfiguratsiyata ti (napr. `gemini-pro`) i povtori zayavkata.

Ochakvan rezulat: otgovor ot vtoriya model.

### 1.4 Multi-turn razgovor

Izprati pone dve sledvashti saobshteniya v syshtata sesiya, za da proveri che
kontekstyat se predava pravilno mezhdu pauzite.

### 1.5 Proverka na billing

```bash
kiko --costs --days 1
```

Ochakvan rezulat: pone 2 reda — po edin za vseki izpolzvan model. `tokens_in`,
`tokens_out` i `cost_usd` tryabva da sa nenulevi.

### 1.6 Malka granitsa → otkaz

Redaktiray konfiguratsiya, zadai granitsa pod tekushtia razkhod:
```yaml
general_settings:
  monthly_cap_usd: 0.001
```

Restartiray kiko:
```
kiko --config kiko-config.yaml --port 4000
```

Izprati nova zayavka ot Claude Code. Ochakvan rezulat: HTTP 429,
`"type": "rate_limit_error"`.

Mahni ili vdigni granitsa sled priklyuchvane, posle restartiray.

---

## 2. Test na Gemini CLI

Gemini CLI izpolzva Gemini `generateContent` API
(`/v1beta/models/{model}:generateContent`).

### 2.1 Zadai environment promenlivi

```bash
export GEMINI_API_KEY=$MASTER_KEY
```

Gemini CLI tryabva da znae i base URL:

```bash
export GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4000
```

Proveri dokumentatsiyata na tvoya Gemini CLI za tochnoto ime na promenilivata.

### 2.2 Izprati zayavka

```bash
gemini "Kazhi 'hello from kiko' i nicho drugo."
```

Ochakvan rezulat: otgovorat sydarezha "hello from kiko".

### 2.3 Proverka na billing

```bash
kiko --costs --days 1
```

Ochakvan rezulat: obsht broi zayavki e narasnal spryamo Claude Code testa.

Za da vidish razpredelenieto po route:
```bash
sqlite3 ~/.kircho/kircho.db "SELECT route, model, SUM(cost_usd) FROM billing WHERE strftime('%Y-%m',ts)=strftime('%Y-%m','now') GROUP BY route, model"
```

---

## 3. Test na Codex

Codex izpolzva OpenAI Responses API (`/v1/responses`).

### 3.1 Konfiguriray `~/.codex/config.toml`

```toml
[model_providers.kiko]
name = "kiko"
base_url = "http://127.0.0.1:4000"
env_key = "KIKO_API_KEY"

model_provider = "kiko"
model = "claude-opus-4-5"
```

### 3.2 Zadai API key

```bash
export KIKO_API_KEY=$MASTER_KEY
```

Codex chete klyucha ot `KIKO_API_KEY` ili `OPENAI_API_KEY` (zavisi ot
versiyata — zadai koyto se ochakva s stoynostta na MASTER_KEY).

### 3.3 Prosta zadacha

```bash
codex "Otpechatay sumata na 2 + 2."
```

Ochakvan rezulat: Codex zavyrshva zadachata bez greshki.

### 3.4 Proverka na billing

```bash
kiko --costs --days 1
```

Ochakvan rezulat: broyt se e uvelichil. Za da proveri `responses` route:
```bash
sqlite3 ~/.kircho/kircho.db "SELECT route, model, SUM(cost_usd) FROM billing WHERE strftime('%Y-%m',ts)=strftime('%Y-%m','now') GROUP BY route, model"
```

---

## 4. Test na mesechenata granitsa (cross-client)

Tova e samostoyatelen test na kapasitetniya mehanizym.

1. Zadai `monthly_cap_usd: 0.001` v konfiguratsiyata (stoynost pod tekushtoto
   mesechno razkhod).

2. Restartiray kiko.

3. Proba s vseki klient:

   **Claude Code** — izprati zayavka, ochakvan otgovor:
   ```json
   {"type": "error", "error": {"type": "rate_limit_error", "message": "Monthly spending cap..."}}
   ```
   HTTP 429.

   **Gemini CLI** — izprati zayavka, ochakvan otgovor:
   ```json
   {"error": {"code": 429, "message": "...", "status": "RESOURCE_EXHAUSTED"}}
   ```
   HTTP 429.

   **Codex** — ochakvan OpenAI error format s `"code": "monthly_cap_exceeded"`.
   HTTP 429.

4. Mahni ili vdigni granitsa, restartiray, proveri che zayavkite uspyavat.

---

## 5. Sled vsichki testove

```bash
kiko --costs --days 1
```

Pregled na obshhiya razkhod. Bazata dannite e `~/.kircho/kircho.db` — mozhesh
da ya otvorysh i direktno s vsyakoy SQLite brauzer.

Za da resetiraysh test dannite: spri kiko, iztriy `~/.kircho/kircho.db`,
izpylni `--init-db` za da vzstanovish models tablitsata, posle restartiray.
