# Code tour

Short excerpts quoted exactly from the exported tree (the committed HEAD of branch kiko-main). Each has a file and line range and a link to the file.

## 1. Resolver: from a class name to a model

The heart of the class idea. A DB alias glob wins; then a class keyword inside the requested name; then the `ANTHROPIC_DEFAULT_<CLASS>_MODEL` variable; then the name itself. Note the `break`: a class that matched but has no usable model falls through to the environment variable instead of failing.

**[kiko_lite/resolver.py:250-265](../../../kiko_lite/resolver.py)**

```python
    def __call__(self, model: str) -> tuple[str, str] | None:
        snap = self.catalog.snapshot()
        for glob, target in snap.aliases:
            if fnmatch.fnmatchcase(model, glob):
                return self._named(snap, target)
        ml = model.lower()
        for name, kw in snap.classes:
            if kw in ml:
                best = snap.best(name)
                if best is not None and best.mapped is not None:
                    return best.pname, best.mapped.model_id
                break  # class matched but no usable model: fall through to the env var
        for name, kw in snap.classes:
            if kw in ml and self.env.get(f"ANTHROPIC_DEFAULT_{name}_MODEL"):
                return self._named(snap, self.env[f"ANTHROPIC_DEFAULT_{name}_MODEL"])
        return self._named(snap, model)
```

The choice inside a class is `Snapshot.best`: status `ok` first, then `unknown`, lowest priority, lowest id.

**[kiko_lite/resolver.py:157-164](../../../kiko_lite/resolver.py)**

```python
    def best(self, cls: str) -> ModelInfo | None:
        """Best enabled, mappable model of a class: status ok first, then unknown, by priority then id."""
        for status in ("ok", "unknown"):
            hits = [m for m in self.models if m.mapped and m.row["enabled"] and m.row["model_class"] == cls
                    and m.row["status"] == status]
            if hits:
                return min(hits, key=lambda m: (m.row["priority"], m.row["id"]))
        return None
```

## 2. Anthropic to OpenAI streaming: tool calls

OpenAI streams a tool call as fragments of a JSON string; Anthropic wants a `tool_use` block followed by `input_json_delta` events. `_tool_delta` keeps one record per call index, opens the Anthropic block when the name is known, and forwards argument fragments. Some providers omit `index`; then a new `id` means a new call.

**[kiko_lite/providers/openai_compat.py:414-443](../../../kiko_lite/providers/openai_compat.py)**

```python
    def _tool_delta(self, tc: dict) -> list[tuple[str, dict]]:
        idx = tc.get("index")
        if not isinstance(idx, int):  # some providers omit the index: a new id means a new call
            tid = tc.get("id") or ""
            if self._calls and not (tid and tid != self._last_call_id):
                idx = max(self._calls)
            else:
                idx = len(self._calls)
        call = self._calls.setdefault(idx, {"id": "", "name": "", "pending": "", "block": None, "closed": False})
        fn = tc.get("function") or {}
        if tc.get("id") and not call["id"]:
            call["id"] = str(tc["id"])
            self._last_call_id = call["id"]
        if fn.get("name") and not call["name"]:
            call["name"] = str(fn["name"])
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args)
        if isinstance(args, str) and args:
            call["pending"] += args
        ev: list[tuple[str, dict]] = []
        if call["block"] is None and call["name"] and not call["closed"]:
            ev += self._begin("tool", {"type": "tool_use", "id": anthropic_id(call["id"]),
                                       "name": call["name"], "input": {}})
            call["block"], self._open_call = self._index, idx
        if call["block"] is not None and not call["closed"] and call["pending"]:
            ev.append(self._delta({"type": "input_json_delta", "partial_json": call["pending"]}))
            call["pending"] = ""
        return ev

```

`feed` turns one OpenAI chunk into Anthropic events (reasoning, text, tool calls, finish reason):

**[kiko_lite/providers/openai_compat.py:444-463](../../../kiko_lite/providers/openai_compat.py)**

```python
    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        raise_if_error(chunk)
        ev = self._start()
        u = chunk.get("usage")
        if isinstance(u, dict) and (u.get("prompt_tokens") or u.get("completion_tokens")):
            self._usage = _usage(u)
        choices = chunk.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            ev += self._emit("thinking", reasoning)
        content = delta.get("content")
        if isinstance(content, str) and content:
            ev += self._content(content)
        for tc in delta.get("tool_calls") or []:
            if isinstance(tc, dict):
                ev += self._tool_delta(tc)
        if choice.get("finish_reason"):
            self._finish = choice["finish_reason"]
```

## 3. Retry with ping keep-alive

`send_with_retry` waits between attempts in slices of at most `PING_INTERVAL` seconds and calls `on_wait` for each slice. The gateway's `on_wait` puts a ping into the queue, so Claude Code keeps getting SSE pings while the router waits for a rate-limited provider.

