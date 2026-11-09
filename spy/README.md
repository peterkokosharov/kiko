# kiko spy

A small, read-only live viewer: one page with two logs, the LLM requests (one line per request row of the kiko
database) and the server log (the file of `[logs] file` in `kiko.toml`). Standard library only.

```
python -m spy                       # http://127.0.0.1:4002 (base_port + 2)
python -m spy --port 5002 --host 127.0.0.1
```

Flag, then `[spy] host` / `[spy] port` of `kiko.toml`, then the default (`127.0.0.1`, `[kiko] base_port` + 2). The
database is the one `kikolib.config` names (env `KIKO_DB_PATH`, `[database] path`, or `kiko.db` in the config
folder). Without `[logs] file` the server pane stays empty.

- **Read-only, decoupled.** It tails the log file and polls the `billing` table through a SQLite `mode=ro` URI
  (plus `PRAGMA query_only`). There is no ingest endpoint; the proxy does not know the spy exists, and the spy never
  writes the database or the log.
- **Buffer.** The newest 1000 lines per log are kept in memory (`spy/ring.py`); a new browser gets them first.
- **Endpoints.** `GET /` page, `/app.js`, `/app.css`; `GET /events` Server-Sent Events (primary; event `line`,
  data `{"seq","ch","text"}`, `ch` is `server` or `llm`, `Last-Event-ID` / `?after=` resume); `GET /ws` WebSocket
  (secondary, the same JSON as text messages, via `kikolib.websocket`).
- **Safety.** The `kikolib.server` browser guard is on: Host allow-list against DNS rebinding, an Origin other
  than the page's own (`http://<Host>`) is a 403 (also for `/ws`). The page inserts log text with `textContent`
  only (never `innerHTML`) and its Content-Security-Policy forbids inline script. Log text is not secret-masked here;
  the kiko logger must not write secrets, and a non-loopback bind logs a warning (plain HTTP).

Layout: `app.py` (hub, polling thread, routes) · `sources.py` (`LogTail`, `DbPoll`) · `ring.py` · `ui.py` ·
`__main__.py`. Tests: `.venv/Scripts/python -m pytest tests/kiko/test_spy.py`.
