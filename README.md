# KIKO

A minimal AI model gateway for personal and small-team use.
Fork of [litellm v1.104.2](https://github.com/BerriAI/litellm), stripped to the essentials.

## What it does

- Single OpenAI-compatible entry point for Google Gemini, NVIDIA NIM, and other AI providers
- Local REST API on `http://localhost:4000` — drop-in replacement for OpenAI clients
- Every call logged to a local SQLite database (tokens + cost) — no cloud, no PostgreSQL, no Redis
- Simple master-key authentication
- Full model CRUD via CLI — add, remove, enable, disable models without editing YAML

## Requirements

- Python 3.12+
- Internet access to reach AI provider APIs
- API keys for the providers you want to use

## Installation

```bash
pip install kiko
```

## Quick start

```bash
# Set environment variables (add to a start.bat or shell script)
set MASTER_KEY=sk-choose-any-secret
set GEMINI_API_KEY=your_google_ai_studio_key

# Seed models from config (run once)
kiko --config kiko-config.yaml --init-db

# Start the proxy
kiko --config kiko-config.yaml --port 4000

# Test it
curl -H "Authorization: Bearer sk-choose-any-secret" ^
     -H "Content-Type: application/json" ^
     -d "{\"model\":\"gemini-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}" ^
     http://localhost:4000/v1/chat/completions
```

## CLI commands

```
kiko --config PATH --port 4000        Start the proxy server
kiko --config PATH --init-db          Seed models from YAML config
kiko --list-models                    List all enabled models
kiko --add-model ALIAS \
    --provider PROVIDER/ID \
    --api-key-env VAR \
    [--api-base URL]                          Add or update a model
kiko --delete-model ALIAS             Remove a model permanently
kiko --enable-model ALIAS             Re-enable a disabled model
kiko --disable-model ALIAS            Disable a model (keeps in DB)
kiko --discover                       Ping all models, update status
kiko --costs [--days N]               Show billing summary (default 30 days)
```

See `commands.txt` for the full reference with examples.

## API endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | no | Liveness check |
| GET | `/v1/models` | yes | List models |
| POST | `/v1/chat/completions` | yes | Chat completion |
| POST | `/v1/completions` | yes | Text completion |
| POST | `/v1/embeddings` | yes | Embeddings |

## Configuration

See `kiko-sample.yaml` for a fully commented example configuration.

Keys are **never stored in config files** — always use environment variables.

## Billing database

Stored at `~/.kircho/kircho.db` (SQLite). Override with `KIKO_DB_PATH` env var.

```bash
kiko --costs --days 7    # last 7 days by model
```

## Connecting your AI clients

### Claude Code
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_AUTH_TOKEN=your-master-key
claude
```

### Gemini CLI
```bash
export GEMINI_API_KEY=your-master-key
export GEMINI_BASE_URL=http://localhost:4000/v1beta
gemini
```

### OpenAI Codex CLI
In `~/.codex/config.toml`:
```toml
model = "codex-mini"   # or any alias from your kiko-sample.yaml
wire_api = "responses"
[provider]
name = "openai"
base_url = "http://localhost:4000"
api_key = "your-master-key"
```

Model aliases are configured in your `kiko-sample.yaml` under `model_list`.
The proxy accepts any alias that matches a configured model.

## Spending cap

Add `monthly_cap_usd: 10.0` to your config file to limit monthly spend.
The proxy returns 429 when the cap is reached.

## License

MIT — Copyright © 2026 Peter Kokosharov  
Based on litellm © 2023-2024 BerriAI
