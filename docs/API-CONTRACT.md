# kiko management API contract (v1) — what the tray needs

Status: **contract only**. kiko-lite has only `GET /health` today; the endpoints below are to be built
in Python as a separate task, FROM THIS DOCUMENT. The tray is written against it and degrades
gracefully until it exists (every dialog then shows "Management API not available"; only the
`/health` state, icon colour and start/stop work).

## General rules

- Base URL `http://127.0.0.1:<port>` (default port 4000). **Loopback only**: the server must bind
  127.0.0.1 and refuse requests whose peer address is not loopback. No TLS. The tray never connects elsewhere.
- Prefix `/kiko/v1/`. JSON in and out, UTF-8, `Content-Type: application/json`.
- `GET /health` stays unauthenticated and unchanged: `{"status":"ok"}` (kiko-lite) or
  `{"status":"healthy",...}` (kiko-full). The tray treats `ok` and `healthy` as green; any other answer
  as orange; no answer as red.
- Auth: every `/kiko/v1/*` request carries `Authorization: Bearer <token>`. The token comes from env
  `KIKO_API_TOKEN` or the first line of `~/.kiko/api-token` (created by kiko on first start, mode 0600,
  random 32+ bytes). The tray sends it only to `/kiko/v1/*`, never to `/health`, and never shows or logs it.
  Missing or wrong token: `401`.
- **Secrets never leave kiko**: no endpoint returns an API key value, ever. Log lines are redacted by the
  server (the tray redacts again as a second line of defence).
- Errors: HTTP status + body `{"error":{"code":"<machine_code>","message":"<human text>"}}`.
  `400 bad_request`, `401 unauthorized`, `404 not_found`, `405 method_not_allowed`, `409 conflict`,
  `500 internal`, `503 unavailable`. The tray treats `404`/`405` on `/kiko/v1/*` as "API not available".

## Endpoints

### GET /kiko/v1/status
```json
{"api_version":1,"version":"0.3.0","state":"ok","uptime_s":3601,"pid":4242,
 "db_path":"C:\\Users\\me\\.kiko\\kiko.db","models_enabled":5}
```

### GET /kiko/v1/models
```json
{"models":[{"name":"opus-4","class":"OPUS","enabled":true,"status":"ok"},
           {"name":"haiku-3","class":"HAIKU","enabled":false,"status":"unknown"}]}
```
`class` is a class name from `/kiko/v1/classes`; `status` is a free short word (`ok`, `down`, `unknown`).

### GET /kiko/v1/classes
```json
{"classes":[{"name":"OPUS","keyword":"opus","role":"architect"}]}
```

### POST /kiko/v1/models/{name}/class
Request `{"class":"HAIKU"}`. `{name}`: `[A-Za-z0-9._:-]+`; class: `[A-Z0-9_]+`. Reply `200`
`{"name":"haiku-3","class":"HAIKU"}`; `404` unknown model; `400` unknown class. Atomic: the new class is
visible to the next request. (The tray has the client call ready; the editing UI is a later step.)

### GET /kiko/v1/keys
Names and presence only:
```json
{"keys":[{"name":"NVIDIA_API_KEY","present":true},{"name":"OPENAI_API_KEY","present":false}]}
```
Names are environment-variable names (`[A-Za-z_][A-Za-z0-9_]*`, max 64). Any other field is ignored by the tray.

### GET /kiko/v1/costs?days=N
`N` = 1..365, default 30.
```json
{"days":30,"today_usd":0.5,"period_usd":12.25,"cap_usd":50.0,
 "per_model":[{"model":"sonnet","usd":12.25,"tokens_in":1000,"tokens_out":500}]}
```
`cap_usd` is `null` when no cap is set.

### GET /kiko/v1/logs?lines=N
`N` = 1..1000, default 200. Newest last.
```json
{"lines":["2026-10-30 10:00:01 started","2026-10-30 10:00:02 request ok"]}
```

## Tray behaviour that depends on the contract

| Situation | What the user sees |
|---|---|
| nothing listens on the port | red icon; dialogs: "kiko is not running" |
| `/health` ok, `/kiko/v1/*` answers 404/405 | green icon; dialogs: "Management API not available" |
| `401`/`403` | dialogs: "The management API refused the token" + where the token is read from |
| `5xx` with an error body | dialogs show `error.message` |
| reply is not JSON (e.g. a proxy page) | dialogs show "Management API error: HTTP <status>" |

Start/stop/restart are **not** part of the API: Windows runs the configured command, Linux uses
`systemctl --user start|stop|restart kiko-lite` (unit: `tools/kiko-lite.service` in the kiko repo),
macOS uses `launchctl` for a user LaunchAgent.
