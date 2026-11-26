# Claude chain on Windows (2026-11-24)

Chain: Claude CLI -> `claude_cli_gateway.py` (port 4100) -> kiko-lite (port 4000) -> clients (rex now, Codex
later). Only Anthropic models (haiku, sonnet, opus) are reachable; no other provider key is configured and no
other model traffic leaves the machine. Everything binds to 127.0.0.1.

## Pieces and paths

| Piece | Command / file | Port |
|---|---|---|
| Python | `%USERPROFILE%\tools\python-3.14.7\python.exe` (3.14, already installed; nothing installed) | - |
| Gateway | `%USERPROFILE%\rex\scripts\claude_cli_gateway.py` (rex code, run only, not changed) | 4100 |
| kiko-lite | `python -m kiko_lite serve` from `%USERPROFILE%\kiko-src\gateway` | 4000 |
| Chain data folder | `%USERPROFILE%\.kiko-chain\` | - |
| Own database | `%USERPROFILE%\.kiko-chain\kiko.db` (selected with env `KIKO_DB_PATH`) | - |
| Gateway token | `...\.kiko-chain\gwtoken` (random hex, gateway bearer token = value of env `CGW_TOKEN` for kiko-lite) | - |
| kiko master key | `...\.kiko-chain\masterkey` (random hex; clients send it as `x-api-key` or `Authorization: Bearer`) | - |
| Logs | `...\.kiko-chain\logs\gateway.log`, `kiko-lite.log` | - |

Ports 4000/4100 were free. No pip, no venv.

Why a separate database: the default ~/.kiko/kiko.db may hold other providers;
serving it would route other classes to them and the health probe would call them.
The chain therefore uses its own DB containing only the three Claude models, so nothing else is called.

## Settings kiko-lite wrote

Only in `...\.kiko-chain\kiko.db` (before: did not exist; after: created by the commands below). Env `KIKO_DB_PATH`
set to that path and `PYTHONPATH=.` in `kiko-src\gateway` for all kiko_lite commands.

    add-provider cgw --api-base http://127.0.0.1:4100/v1 --api-key-env CGW_TOKEN
    add-model haiku  --provider cgw/haiku  --api-base http://127.0.0.1:4100/v1 --api-key-env CGW_TOKEN --class HAIKU
    add-model sonnet --provider cgw/sonnet --api-base http://127.0.0.1:4100/v1 --api-key-env CGW_TOKEN --class SONNET
    add-model opus   --provider cgw/opus   --api-base http://127.0.0.1:4100/v1 --api-key-env CGW_TOKEN --class OPUS   --price-in 4 --price-out 20

Result: providers table = the 6 built-ins + `cgw` (3 models); models haiku/sonnet/opus -> `custom/<id>`, key name
CGW_TOKEN (only the name is stored, never the value). Also created by `serve`: `...\.kiko-chain\api-token` (kiko
management token). Prices: opus is 4 / 20 USD per million tokens; sonnet and haiku have none yet (add them by running
`add-model` again with --price-in/--price-out; an unset price is not the same as free).

Existing `<home>\.kiko\kiko.db`: content unchanged (same size and date, 2026-11-05). Read-only
`config` / `list-models` calls only added empty `kiko.db-shm` and `kiko.db-wal` files next to it (SQLite side
files, harmless, may be deleted when no kiko-lite uses that DB).

## Start

Git Bash; `D` = the chain data folder. PYTHONPATH and KIKO_DB_PATH as above.

    PY=$HOME/tools/python-3.14.7/python.exe
    D=$HOME/.kiko-chain
    # 1. gateway
    cd $HOME/rex
    REX_GATEWAY_TOKEN=$(cat $D/gwtoken) nohup $PY scripts/claude_cli_gateway.py --port 4100 > $D/logs/gateway.log 2>&1 &
    # 2. kiko-lite
    cd $HOME/kiko-src/gateway
    PYTHONPATH=. KIKO_DB_PATH=$D/kiko.db CGW_TOKEN=$(cat $D/gwtoken) MASTER_KEY=$(cat $D/masterkey) \
      nohup $PY -m kiko_lite serve --host 127.0.0.1 --port 4000 > $D/logs/kiko-lite.log 2>&1 &

The gateway needs the `claude` CLI logged in (it is: `<home>\.local\bin\claude`).
At start kiko-lite probes each model through the gateway (one tiny haiku/sonnet/opus call each, status 200).

## Stop

Find the PID that listens (the Git Bash `$!` is not the Windows PID), then kill the tree:

    netstat -ano | grep -E ":(4000|4100) .*LISTENING"
    taskkill //PID <pid> //T //F

## Test results (2026-11-24, all via kiko-lite on 127.0.0.1:4000, master key in `x-api-key`/Bearer)

- `GET /health` -> `{"status":"ok"}`
- `GET /v1/models` -> `{"object":"list","data":[{"id":"haiku",...},{"id":"opus",...},{"id":"sonnet",...}]}`
- `POST /v1/chat/completions` `{"model":"haiku","messages":[{"role":"user","content":"say OK"}]}` ->
  `choices[0].message.content = "OK"`, finish_reason stop.
- `POST /v1/responses` `{"model":"haiku","input":"say OK"}` -> `status completed`, `output_text "OK"`.
- `POST /v1/responses` with `"stream":true` -> SSE: `response.created`, `output_item.added`,
  `response.output_text.delta` ("OK"), `output_text.done`, `response.completed` (sequence 0..7).
- Gateway alone (`:4100`, Bearer gateway token) also answered "OK" for haiku.

Clients: base URL `http://127.0.0.1:4000/v1`, key = contents of `masterkey`, model `haiku`/`sonnet`/`opus`.
Codex (later) uses the `/v1/responses` path tested above.

## Undo

1. Stop both servers (see Stop).
2. Delete the folder `<home>\.kiko-chain\` (database, tokens, logs, api-token). Nothing else was
   changed on the machine; rex and the default `.kiko` database were not modified.
3. Optional: delete `kiko.db-shm` and `kiko.db-wal` in `<home>\.kiko\`.
