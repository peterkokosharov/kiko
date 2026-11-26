# Proposal: `kiko doctor` maintenance services

Status: draft, 2026-11-26. `doctor` today is read-only diagnosis (`cli.py:cmd_doctor`, opens the DB
read-only, never prints a secret). This adds opt-in *maintenance* actions behind the same command.
Design assumes schema v10 is landing now (`model_roles`, `display_name`) from the roles/XML agent; the
services below need a small additive **v11** and must not touch that agent's files.

Non-negotiable invariants (every service honours them):
- **Names only.** Never read, print, log or diff a key *value* (reuse `keys.key_source` → env/store/dotenv).
- **Backup before any write.** Snapshot the DB (`con.backup()` → `kiko.db.bak-STAMP`) before the first change.
- **Dry-run by default.** No flag = report the plan only; `--yes` (or `--fix ... --apply`) is required to write.
- **Ping before enable.** A catalog listing is not proof of access (harvest lesson: 6 NVIDIA ids were listed
  but answered 404). Use a large `max_tokens` (1024) so thinking models return a non-empty reply.
- **Never change class picks silently.** Keep Claude rows (`opus`/`sonnet`/`haiku` → CGW_TOKEN) untouched; new
  free rows enter at priority 900 so a class still resolves to its current winner. Any change to what a class
  resolves to is shown and needs explicit `--yes`.
- **Never delete hand-managed rows.** A row with `pinned=1` or `source='manual'` is only ever reported, never
  auto-disabled or auto-deleted.

## 1. Services

Each service runs in two phases: **plan** (read-only, prints a table) then **apply** (only with `--yes`).

**keys** — Which provider key NAMES are present. Input: DB rows' `api_key_env` + `providers.key_names`;
`keys.key_source(name)` for each. Output: name → present-in (env/store/dotenv) or MISSING. Apply step: for a
present key whose provider has models currently disabled *for lack of a key*, offer to enable them (after a
ping). Never prompts for or writes a value — storing a key stays an owner `!` line, as today.

**dead** — Mark/disable models that no longer work. Input: live `models.status`, `fail_count`,
`last_seen_upstream`, and (when `--online`) the provider catalog. A model is *dead* when either it failed the
last N probes in a row (`fail_count >= N`, N default 5) **or** its upstream id has been absent from the
provider's public catalog since `last_seen_upstream` older than D days (D default 14). Apply: set `enabled=0`
(never DELETE automatically; deletion is a separate `--fix dead --purge` for `source='auto'` rows only).
Pinned/manual rows are reported, never changed.

**harvest** — Fetch public catalogs and propose *new* free models. Sources: NVIDIA
`GET https://integrate.api.nvidia.com/v1/models` (public, no key; one key serves many vendors), the Gemini
models list (needs GEMINI key present), and `https://models.dev/api.json` for context/max_output/prices.
Filter out non-chat kinds (embedding/safety/reranker/PII/clip/detector/completion-only). For each candidate:
add the row **disabled**, `source='auto'`, ping it, enable only the ones that answer, and fill
context/max_output from models.dev. This is exactly `apply_merge.py` promoted into the tool.

**rotate** — Rename/rotation detection. Input: harvest ids vs DB ids. When an enabled id disappears and a new
id shares its stem (e.g. `...-550b-a55a` → `...-550b-a55b`, or `nemotron-ultra-253b` → `nemotron-3-ultra-550b`),
propose repointing the row's `provider` id (keeping its alias, class, priority). Apply re-pings before switch;
never repoints a pinned row.

**limits** — Refresh `context_window`, `max_output` and (when columns exist) prices from models.dev for rows
whose values are NULL or stale. Metadata only — never flips `enabled` and never pings.

**reconcile** — Compare the DB against an XML catalog file and report drift (rows only in one side, differing
class/priority/limits). Read-only here; writing is delegated to the roles-agent's `inject.py` (XML import), so
`doctor reconcile --write` just shells out to it. This keeps one import path and avoids two writers.

## 2. CLI and GUI shape

```
kiko doctor                     # unchanged: full read-only report (now also shows source/pinned/fail_count)
kiko doctor --fix keys          # plan for one service (dry-run)
kiko doctor --fix dead --yes    # apply that service
kiko doctor --fix all           # plan for keys+dead+harvest+limits (never rotate/reconcile in "all")
kiko doctor --fix harvest --online --yes
  --dry-run (default)  --yes/--apply  --online (allow network; off = local DB only)
  --provider nvidia|gemini|...  --max-add N  --fail-threshold N  --stale-days D
```
`--fix` is additive to today's report; with no `--fix` nothing new runs, so existing scripts/tests are safe.
Each service prints a plan table and a one-line summary (`would add 3, enable 2, disable 1`); `--yes` makes it
act and prints what it did. Network is used **only** with `--online`; the default stays offline and silent, so
`doctor` keeps its "no network" promise unless the owner opts in.

**Tray / web:** a new **Maintenance** panel beside Models, backed by new loopback management routes
(`POST /kiko/v1/maintenance/{service}` with `?apply=0|1`, loopback+token guarded like the rest of
`management.py`). Each service is a button: *Check keys*, *Remove dead*, *Harvest new*, *Refresh limits*. The
first click always shows the plan (diff list); a second **Apply** button commits. The panel never shows a key
value (the keys route already returns `{name, present}` only). The read-only web page gains the same plan view
without the Apply button when auth is off.

