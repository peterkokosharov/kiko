# Verified free-tier model catalog

Measured on 2026-11-22 against `kiko-lite` on Linux Mint 22.3. Every row answered a live
`POST /v1/chat/completions` with `Reply with exactly: OK` through the gateway. Run
`bash tray/tools/setup-models.sh` to install this catalog on another laptop; it writes only
key NAMES to the DB. Provider key VALUES stay in the local OS keyring.

The starting point was [llmhub's models.json](https://github.com/peterkokosharov/llmhub/blob/main/models.json).
NVIDIA rotates model ids, so the ids below are cross-checked against the live `/v1/models` list
of each provider, not copied from that file. Where llmhub's name no longer exists, the current
equivalent is used (e.g. `nemotron-ultra-253b-instruct` → `nemotron-3-ultra-550b-a55b`).

| Alias | Class | Provider / upstream id | Key name | Result |
|---|---|---|---|---|
| `nemotron-ultra` | OPUS | nvidia/nvidia/nemotron-3-ultra-550b-a55b | NVIDIA_API_KEY | OK, 45 tok |
| `gpt-astra` | OPUS | openrouter/openai/gpt-6.2-astra | OPENROUTER_API_KEY | OK, 16 tok |
| `nemotron-super` | SONNET | nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b | NVIDIA_API_KEY | OK, 44 tok |
| `llama-nemotron` | SONNET | nvidia/nvidia/nemotron-3.5-lightning-30b-a3b | NVIDIA_API_KEY | OK with `--thinking off`, 23 tok |
| `nvidia-large` | SONNET | nvidia/mistralai/mistral-large-4-1t-a52b | NVIDIA_API_KEY | OK, 33 tok |
| `gemini-lite` | HAIKU | gemini/gemini-flash-lite-latest | GEMINI_API_KEY | OK, 7 tok — fastest |
| `gemini-35-lite` | HAIKU | gemini/gemini-3.5-flash-lite | GEMINI_API_KEY | OK, 7 tok |
| `ministral-3b` | HAIKU | mistral/ministral-3b-latest | MISTRAL_API_KEY | OK, 10 tok |
| `muse-glimmer` | FABLE | nvidia/meta/muse-glimmer-30b | NVIDIA_API_KEY | OK, 162 tok |
| `codestral` | CODER | mistral/codestral-latest | MISTRAL_API_KEY | OK, 10 tok |
| `nemotron-vl` | EYES | nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b | NVIDIA_API_KEY | OK with `--thinking off`, 23 tok (text checked; image input not checked) |
| `gemini-tutor` | TUTOR | gemini/gemini-3.5-flash-lite | GEMINI_API_KEY | OK, 7 tok |
| `router-mini` | ROUTER | mistral/ministral-3b-latest | MISTRAL_API_KEY | OK, 10 tok |

## Kept but disabled

| Alias | Why |
|---|---|
| `starcoder2` | NVIDIA 404 — completion model, not a chat endpoint |
| `gemma-3-12b` | NVIDIA 404 "function not found" for this account |
| `gemma-4-31b`, `minimax-flash`, `gemini-flash` (3.8) | timed out at 75 s |
| `gemini-pro-preview` | Gemini free quota exhausted (429) |
| `mistral-small` | Mistral rate limit (429) |

Disabled models stay in the database so the tray shows them as off; they are never picked by
a class alias. Class aliases `opus`, `sonnet`, `haiku`, `fable` and `kiko-coder` all answered
through their enabled candidates in the same test run.

## Groq

`GROQ_API_KEY` returned 403 on `/v1/models` on this machine — no Groq rows are configured.

## Context window and max output (schema v9, 2026-11-26)

`models` has two nullable columns, `context_window` and `max_output` (tokens; NULL = unknown). They show in
`kiko-lite list-models` (`ctx=1M/64k`, `?` = unknown), on the web UI Models page and edit form, and in
`GET /v1/models` (`context_length`, `max_input_tokens`, `max_output_tokens`, `max_tokens`; omitted when unknown)
and `GET /model/info` (`max_input_tokens`, `max_output_tokens`). Set them with
`kiko-lite add-model ALIAS ... --context-window N --max-output N` (kept when omitted).

The tray apps show both in their Models panel as two extra columns, **Context** and **Max out** (`1M`, `262k`,
`64k`, `?` = unknown), using the same rule as `list-models`: the native tray (Win10, GTK3 on Mint, Qt6 on
Plasma, macOS) reads them from `GET /kiko/v1/models` (`context_window`, `max_output`; `null` = unknown), the
tk tray from `/ui/api/status`. The Q4OS/TQt applet has no Models panel (log panel only). The panels are not
sortable (none of their columns is).

Source codes: **A** = Anthropic model docs (2026-11-26); **C** = public catalog models.dev
(`https://models.dev/api.json`, fetched 2026-11-26; the provider entry named after `C`); **D** = the vendor's model
card, from the author's knowledge and NOT re-fetched (treat as approximate). Provider-served limits can be lower
than the model's own limit. "llmhub" rows were imported from
[llmhub models.json](https://github.com/peterkokosharov/llmhub/blob/main/models.json) (priority 900, alias `hub-<id>`);
llmhub has no context or price fields, so every figure here comes from A/C/D.

| Alias | Class | Provider id | Context | Max output | Key name | Enabled | Source |
|---|---|---|---|---|---|---|---|
| `opus` | OPUS | custom/opus (Claude chain) | 1M | 128k | CGW_TOKEN | yes | A |
| `sonnet` | SONNET | custom/sonnet (Claude chain) | 1M | 128k | CGW_TOKEN | yes | A |
| `haiku` | HAIKU | custom/haiku (Claude chain) | 1M | 128k | CGW_TOKEN | yes | A (claude-haiku-5-5) |
| `nemotron-ultra` | OPUS | nvidia/nemotron-3-ultra-550b-a55b | 1M | 64k | NVIDIA_API_KEY | yes | C nvidia |
| `gpt-astra` | OPUS | openai/gpt-6.2-astra via OpenRouter | 1.05M | 128k | OPENROUTER_API_KEY | no (no key) | C openai |
| `nemotron-super` | SONNET | nvidia/nemotron-3.5-super-vl-120b-a12b | 1048576 | 8k | NVIDIA_API_KEY | yes | C nvidia |
| `llama-nemotron` | SONNET | nvidia/nemotron-3.5-lightning-30b-a3b | 262144 | 262144 | NVIDIA_API_KEY | yes | C nvidia |
| `nvidia-large` | SONNET | mistralai/mistral-large-4-1t-a52b | 1048576 | 262144 | NVIDIA_API_KEY | yes | C mistral/mistral-large-4 (same model, Mistral API figures) |
| `gemini-lite` | HAIKU | gemini/gemini-flash-lite-latest | 1048576 | 64k | GEMINI_API_KEY | yes | C google |
| `gemini-35-lite` | HAIKU | gemini/gemini-3.5-flash-lite | 1048576 | 64k | GEMINI_API_KEY | yes | C google |
| `ministral-3b` | HAIKU | mistral/ministral-3b-latest | 131072 | 131072 | MISTRAL_API_KEY | no (no key) | C mistral |
| `muse-glimmer` | FABLE | nvidia/meta/muse-glimmer-30b | 131072 | 131072 | NVIDIA_API_KEY | yes | C nvidia |
| `codestral` | CODER | mistral/codestral-latest | 256000 | 4096 | MISTRAL_API_KEY | no (no key) | C mistral |
| `nemotron-vl` | EYES | nvidia/nemotron-3.5-super-vl-120b-a12b | 1048576 | 8k | NVIDIA_API_KEY | yes | C nvidia |
| `gemini-tutor` | TUTOR | gemini/gemini-3.5-flash-lite | 1048576 | 64k | GEMINI_API_KEY | yes | C google |
| `router-mini` | ROUTER | mistral/ministral-3b-latest | 131072 | 131072 | MISTRAL_API_KEY | no (no key) | C mistral |
| `hub-gemini-3.5-flash-lite` | HAIKU | gemini/gemini-3.5-flash-lite | 1048576 | 64k | GEMINI_API_KEY | yes (ping OK) | C google; llmhub |
| `hub-gemini-3.8-flash` | SONNET | gemini/gemini-3.8-flash | 1048576 | 64k | GEMINI_API_KEY | no (ping: timeout 40 s twice, then 529 overloaded after 36 s) | C google; llmhub |
| `hub-gemini-3.1-pro-preview` | FABLE | gemini/gemini-3.1-pro-preview | 1048576 | 64k | GEMINI_API_KEY | no (429, free quota) | C google; llmhub |
| `hub-gpt-6-luna` | FABLE | openai/gpt-6-luna | 1.05M | 128k | OPENAI_API_KEY | no (no key) | C openai; llmhub |
| `hub-gpt-5-nano` | HAIKU | openai/gpt-5-nano | 400k | 128k | OPENAI_API_KEY | no (no key) | C openai; llmhub |
| `hub-gpt-4o-mini` | HAIKU | openai/gpt-4o-mini | 128k | 16k | OPENAI_API_KEY | no (no key) | C openai; llmhub |
| `hub-nemotron-ultra-253b-instruct` | OPUS | nvidia/nemotron-ultra-253b-instruct | 131072 | ? | NVIDIA_API_KEY | no (404, id rotated away) | D; llmhub |
| `hub-nemotron-super-49b-instruct` | SONNET | nvidia/nemotron-super-49b-instruct | 131072 | ? | NVIDIA_API_KEY | no (404) | D; llmhub |
| `hub-llama-3.3-70b-instruct` | SONNET | nvidia/llama-3.3-70b-instruct | 131072 | 4096 | NVIDIA_API_KEY | no (404) | C openrouter meta-llama/llama-3.3-70b-instruct; llmhub |
| `hub-llama-3.1-nemotron-70b-instruct` | SONNET | nvidia/llama-3.1-nemotron-70b-instruct | 131072 | ? | NVIDIA_API_KEY | no (400) | D; llmhub |
| `hub-llama-3.1-70b-instruct` | HAIKU | nvidia/llama-3.1-70b-instruct | 131072 | 8192 | NVIDIA_API_KEY | no (404) | C openrouter; llmhub |
| `hub-mistral-nemo-12b-instruct` | HAIKU | nvidia/mistral-nemo-12b-instruct | 131072 | 16384 | NVIDIA_API_KEY | no (404) | C openrouter mistralai/mistral-nemo; llmhub |
| `hub-llama-3.2-3b-instruct` | HAIKU | nvidia/llama-3.2-3b-instruct | 131072 | 131072 | NVIDIA_API_KEY | no (404) | C openrouter; llmhub |
| `hub-phi-3-mini-128k-instruct` | HAIKU | nvidia/phi-3-mini-128k-instruct | 131072 | ? | NVIDIA_API_KEY | no (404) | D; llmhub |
| `hub-deepseek-r1` | FABLE | nvidia/deepseek-r1 | 131072 | ? | NVIDIA_API_KEY | no (404) | D; llmhub |
| `hub-qwen2.5-72b-instruct` | SONNET | nvidia/qwen2.5-72b-instruct | 131072 | 8192 | NVIDIA_API_KEY | no (404) | D; llmhub |
| `hub-qwen2.5-coder-32b-instruct` | SONNET | nvidia/qwen2.5-coder-32b-instruct | 131072 | 8192 | NVIDIA_API_KEY | no (404) | D; llmhub |
| `hub-mistral-7b-instruct-v0.3` | HAIKU | nvidia/mistral-7b-instruct-v0.3 | 32768 | ? | NVIDIA_API_KEY | no (404) | D; llmhub |
| `hub-gemma-2-27b-it` | HAIKU | nvidia/gemma-2-27b-it | 8192 | 2048 | NVIDIA_API_KEY | no (404) | C openrouter google/gemma-2-27b-it; llmhub |
| `hub-gemma-2-9b-it` | HAIKU | nvidia/gemma-2-9b-it | 8192 | ? | NVIDIA_API_KEY | no (404) | D; llmhub |

Other Anthropic figures (for routes added later): claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5, claude-fable-5-x
1,000,000 / 128,000; Claude Haiku 4.5 200,000 / 64,000; Sonnet 4.6 and Opus 4.7/4.8 1,000,000 / 128,000 (A).

The `hub-*` NVIDIA rows are kept (disabled, status error) as a record of llmhub's stale ids: the current NVIDIA
catalog (models.dev, 41 entries) holds none of them; use the verified rows above instead. Enabling one of them
re-checks it. The ping of the enabled `hub-*` rows was done through `kiko_ping.py`; class routing is unchanged
(`hub-*` rows have priority 900, below every existing row of their class). A timestamped backup
`kiko.db.bak-YYYYMMDD-HHMMSS-llmhub` was made next to the live DB before the import.

### NVIDIA-hosted stand-ins for models whose own provider has no key yet (2026-11-26)

Checked against NVIDIA's public model list (`GET https://integrate.api.nvidia.com/v1/models`, no key needed, 30
entries that day) and models.dev. NVIDIA hosts no OpenAI model, no Codestral/Ministral, and none of llmhub's old
Llama/Qwen/Phi/DeepSeek ids. The original-provider rows stay disabled until their keys arrive. "Served by" is the
provider that really answers.

| Alias | Class | Stand-in for | Served by (provider id) | Context / out | Ping | Enabled |
|---|---|---|---|---|---|---|
| `codestral-nv` | CODER | codestral (Mistral) | NVIDIA `poolside/laguna-xs-2.1` (closest coder by role, not the same model) | 262144 / 16384 (C nvidia) | 200 OK | yes |
| `router-mini-nv` | ROUTER | router-mini / ministral-3b (Mistral) | NVIDIA `nvidia/nemotron-3.5-bolt-e4b` (small model by role) | unknown (not in models.dev) | 200 OK | yes |
| `ministral-3b-nv` | HAIKU | ministral-3b (Mistral) | NVIDIA `google/gemma-3-4b-it` | 131072 / 16384 (C nvidia) | 404 | no |
| `gpt-4o-mini-nv` | HAIKU | hub-gpt-4o-mini (OpenAI) | NVIDIA `google/gemma-3-12b-it` | 131072 / 16384 (C nvidia) | 404 | no |

No NVIDIA equivalent exists for `hub-gpt-6-luna`, `hub-gpt-5-nano`, `gpt-astra` (OpenAI / OpenRouter); Mistral Large 4
is already served by NVIDIA as `nvidia-large`. NVIDIA also lists models llmhub does not know (kimi-k3.1-pro, glm-5.3-flash,
minimax-m3.1-flash, step-5-preview); they were not added.

## One model, many roles (schema v10, 2026-11-26)

A model serves one PRIMARY class (`models.model_class`: shown in lists, used for sorting, what kiko-full knows) and any
number of further roles, stored in `model_roles(model_id, class_name)`. A class alias (`opus`, `sonnet`, `haiku`,
`fable`, `coder`, `eyes`, `tutor`, `router`, ...) picks its candidates among the models that HAVE that role, then by
the unchanged rules: enabled, status ok / unknown / error, priority, id. A model with no role rows (written by
kiko-full) serves its primary class only. The v9 to v10 migration copies each model's class as its first role, so
today's routing is identical (tests rebuild the live data shape and compare `opus`, `sonnet`, `haiku`, ... before
and after). `display_name` (nullable) is the human name used by the catalog export.