**[kikolib/retry.py:104-113](../../../kikolib/retry.py)**

```python
        delay = policy.delay_for(attempt, retry_after, remaining=remaining)
        if resp is not None:
            resp.close()
        left = delay
        while left > 0:
            piece = min(left, PING_INTERVAL)
            if on_wait is not None:
                on_wait(piece, attempt, reason)
            sleep(piece)
            left -= piece

```

The gateway side, in `_stream`: a worker thread does the retrying and the HTTP read; the generator sends a ping at once and again on every queue timeout.

**[kiko_lite/app.py:415-419](../../../kiko_lite/app.py)**

```python
        def on_wait(piece: float, attempt: int, why: str) -> None:
            if cancel.is_set():
                raise _Cancelled
            q.put(("ping",))

```

**[kiko_lite/app.py:450-458](../../../kiko_lite/app.py)**

```python
            yield sse.ping()  # the client gets a response at once, whatever the upstream does
            while True:
                try:
                    item = q.get(timeout=self.ping_interval)
                except queue.Empty:
                    yield sse.ping()
                    continue
                kind = item[0]
                if kind == "ping":
```

## 4. Keystore chain

Environment first, then every available OS store in priority order. A store that is present but broken (for example no daemon) is skipped, not fatal. The stores are registered with `kikolib/factory.py`, which probes them.

**[kikolib/keystore.py:257-268](../../../kikolib/keystore.py)**

```python
def register_all() -> None:
    """Register the backends with the factory (done at import; call again after factory.clear())."""
    for name, probe, cls, prio in (
        ("wcm", _probe_wcm, WcmStore, 10),
        ("keychain", _probe_keychain, KeychainStore, 10),
        ("secret-tool", _probe_secret_tool, SecretToolStore, 20),
        ("kwallet", _probe_kwallet, KWalletStore, 30),
        ("dcop", _probe_dcop, DcopStore, 40),
    ):
        factory.register(KIND, name, probe=probe, load=cls, priority=prio)


```

**[kikolib/keystore.py:279-305](../../../kikolib/keystore.py)**

```python
def get_secret(name: str, env: Mapping[str, str] = os.environ) -> str | None:
    """Secret ``name``: environment variable first, then the OS stores; else None.

    Without KIKO_KEYSTORE_IMPL every probe-ok store is tried in priority order until one
    returns a value (a store that is present but unusable, e.g. no daemon, is skipped).
    With KIKO_KEYSTORE_IMPL only that store is used.
    """
    v = env.get(name) or env.get(name.upper())
    if v:
        return v
    if os.environ.get("KIKO_KEYSTORE_IMPL"):
        stores = [_store()]
    else:
        stores = [impl for _, impl in factory.available(KIND)]
    for store in stores:
        if store is None:
            continue
        try:
            value = store.get(name)
        except Exception:
            value = None
        if value:
            return value
    return None


def set_secret(name: str, value: str) -> bool:
```

## 5. SQLite schema and migration

The schema is versioned with `PRAGMA user_version`. A database already at the current version is not written at all (a write-protected file must still open). Older databases (kiko-full or kiko-lite v1) get the missing columns and tables, and the four default classes are seeded with `INSERT OR IGNORE`.

**[kiko_lite/db.py:95-103](../../../kiko_lite/db.py)**

```python
_V2 = """
CREATE TABLE IF NOT EXISTS model_classes (
    name    TEXT PRIMARY KEY,
    keyword TEXT NOT NULL UNIQUE,
    role    TEXT NOT NULL DEFAULT '',
    sort    INTEGER NOT NULL DEFAULT 100
);
"""

```

**[kiko_lite/db.py:204-225](../../../kiko_lite/db.py)**

```python
    def _migrate(con: sqlite3.Connection) -> None:
        if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return  # already migrated: no write at all (a write-protected file must still open)
        con.executescript(_SCHEMA)
        for table, column, definition in _COLUMNS:
            if column not in Database._columns(con, table):
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():  # locked, read-only, disk full ...
                        raise
        con.executescript(_V1)
        con.executescript(_V2)
        con.executemany("INSERT OR IGNORE INTO model_classes(name, keyword, role, sort) VALUES(?,?,?,?)",
                        DEFAULT_CLASSES)
        for table, column, _ in _COLUMNS:  # never mark a half-migrated DB as migrated
            if column not in Database._columns(con, table):
                raise sqlite3.OperationalError(f"migration incomplete: {table}.{column} is missing")
        if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()

```

## Where to read next

- [kiko_lite/app.py](../../../kiko_lite/app.py): `_Gateway`, `_plan`, `_stream`
- [kiko_lite/providers/gemini.py](../../../kiko_lite/providers/gemini.py): the native Gemini translator
- [kikolib/server.py](../../../kikolib/server.py): the threaded server and SSE sending
- [tests/kiko/](../../../tests/kiko/): `test_kiko_lite_*.py` show how each part is used
