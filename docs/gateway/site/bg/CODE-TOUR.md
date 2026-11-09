# Обиколка на кода

Кратки откъси, цитирани точно от експортираното дърво (комитнатият HEAD на клон kiko-main). Всеки има файл и диапазон от редове и връзка към файла.

## 1. Resolver: от име на клас до модел

Сърцето на идеята за класовете. Печели alias глоб от базата; после ключова дума на клас в поисканото име; после променливата `ANTHROPIC_DEFAULT_<CLASS>_MODEL`; после самото име. Забележете `break`: клас, който е съвпаднал, но няма използваем модел, пада към променливата на средата вместо да се провали.

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

Изборът в рамките на клас е `Snapshot.best`: първо статус `ok`, после `unknown`, най-малък priority, най-малък id.

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

## 2. Streaming от Anthropic към OpenAI: извиквания на инструменти

OpenAI праща извикване на инструмент като фрагменти от JSON низ; Anthropic иска блок `tool_use`, следван от събития `input_json_delta`. `_tool_delta` пази по един запис за всеки индекс на извикване, отваря блока на Anthropic, когато името е известно, и препраща фрагментите от аргументите. Някои доставчици пропускат `index`; тогава нов `id` означава ново извикване.

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

`feed` превръща един OpenAI chunk в събития на Anthropic (разсъждение, текст, извиквания на инструменти, причина за край):

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

## 3. Повторен опит с ping keep-alive

`send_with_retry` чака между опитите на парчета от най-много `PING_INTERVAL` секунди и вика `on_wait` за всяко парче. `on_wait` на gateway слага ping в опашката, така че Claude Code продължава да получава SSE ping, докато рутерът чака доставчик с изчерпан лимит на заявки.

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

Страната на gateway, в `_stream`: работна нишка прави повторните опити и четенето по HTTP; генераторът праща ping веднага и пак при всеки timeout на опашката.

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

## 4. Верига от хранилища за ключове

Първо средата, после всяко налично хранилище на ОС по ред на приоритет. Хранилище, което е налично, но повредено (например няма daemon), се прескача и не е фатално. Хранилищата се регистрират във `kikolib/factory.py`, който ги проверява (probe).

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

## 5. SQLite схема и миграция

Схемата е с версия чрез `PRAGMA user_version`. База, която вече е на текущата версия, изобщо не се пише (файл със защита от запис все пак трябва да се отваря). По-старите бази (kiko-full или kiko-lite v1) получават липсващите колони и таблици, а четирите класа по подразбиране се зареждат с `INSERT OR IGNORE`.

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

## Какво да се чете нататък

- [kiko_lite/app.py](../../../kiko_lite/app.py): `_Gateway`, `_plan`, `_stream`
- [kiko_lite/providers/gemini.py](../../../kiko_lite/providers/gemini.py): родният преводач за Gemini
- [kikolib/server.py](../../../kikolib/server.py): многонишковият сървър и изпращането на SSE
- [tests/kiko/](../../../tests/kiko/): `test_kiko_lite_*.py` показват как се ползва всяка част