* CLI: `kiko-lite add-model ALIAS ... --role OPUS,CODER,EYES` (the first is the primary unless `--class` is given; the
  primary is always one of the roles; `--role` replaces the set, omitted = kept; at least one role). `list-models`
  prints `roles=...` for models with more than one. `--display-name "Anthropic Opus 5.5"`.
* Web UI: the Models form has a Roles checklist next to the primary class; the list shows "Roles (primary first)".
* Tray: the Models panel (native Win10 / GTK3 / Qt6 / macOS through `kt_presenter.c`, and the tk tray) shows the roles
  in place of the class. The trays have no editing UI (the panels are read-only tables), so there is no checklist
  there; roles are changed with `POST /kiko/v1/models/{name}/roles` (docs/API-CONTRACT.md), the CLI or the web UI.
* Owner decision 2026-11-26: the Claude Opus route (`opus`, "Anthropic Opus 5.5") has roles OPUS (primary), CODER and
  EYES, context 1M, max output 128k.

## XML export of the catalog (2026-11-26)

`kiko-lite export-models --xml [--out FILE]` writes the whole catalog; the gateway serves the same bytes at
`GET /kiko/v1/models.xml` (loopback only, management token, like every `/kiko/v1/*` route; `Content-Type:
application/xml`). UTF-8, written with `xml.etree.ElementTree`, ordered by alias, so two exports of the same
catalog are byte-identical. Only key NAMES appear; a stored text that looks like a key value is replaced by
`<invalid name, value hidden>`. Schema: [`docs/schema/kiko-models.xsd`](schema/kiko-models.xsd) (version "1").

