# KIKO — Home Test Checklist (Step 15)

**Author:** Peter Kokosharov  
**Version tested:** 0.3.0

This checklist covers a live end-to-end test of all three client integrations:
Claude Code, Gemini CLI, and Codex. Run every section in order; billing checks
confirm that each call was recorded correctly.

---

## Prerequisites

Before starting, confirm:

1. kiko is running:
   ```
   kiko --config kiko-config.yaml --port 4000
   ```
   Expected output: `KIKO running on http://127.0.0.1:4000`

2. `MASTER_KEY` is set in the environment or Windows Credential Manager
   (`KIKO/KIKO_IDENTITY/MASTER_KEY`). This is the single key used by all clients.

3. At least one model is seeded in the DB:
   ```
   kiko --list-models
   ```

4. Billing DB exists (created automatically on first call):
   `~/.kircho/kircho.db`

Throughout this document `$MASTER_KEY` means the value you chose for MASTER_KEY.

---

## 1. Claude Code test

Claude Code uses the Anthropic Messages API (`/v1/messages`).

### 1.1 Set environment variables

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export ANTHROPIC_AUTH_TOKEN=$MASTER_KEY
```

On Windows (Git Bash or cmd):
```
set ANTHROPIC_BASE_URL=http://127.0.0.1:4000
set ANTHROPIC_AUTH_TOKEN=<your MASTER_KEY value>
```

### 1.2 Basic prompt (model 1)

Launch Claude Code and send a simple prompt:
```
> Say "hello from kiko" and nothing else.
```

Expected: response contains "hello from kiko".

### 1.3 Model switch (model 2)

In Claude Code settings (or via `/model`), switch to a second alias defined in
your config (e.g. `gemini-pro`) and repeat the prompt.

Expected: response received from the second model.

### 1.4 Multi-turn conversation

Send at least two follow-up messages in the same session to confirm context is
passed correctly across turns.

### 1.5 Billing check

```bash
kiko --costs --days 1
```

Expected: at least 2 rows, one per model used. `tokens_in`, `tokens_out`, and
`cost_usd` should all be non-zero.

### 1.6 Tiny cap → refusal

Edit your config, set a cap that is already exceeded by current spend:
```yaml
general_settings:
  monthly_cap_usd: 0.001
```

Restart kiko:
```
kiko --config kiko-config.yaml --port 4000
```

Send another Claude Code prompt. Expected: error response with HTTP 429,
`"type": "rate_limit_error"`.

Remove or raise the cap when done, then restart.

---

## 2. Gemini CLI test

Gemini CLI uses the Gemini `generateContent` API
(`/v1beta/models/{model}:generateContent`).

### 2.1 Set environment variables

```bash
export GEMINI_API_KEY=$MASTER_KEY
```

Gemini CLI also needs to know the base URL:

```bash
export GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4000
```

### 2.2 Send a prompt

```bash
gemini "Say 'hello from kiko' and nothing else."
```

Expected: response contains "hello from kiko". Requests will appear in kiko's
console output (if log level is info).

### 2.3 Billing check

```bash
kiko --costs --days 1
```

Expected: total call count increased since the Claude Code test.

To see per-route breakdown:
```bash
sqlite3 ~/.kircho/kircho.db "SELECT route, model, SUM(cost_usd) FROM billing WHERE strftime('%Y-%m',ts)=strftime('%Y-%m','now') GROUP BY route, model"
```

---

## 3. Codex test

Codex uses the OpenAI Responses API (`/v1/responses`).

### 3.1 Configure `~/.codex/config.toml`

```toml
[model_providers.kiko]
name = "kiko"
base_url = "http://127.0.0.1:4000"
env_key = "KIKO_API_KEY"

model_provider = "kiko"
model = "claude-opus-4-5"
```

### 3.2 Set the API key

```bash
export KIKO_API_KEY=$MASTER_KEY
```

Codex reads the key from `KIKO_API_KEY` or `OPENAI_API_KEY` (check your Codex
version; set whichever it expects, with the MASTER_KEY value).

### 3.3 Run a simple task

```bash
codex "Print the sum of 2 + 2."
```

Expected: Codex completes the task; response arrives without errors.

### 3.4 Billing check

```bash
kiko --costs --days 1
```

Expected: total count increased. To confirm `responses` route rows:
```bash
sqlite3 ~/.kircho/kircho.db "SELECT route, model, SUM(cost_usd) FROM billing WHERE strftime('%Y-%m',ts)=strftime('%Y-%m','now') GROUP BY route, model"
```

---

## 4. Monthly cap test (cross-client)

This is a standalone stress test of the cap mechanism.

1. Set `monthly_cap_usd: 0.001` in your config (a value you know is below
   current month's spend).

2. Restart kiko.

3. Test each client type in turn:

   **Claude Code** — send a prompt, expect:
   ```json
   {"type": "error", "error": {"type": "rate_limit_error", "message": "Monthly spending cap of 0.00 USD reached"}}
   ```
   HTTP 429.

   **Gemini CLI** — send a prompt, expect:
   ```json
   {"error": {"code": 429, "message": "...", "status": "RESOURCE_EXHAUSTED"}}
   ```
   HTTP 429.

   **Codex** — send a prompt, expect OpenAI error shape with `"code": "monthly_cap_exceeded"`.
   HTTP 429.

4. Remove or raise the cap, restart, confirm requests succeed again.

---

## 5. After all tests

```bash
kiko --costs --days 1
```

Review total spend. The DB path is `~/.kircho/kircho.db`; you can also inspect
it directly with any SQLite browser.

To reset test data: stop kiko, delete `~/.kircho/kircho.db`, run `kiko --config <file> --init-db` to
recreate the models table, then restart.
