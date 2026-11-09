# kiko-lite

kiko-lite is a small "model router" for the home. It is written in plain Python (standard library only, Python 3.11+),
starts in well under a second and needs about 36 MB of memory.

The idea: a client such as **Claude Code**, or the owner's multi-agent system **roni**, does not ask for a vendor
model. It asks for a model **class** (OPUS, SONNET, HAIKU, FABLE, and [more later](MODEL-CLASSES.md#planned-classes)) and kiko-lite gives it an alive
model of that class from any vendor (Gemini, NVIDIA, Groq, Qwen, OpenAI-compatible endpoints).

- **No vendor lock-in.** The client always speaks the Anthropic Messages API (`POST /v1/messages`); kiko-lite
  translates it for the real provider and translates the streamed answer back.
- **Free tiers for non-critical work.** The class map can point SONNET at a free NVIDIA model and HAIKU at a cheap
  Gemini flash-lite, and keep the expensive model for the manager role.
- **Small.** No third-party package at runtime: `http.client`, `ssl`, `http.server`, `sqlite3`, `json`.
- **Secrets stay out of files.** API keys come from environment variables or the OS key store (Windows Credential
  Manager, macOS Keychain, secret-tool, KWallet). The SQLite database only holds key NAMES.

It is the small sibling of "kiko-full", which is a LiteLLM fork with billing on top. See [NUMBERS](NUMBERS.md).

## Quick start

Run from the root of the exported tree (the folder that contains `kiko_lite/` and `kikolib/`):

```
python -m kiko_lite doctor                       # check the installation, never prints a secret
python -m kiko_lite add-model gem-flash --provider gemini/<model-id> --api-key-env GEMINI_API_KEY --class OPUS
python -m kiko_lite list-classes                 # OPUS, SONNET, HAIKU, FABLE ...
python -m kiko_lite list-models
python -m kiko_lite serve --no-auth              # home mode (or env KIKO_AUTH=none), loopback only, port 4000 by default
python -m kiko_lite config --json                # the resolved paths, hosts, ports and settings
```

Then point Claude Code at it:

```
set ANTHROPIC_BASE_URL=http://127.0.0.1:4000
set ANTHROPIC_DEFAULT_OPUS_MODEL=<alias or provider/model-id>
```

`<model-id>` is a placeholder: use a model id that your provider really offers. The database is
`~/.kiko/kiko.db` unless `--db`, env `KIKO_DB_PATH` or `$KIKO_CONFIG_DIR` says otherwise. All settings live in
`kiko.toml` in the same folder (see [Configuration](#configuration)). Without `--no-auth` (or env `KIKO_AUTH=none`) the server needs a
`MASTER_KEY` (env or key store) and clients send it as `x-api-key`.

### Per-model thinking switch (OpenAI-compatible models)

Some reasoning models (for example NVIDIA Nemotron) think by default and can spend the whole `max_tokens` on
reasoning, leaving the text empty. `add-model` takes `--thinking default|off|on|off-prompt`, and `set-thinking ALIAS MODE`
changes it later (the web Models page has the same choice and shows it in a column):

- `default` (the default): send nothing. Omitting `--thinking` on an update keeps the stored value.
- `off` / `on`: the upstream body carries `"chat_template_kwargs": {"enable_thinking": false}` (or `true`).
- `off-prompt`: instead adds a `/no_think` line to the system message (for models that read that switch).

```
python -m kiko_lite set-thinking nemotron-super off
```

With `on` or `default` nothing else is done: the client still receives the thinking block, and if `max_tokens` is
used up by reasoning the answer text can be empty. Gemini models ignore the setting. `list-models` prints
`thinking=<mode>` for rows where it is set.

### Per-model toolshim (tool calls written as text)

Some free models (NVIDIA Nemotron, some Mistral and Llama builds) write the tool call as plain text instead of the
structured field, so the harness sees text and the agent stalls. With toolshim on, kiko parses such text itself (no
extra model call) and answers with a real `tool_use` block (`stop_reason` `tool_use`). Off by default.

- Switch: `add-model ... --toolshim on|off` (omitted keeps the stored value), `set-toolshim ALIAS on|off`, or the
  Toolshim field and column of the web Models page. `list-models` prints `toolshim=on`.
- Active only when the request has tools, the upstream answer has no structured tool call and the text holds a call:
  `<tool_call>{json}</tool_call>` (Hermes/Qwen), `<TOOLCALL>[json list]</TOOLCALL>` (Nemotron),
  `[TOOL_CALLS][json list]` (Mistral), `<|python_tag|>{json}` (Llama), or a fenced ```json block or bare JSON that is
  the whole answer. The JSON is `{"name": ..., "arguments"|"parameters": {...}}`; several calls are allowed.
- A call counts only if its name is one of the request's tools and its arguments are a JSON object; otherwise the
  answer is left untouched (nothing is invented). Text around the call stays as text; thinking is not parsed.
- Streaming: for a toolshim model with tools in the request, kiko buffers the text of that response and then sends
  the converted answer as a normal stream (so nothing is shown until the model has finished). Over 32 MiB of text
  it gives up and streams the text as it is. Models with toolshim off keep true streaming.
- The log gets one INFO line per conversion (request id, model, number of calls), never the arguments.

```
python -m kiko_lite set-toolshim nemotron-super on
```

### Upstream timeouts (slow free models)

The timeout is the longest the proxy waits for the model to say something, not a limit on the whole answer. After
connecting (10 s, fixed) it covers the wait for the response head (the first byte) and then every single read of the
body: a streaming answer that keeps sending bytes is never cut, however long it takes; an upstream that goes silent
is cut that many seconds after its last byte (the request then fails over to the next model). A slow free model
(for example `nemotron-ultra`, which needed more than 180 s before its first byte) needs a larger value.

- Global: `[proxy] upstream_timeout` in `kiko.toml` (seconds, default 300), or `serve --upstream-timeout SECONDS`
  (the flag wins over the file). Also on the web Settings page. Read when the proxy starts.
- Per model (wins over the global value): `add-model ... --timeout SECONDS` (omitted keeps the stored value; `0` or
  `default` clears it), or `set-timeout ALIAS SECONDS|default`, or the Timeout column and field of the web Models
  page (an empty field clears it). It takes effect within a few seconds without a restart.
- Health probes: `[proxy] probe_timeout` (default 20 s); a model that has its own timeout is probed with that
  timeout, but never longer than 120 s (or `probe_timeout`, if that is larger), because probes run one after another.
- Values must be more than 0 and at most 86400 seconds. `list-models` prints `timeout=<n>s` for rows where it is set.

```
python -m kiko_lite set-timeout nemotron-super 600
python -m kiko_lite set-timeout nemotron-super default
```

## Configuration

One place decides every path and port: `kikolib/config.py`. The config folder is `$KIKO_CONFIG_DIR`, else `~/.kiko`.
It holds `kiko.toml` (all settings), the database, the `.env` file with API key values, the log folder and the
first-start password files. Command-line flags override the file. Edit the file by hand or on the web Settings page.

Ports come from a base: `base_port = 4000` gives the proxy 4000, the web UI 4001 and spy (not built yet) 4002. Each can
be overridden in its own section. Two installs side by side: give each its own `KIKO_CONFIG_DIR` (and so its own
`kiko.toml` and database), e.g. one on base 4000 and one on base 5000.

```toml
# <config folder>/kiko.toml  (every key is optional; this shows the defaults)
[kiko]
base_port = 4000          # proxy = base, web = base + 1, spy = base + 2

[proxy]
host = "127.0.0.1"        # a non-loopback address makes the proxy reachable from the network
# port = 4000             # overrides base_port for the proxy only
no_auth = false           # home mode without a client key; refused unless host is loopback
upstream_timeout = 300    # seconds to wait for a model's first byte and between chunks; a model's own timeout wins
probe_timeout = 20        # the same for background health probes

[web]
host = "127.0.0.1"
# port = 4001

[spy]                     # spy is not built yet; the values are only reserved
# port = 4002

[billing]
# monthly_cap_usd = 20.0  # requests are refused with 429 once this month's spend reaches it; 0 = no cap
disable_billing = false

[health]
check_interval = 300      # seconds between probes of failing models; 0 = off
no_probe = ""             # comma separated model aliases never probed

[logs]
retention_days = 0        # delete billing rows and stored responses older than this; 0 = keep all
store_text = true         # keep every prompt and answer in kiko.db (web page "Text log"); false = store nothing
# file = "logs/kiko.log"  # also log to this file; relative to the config folder
```

`python -m kiko_lite config --json` prints the resolved values (never a secret); scripts, systemd units, `.bat` files
and the tray apps call it (or `config --get proxy_port` for one value). The settings `monthly_cap_usd`,
`disable_billing`, `health_check_interval` and `health_no_probe` used to live only in the database: the file wins, and
the old database value is still used while the file has no such key. A cap of 0 now means no cap (before this change a
database cap of 0 blocked every request); `doctor` and the proxy start warn when the database holds one. On the web
Settings page the old database value is shown in the box (marked "from DB") and kept when you save; type `0` to turn
the cap off.

## Where things are

| Page | Content |
|---|---|
| [ARCHITECTURE](ARCHITECTURE.md) | components, class diagram, one streaming request, deployment |
| [MODEL-CLASSES](MODEL-CLASSES.md) | the class idea, roles, the owner's map, resolver order |
| [ROADMAP](ROADMAP.md) | watchdog and failover, updatable providers, router target |
| [NUMBERS](NUMBERS.md) | kiko-lite vs LiteLLM-based kiko-full, what was cut |
| [CODE-TOUR](CODE-TOUR.md) | a guided tour with real code excerpts |

Source folders in the exported tree:

| Folder | Content |
|---|---|
| [kikolib/](../../../kikolib/) | the small standard-library toolkit (http, server, retry, keystore, ...) |
| [kiko_lite/](../../../kiko_lite/) | the app: gateway, resolver, providers, SQLite, CLI |
| [tests/kiko/](../../../tests/kiko/) | tests (the folder also holds older kiko-full tests) |
| [tools/](../../../tools/) | helper scripts |
| [docs/](../../../docs/) | older developer docs (mostly about kiko-full) |

Status: kiko-lite is young. Anything marked "planned" in these pages is not built yet.