```xml
<?xml version="1.0" encoding="UTF-8"?>
<kiko-models version="1">
  <model id="opus" enabled="true" status="ok">
    <alias>Anthropic Opus 5.5</alias>      <!-- display_name; the alias when none is set -->
    <name>cgw/opus</name>                  <!-- full provider model id as stored -->
    <key>CGW_TOKEN</key>                   <!-- key NAME, never a value -->
    <api_base>http://127.0.0.1:4100/v1</api_base>   <!-- only when the row has its own base -->
    <roles>
      <role primary="true">OPUS</role>
      <role>EYES</role>
      <role>CODER</role>
    </roles>
    <context>1000000</context>             <!-- tokens; always present, empty when unknown -->
    <max_output>128000</max_output>
    <price per="1M">free</price>           <!-- or <in>/<out>[/<cache_read>] in USD, or empty = unknown -->
    <priority>100</priority>
    <!-- <thinking>, <timeout>, <toolshim> only when set -->
  </model>
</kiko-models>
```

`<price>` reads `free` when the stored price is 0 / 0 or the provider is a free-tier provider with no stored price
(`FREE_TIER_PROVIDERS` in `kiko_lite/catalog_xml.py`: `nvidia`); `<in>` / `<out>` (and `<cache_read>` when known)
when priced; empty when the price is unknown. The `enabled` and `status` attributes are the row's own.