## 3. Scheduling

Optional **daily harvest**, off by default. Reuse the existing background prober thread
(`health.py:HealthProber`) rather than a new daemon: add a once-a-day tick that runs `harvest --online` and
`limits` in **plan-only** mode and writes the proposal to a file / a `doctor_pending` setting; it never enables
or disables on its own. The owner (or the tray) applies it. Controlled by settings
`doctor_auto` (off/plan/apply; default off), `doctor_auto_hour`, and reuse of `health_no_probe`. Rate limits:
at most one catalog fetch per source per run, a 30 s timeout per request (as `nvidia_harvest.py`), and the same
monthly-cap gate the prober already applies before any *paid* ping. Free pings for enable-checks are capped at
`--max-add` per run.

## 4. Data model (additive schema v11)

All nullable columns on `models`, added via the existing `_COLUMNS` + guarded `ALTER TABLE` path; no table
rewrite, kiko-full stays forward-compatible (it ignores unknown columns).

| Column | Type | Meaning / default |
|---|---|---|
| `source` | TEXT | `'manual'` (owner/CLI/XML) or `'auto'` (harvest). NULL/absent = treated as `manual`. |
| `pinned` | INTEGER | 1 = never auto-change or auto-delete. Default 0. |
| `last_seen_upstream` | TEXT | ISO date the id last appeared in a provider catalog. Set by harvest. |
| `fail_count` | INTEGER | consecutive failed probes; reset to 0 on any success. Default 0. |

`fail_count` is maintained by the health prober (`on_status`: increment on False, zero on True) so `dead` has a
signal without extra probing. `source`/`pinned` are set by `add-model` (manual ⇒ `source='manual'`) and by
harvest (`source='auto'`), and exposed in `list-models`, `doctor`, `/kiko/v1/models` and the XML export so the
roles-agent's catalog round-trips them. Migration seeds `source='manual'`, `pinned=0` for existing rows.

## 5. Test plan

- **Unit, offline (free-model team):** planner functions pure over a fixture DB + a saved catalog JSON
  (`harvest-2026-11-26.json`): dead-detection thresholds, rotation stem-match, keys present/missing, limits
  diff. Assert plan tables; assert **no DB write** without `--yes`.
- **Safety invariants (Claude writes, critical):** backup file created before first write; pinned/manual rows
  never in an apply set; Claude rows and class-pick resolution identical before/after a harvest+enable (reuse
  `test_kiko_lite_home_classes.py` style); no key value in any output or log (grep the captured stdout against
  `_SECRET`/`NO_ANSWER`).
- **Ping gate:** a mock upstream returns 404 for a listed id ⇒ row stays disabled; returns OK ⇒ enabled.
  Thinking-model case: empty reply at low `max_tokens`, non-empty at 1024 ⇒ only the latter enables.
- **Management routes:** loopback-only, token required, `apply=0` never writes; `apply=1` writes once.
- **Live smoke (owner laptop, manual):** `doctor --fix all` dry-run against the real `apps/kiko` DB; diff vs
  today's hand-run `apply_merge.py` result.

## 6. Phased implementation (small steps)

1. **v11 columns + migration** (`source`, `pinned`, `last_seen_upstream`, `fail_count`); surface them in
   `doctor`, `list-models`, `/kiko/v1/models`. — *Claude* (schema + migration safety).
2. **Prober writes `fail_count`** (increment/reset in `on_status`). — *Claude* (touches live routing).
3. **`doctor --fix keys` and `--fix limits`** (lowest risk: names + metadata only, no enable/disable). — *free
   team*, Claude review.
4. **Catalog client + `--fix harvest` plan-only** (fetch, filter kinds, diff; no writes). — *free team* (it is
   `nvidia_harvest.py` generalised).
5. **Apply path with backup + ping-before-enable + `dead`/`rotate`.** — *Claude* (this is where state changes
   and class picks could move).
6. **Maintenance management routes + tray/web panel.** — *free team* for the panel, Claude for the routes.
7. **Scheduling tick (`doctor_auto`, plan-only) in the prober.** — *Claude* (concurrency with the probe loop).
8. **`reconcile` shelling out to the roles-agent's `inject.py`** — after that agent lands; *Claude* to wire.

Rule of thumb: a free-model team can build the pure planners, the catalog parsing and the UI panel (all
testable offline with fixtures); Claude owns every step that migrates the schema, writes to the live DB, or
can change which model a class resolves to.

---
Sources: `docs/MODELS.md`, `docs/WINDOWS-FREE-MODELS.md`, `docs/gateway/site/en/MODEL-CLASSES.md`,
`gateway/kiko_lite/{cli.py,health.py,keys.py,management.py,db.py}` (read 2026-11-26);
`C:\private\team\harvest\{HARVEST-2026-11-26.md,nvidia_harvest.py,apply_merge.py}`, `C:\private\team\kiko_ping.py`
(read 2026-11-26).