## XML import: `inject.py` / `import-models` (2026-11-26)

The reverse of the export: `kiko-lite import-models --xml FILE [--dry-run]`, or the same through
`python tools/inject.py FILE [--dry-run] [--db PATH]`. It reads the format above (`docs/schema/kiko-models.xsd`)
and upserts models by alias (`<model id="...">`):

* **Validated first, all errors at once.** The whole file is checked (well-formed, version "1", no DOCTYPE, every
  required element, key a NAME in UPPER_SNAKE_CASE, roles that exist as classes, whole-number tokens, numeric
  prices, a model id kiko can route). Every problem is listed; with any problem nothing is written. A file that
  contains anything shaped like an API key value (`sk-...`, `nvapi-...`, `AIza...`, `AKIA...`, ...) in any text or
  attribute is refused as a whole, and the value is never echoed back.
* **All or nothing.** The write is one SQLite transaction; if the database refuses one model (for example an alias
  that differs only in case from an existing one) none of them is written.
* **`--dry-run`** prints `+ added`, `~ changed (fields)` and `= unchanged` per model and writes nothing (it does not
  even create a missing database).
* **Backup.** A real write into an existing database is preceded by `kiko.db.bak-YYYYMMDD-HHMMSS-import` (SQLite
  backup API); nothing to change means no write and no backup.
* **What is taken from the file:** name (provider/model id), key NAME, api_base, roles (the `primary="true"` one, else
  the first, is the primary), context, max output, price (`free` = 0 / 0; empty = unknown, kept as is), priority,
  thinking, timeout, toolshim, and the human name (`<alias>` text when it differs from the id). Empty context /
  price in the file keep the database value. The `enabled` flag and the health `status` of an EXISTING model are never
  changed by an import.
* **New models are added disabled** unless the file says `enabled="true"` AND the model answers a ping through the
  running kiko (a tiny `/v1/messages` call to `127.0.0.1:<port>`): it is enabled for the ping, then stays enabled with
  status ok, or is switched off again with status error. With kiko not running the ping fails and the model stays
  disabled.
* Round trip: export, import into an empty database (every pinged model answers), export again gives byte-identical
  XML (covered by a test). Health `status` is operational state and is not carried by the file.

