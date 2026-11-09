# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""TASK #132: toolshim turns tool calls a weak model wrote as text into real tool_use blocks (per-model switch
models.toolshim, schema 8). Fully offline: fake upstreams are kikolib.server Apps on 127.0.0.1:0; the config dir and
the DB are in tmp."""
import json
import logging
import sqlite3
import time

import pytest

from kiko_lite import db as kdb
from kiko_lite import toolshim
from kiko_lite.db import Database
from kiko_lite.resolver import build_db_app
from kikolib import http as khttp
from kikolib import retry, server

from .test_kiko_lite_cli import run, store  # noqa: F401  (the cli fixtures)

MASTER = "master-key"
SECRET = "UPSTREAM-SECRET-KEY-9876"
NAMES = {"get_weather", "read_file"}
TOOLS = [{"name": "get_weather", "description": "w", "input_schema": {"type": "object"}},
         {"name": "read_file", "description": "r", "input_schema": {"type": "object"}}]
CALL = {"name": "get_weather", "arguments": {"city": "Sofia"}}


# ---- the parser ----

@pytest.mark.parametrize("text", [
    '<tool_call>{"name": "get_weather", "arguments": {"city": "Sofia"}}</tool_call>',
    '<tool_call>\n{"name": "get_weather", "parameters": {"city": "Sofia"}}\n</tool_call>',
    '<TOOLCALL>[{"name": "get_weather", "arguments": {"city": "Sofia"}}]</TOOLCALL>',
    '[TOOL_CALLS][{"name": "get_weather", "arguments": {"city": "Sofia"}}]',
    '[TOOL_CALLS] [{"name": "get_weather", "arguments": {"city": "Sofia"}}]',
    '<|python_tag|>{"name": "get_weather", "parameters": {"city": "Sofia"}}',
    '```json\n{"name": "get_weather", "arguments": {"city": "Sofia"}}\n```',
    '```\n{"name": "get_weather", "arguments": {"city": "Sofia"}}\n```',
    '  {"name": "get_weather", "arguments": {"city": "Sofia"}}  ',
    '{"name": "get_weather", "arguments": "{\\"city\\": \\"Sofia\\"}"}',
    '[{"name": "get_weather", "arguments": {"city": "Sofia"}}]',
    '{"type": "function", "function": {"name": "get_weather", "arguments": {"city": "Sofia"}}}',
])
def test_each_format(text):
    got = toolshim.extract(text, NAMES)
    assert got == ("", [{"name": "get_weather", "input": {"city": "Sofia"}}])


def test_mixed_text_and_call_keeps_the_text_trimmed():
    got = toolshim.extract('Let me check.\n<tool_call>{"name": "get_weather", "arguments": {}}</tool_call>\nDone.',
                           NAMES)
    assert got == ("Let me check.\n\nDone.", [{"name": "get_weather", "input": {}}])
    got = toolshim.extract('  Checking... [TOOL_CALLS][{"name": "read_file", "arguments": {"p": "a"}}]', NAMES)
    assert got == ("Checking...", [{"name": "read_file", "input": {"p": "a"}}])


def test_multiple_calls():
    two = ('<tool_call>{"name": "get_weather", "arguments": {"city": "A"}}</tool_call>'
           '<tool_call>{"name": "read_file", "arguments": {"p": "b"}}</tool_call>')
    assert toolshim.extract(two, NAMES)[1] == [{"name": "get_weather", "input": {"city": "A"}},
                                               {"name": "read_file", "input": {"p": "b"}}]
    lst = '<TOOLCALL>[{"name": "get_weather", "arguments": {}}, {"name": "read_file", "arguments": {}}]</TOOLCALL>'
    assert [c["name"] for c in toolshim.extract(lst, NAMES)[1]] == ["get_weather", "read_file"]
    py = '<|python_tag|>{"name": "get_weather", "parameters": {}}; {"name": "read_file", "parameters": {}}'
    assert [c["name"] for c in toolshim.extract(py, NAMES)[1]] == ["get_weather", "read_file"]


@pytest.mark.parametrize("text", [
    '<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>',  # unknown tool
    '{"name": "rm_rf", "arguments": {}}',
    '[TOOL_CALLS][{"name": "get_weather", "arguments": {}}, {"name": "rm_rf", "arguments": {}}]',  # one bad: none
    '<tool_call>{"name": "get_weather", "arguments": </tool_call>',  # bad JSON
    '<tool_call>{"name": "get_weather", "arguments": {"a": 1}',  # not closed
    '[TOOL_CALLS][{"name": "get_weather", "arguments": ',
    '{"name": "get_weather", "arguments": [1, 2]}',  # arguments not an object
    '{"name": "get_weather", "arguments": "not json"}',
    '{"name": "get_weather"}',  # no arguments at all
    '{"name": "get_weather", "arguments": {}} and more text',  # bare JSON must be the whole answer
    'Here: {"name": "get_weather", "arguments": {}}',
    'hello, no tool call here',
    '',
])
def test_not_a_valid_call_is_left_alone(text):
    assert toolshim.extract(text, NAMES) is None
    assert toolshim.extract('<tool_call>{"name": "get_weather", "arguments": {}}</tool_call>', set()) is None


def test_a_bad_block_stays_as_text_next_to_a_good_one():
    text = ('<tool_call>{"name": "nope", "arguments": {}}</tool_call>'
            '<tool_call>{"name": "get_weather", "arguments": {}}</tool_call>')
    rest, calls = toolshim.extract(text, NAMES)
    assert [c["name"] for c in calls] == ["get_weather"] and "nope" in rest


# ---- db: column, migration, CLI ----

def test_schema_8_column_and_upsert_semantics(tmp_path):
    d = Database(tmp_path / "k.db")
    assert kdb.SCHEMA_VERSION == 8 and d.schema_version() == 8
    kw = dict(api_base="http://127.0.0.1:1/v1")
    d.upsert_model("m", "acme/x", "KEY_X", **kw)
    assert d.get_model("m")["toolshim"] is None
    d.upsert_model("m", "acme/x", "KEY_X", toolshim="on", **kw)
    assert d.get_model("m")["toolshim"] == "on"
    d.upsert_model("m", "acme/x", "KEY_X", **kw)  # omitted: kept
    assert d.get_model("m")["toolshim"] == "on"
    d.upsert_model("m", "acme/x", "KEY_X", toolshim="off", **kw)
    assert d.get_model("m")["toolshim"] is None
    assert d.set_toolshim("m", "on") and d.get_model("m")["toolshim"] == "on"
    assert d.set_toolshim("m", "off") and d.get_model("m")["toolshim"] is None
    assert not d.set_toolshim("nope", "on")
    for bad in ("maybe", "", "ON"):
        with pytest.raises(ValueError):
            d.set_toolshim("m", bad)
        with pytest.raises(ValueError):
            d.upsert_model("m", "acme/x", "KEY_X", toolshim=bad, **kw)


def test_v7_db_with_rows_migrates_and_keeps_them(tmp_path):
    p = tmp_path / "v7.db"
    d = Database(p)
    d.upsert_model("keep", "acme/x", "KEY_X", api_base="http://127.0.0.1:1/v1", model_class="HAIKU", priority=3,
                   thinking="off", timeout=600, price_in=1.5)
    d.upsert_model("two", "acme/y", "KEY_Y")
    con = sqlite3.connect(p)
    con.execute("ALTER TABLE models DROP COLUMN toolshim")  # the v7 shape
    con.execute("PRAGMA user_version=7")
    con.commit()
    con.close()
    d2 = Database(p)
    assert d2.schema_version() == 8
    rows = {m["alias"]: m for m in d2.models()}
    assert set(rows) == {"keep", "two"}
    assert rows["keep"]["priority"] == 3 and rows["keep"]["thinking"] == "off" and rows["keep"]["timeout"] == 600.0
    assert rows["keep"]["price_in_per_mtok"] == 1.5 and rows["keep"]["toolshim"] is None
    assert rows["two"]["toolshim"] is None


def test_cli_set_toolshim_and_list(run):
    base = ("add-model", "nim", "--provider", "nvidia/nvidia/nemotron-x", "--api-key-env", "NVIDIA_API_KEY")
    assert run(*base)[0] == 0 and Database(run.path).get_model("nim")["toolshim"] is None
    assert "toolshim=" not in run("list-models")[1]
    assert run("set-toolshim", "nim", "on")[0] == 0 and Database(run.path).get_model("nim")["toolshim"] == "on"
    assert "toolshim=on" in run("list-models")[1]
    assert run(*base)[0] == 0 and Database(run.path).get_model("nim")["toolshim"] == "on"  # kept
    assert run(*base, "--toolshim", "off")[0] == 0 and Database(run.path).get_model("nim")["toolshim"] is None
    assert run(*base, "--toolshim", "on")[0] == 0 and Database(run.path).get_model("nim")["toolshim"] == "on"
    assert run("set-toolshim", "nim", "off")[0] == 0 and Database(run.path).get_model("nim")["toolshim"] is None
    assert run("set-toolshim", "nope", "on")[0] == 1
    with pytest.raises(SystemExit):  # argparse refuses a bad mode
        run("set-toolshim", "nim", "maybe")


# ---- the gateway: a fake OpenAI-compatible upstream ----

class Upstream:
    """Answers every chat completion with ``content`` (and ``tool_calls`` when given); streaming cuts the text
    into 5-character pieces. ``gap`` = seconds between stream frames."""

    def __init__(self, content="", tool_calls=None, reasoning=None, gap=0.0, finish=None):
        self.content, self.tool_calls, self.reasoning, self.gap = content, tool_calls, reasoning, gap
        self.finish = finish
        app = server.App()
        app.route("POST", "/v1/chat/completions")(self._play)
        self.server = server.Server(app, port=0).start()

    @property
    def base(self):
        return self.server.url + "/v1"

    def _play(self, req):
        body = req.json()
        finish = self.finish or ("tool_calls" if self.tool_calls else "stop")
        if body.get("stream"):
            def gen():
                if self.reasoning:
                    yield self._frame({"reasoning_content": self.reasoning})
                for i in range(0, len(self.content), 5):
                    if self.gap:
                        time.sleep(self.gap)
                    yield self._frame({"content": self.content[i:i + 5]})
                if self.tool_calls:
                    yield self._frame({"tool_calls": [{**c, "index": n} for n, c in enumerate(self.tool_calls)]})
                yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n"
                yield "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}) + "\n\n"
                yield "data: [DONE]\n\n"
            return server.StreamResponse(gen())
        msg = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.reasoning:
            msg["reasoning_content"] = self.reasoning
        return server.JsonResponse({"choices": [{"message": msg, "finish_reason": finish}],
                                    "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    @staticmethod
    def _frame(delta):
        return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}) + "\n\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    db = Database(tmp_path / "k.db")
    ups, clients = [], []

    class W:
        pass

    w = W()
    w.db = db

    def upstream(**kw):
        u = Upstream(**kw)
        ups.append(u)
        return u

    def start(up, shim=True):
        db.upsert_model("a", "acme/a-model", "KEY_A", api_base=up.base, model_class="HAIKU", priority=1,
                        toolshim="on" if shim else None)
        policy = retry.RetryPolicy(attempts=1, base_delay=0.01, max_delay=0.02, budget=5.0, jitter=False)
        app = build_db_app(db, MASTER, secret={"KEY_A": SECRET}.get, env={}, ttl=0.0, policy=policy,
                           ping_interval=0.2, upstream_timeout=5.0, probe_timeout=1.0)
        srv = server.Server(app, port=0).start()
        c = khttp.StdlibBackend()
        clients.append((srv, c))

        def post(stream=False, tools=TOOLS, **extra):
            payload = {"model": "claude-haiku-9", "max_tokens": 64, "stream": stream,
                       "messages": [{"role": "user", "content": "weather?"}]}
            if tools is not None:
                payload["tools"] = tools
            payload.update(extra)
            h = {"x-api-key": MASTER, "content-type": "application/json"}
            return c.request("POST", srv.url + "/v1/messages", h, json.dumps(payload).encode(), stream=stream)
        return post

    w.upstream, w.start = upstream, start
    yield w
    for srv, c in clients:
        c.close()
        srv.stop()
    for u in ups:
        u.server.stop()


def sse_events(resp):
    """The Anthropic SSE events of a streamed response: [(name, data)], pings skipped."""
    out, name = [], None
    for line in resp.iter_lines():
        line = line.rstrip("\r\n")
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and name:
            if name != "ping":
                out.append((name, json.loads(line[5:])))
            name = None
    return out


def assert_valid_stream(events):
    """message_start, balanced blocks with ascending indexes, message_delta, message_stop, nothing after."""
    names = [n for n, _ in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    open_idx, expect = None, 0
    for n, d in events[1:-2]:
        if n == "content_block_start":
            assert open_idx is None and d["index"] == expect
            open_idx, expect = d["index"], expect + 1
        elif n == "content_block_delta":
            assert d["index"] == open_idx
        elif n == "content_block_stop":
            assert d["index"] == open_idx
            open_idx = None
        else:
            raise AssertionError(n)
    assert open_idx is None


def tool_uses_of_stream(events):
    blocks, parts = {}, {}
    for n, d in events:
        if n == "content_block_start":
            blocks[d["index"]] = d["content_block"]
        elif n == "content_block_delta" and d["delta"]["type"] == "input_json_delta":
            parts.setdefault(d["index"], []).append(d["delta"]["partial_json"])
    return [{**b, "input": json.loads("".join(parts.get(i, [])) or "{}")} for i, b in sorted(blocks.items())
            if b["type"] == "tool_use"]


TEXT_CALL = 'Checking now. <tool_call>{"name": "get_weather", "arguments": {"city": "Sofia"}}</tool_call>'


def test_nonstream_text_call_becomes_tool_use(world, caplog):
    caplog.set_level(logging.INFO, logger="kiko_lite.app")
    post = world.start(world.upstream(content=TEXT_CALL))
    r = post()
    assert r.status == 200
    m = r.json()
    assert m["stop_reason"] == "tool_use" and m["stop_sequence"] is None
    assert [b["type"] for b in m["content"]] == ["text", "tool_use"]
    assert m["content"][0]["text"] == "Checking now."
    tu = m["content"][1]
    assert tu["name"] == "get_weather" and tu["input"] == {"city": "Sofia"} and tu["id"].startswith("toolu_")
    assert m["usage"]["input_tokens"] == 7
    lines = [r.getMessage() for r in caplog.records if "toolshim" in r.getMessage()]
    assert len(lines) == 1 and "1 text tool call" in lines[0] and "Sofia" not in lines[0]


def test_nonstream_multiple_calls_and_reasoning_kept(world):
    text = ('<TOOLCALL>[{"name": "get_weather", "arguments": {"city": "A"}}, '
            '{"name": "read_file", "arguments": {"p": "b"}}]</TOOLCALL>')
    m = world.start(world.upstream(content=text, reasoning="thinking hard"))().json()
    assert [b["type"] for b in m["content"]] == ["thinking", "tool_use", "tool_use"]
    assert [b["name"] for b in m["content"][1:]] == ["get_weather", "read_file"] and m["stop_reason"] == "tool_use"
    assert len({b["id"] for b in m["content"][1:]}) == 2


def test_reasoning_text_is_not_parsed(world):
    m = world.start(world.upstream(content="no call here", reasoning=TEXT_CALL))().json()
    assert m["stop_reason"] == "end_turn" and all(b["type"] != "tool_use" for b in m["content"])


def test_off_leaves_the_answer_untouched(world):
    m = world.start(world.upstream(content=TEXT_CALL), shim=False)().json()
    assert m["stop_reason"] == "end_turn" and [b["type"] for b in m["content"]] == ["text"]
    assert "<tool_call>" in m["content"][0]["text"]


def test_no_tools_in_the_request_leaves_it_untouched(world):
    m = world.start(world.upstream(content=TEXT_CALL))(tools=None).json()
    assert m["stop_reason"] == "end_turn" and [b["type"] for b in m["content"]] == ["text"]


def test_unknown_tool_name_and_bad_json_are_not_converted(world):
    for text in ('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>',
                 '<tool_call>{"name": "get_weather", "arguments": </tool_call>'):
        m = world.start(world.upstream(content=text))().json()
        assert m["stop_reason"] == "end_turn" and m["content"][0]["text"] == text


def test_structured_tool_calls_from_upstream_are_untouched(world):
    real = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"p": "x"}'}}]
    m = world.start(world.upstream(content=TEXT_CALL, tool_calls=real))().json()
    uses = [b for b in m["content"] if b["type"] == "tool_use"]
    assert len(uses) == 1 and uses[0]["name"] == "read_file" and uses[0]["input"] == {"p": "x"}
    assert any(b["type"] == "text" and "<tool_call>" in b["text"] for b in m["content"])  # text left as it was


def test_streaming_text_call_becomes_a_valid_tool_use_stream(world, caplog):
    caplog.set_level(logging.INFO, logger="kiko_lite.app")
    post = world.start(world.upstream(content=TEXT_CALL, reasoning="hmm"))
    r = post(stream=True)
    assert r.status == 200
    ev = sse_events(r)
    assert_valid_stream(ev)
    uses = tool_uses_of_stream(ev)
    assert len(uses) == 1 and uses[0]["name"] == "get_weather" and uses[0]["input"] == {"city": "Sofia"}
    assert uses[0]["id"].startswith("toolu_")
    kinds = [d["content_block"]["type"] for n, d in ev if n == "content_block_start"]
    assert kinds == ["thinking", "text", "tool_use"]
    text = "".join(d["delta"]["text"] for n, d in ev if n == "content_block_delta" and d["delta"]["type"] == "text_delta")
    assert text == "Checking now."
    md = [d for n, d in ev if n == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use" and md["usage"]["output_tokens"] == 3
    assert sum("text tool call" in r.getMessage() for r in caplog.records) == 1


def test_streaming_multiple_calls(world):
    text = '[TOOL_CALLS][{"name": "get_weather", "arguments": {"city": "A"}}, {"name": "read_file", "arguments": {}}]'
    ev = sse_events(world.start(world.upstream(content=text))(stream=True))
    assert_valid_stream(ev)
    assert [u["name"] for u in tool_uses_of_stream(ev)] == ["get_weather", "read_file"]
    assert not any(d["content_block"]["type"] == "text" for n, d in ev if n == "content_block_start")


def test_streaming_without_a_call_is_passed_on_as_text(world):
    ev = sse_events(world.start(world.upstream(content="just an answer, nothing to call"))(stream=True))
    assert_valid_stream(ev)
    text = "".join(d["delta"]["text"] for n, d in ev if n == "content_block_delta")
    assert text == "just an answer, nothing to call" and not tool_uses_of_stream(ev)
    assert [d for n, d in ev if n == "message_delta"][0]["delta"]["stop_reason"] == "end_turn"


def test_streaming_structured_tool_calls_are_untouched(world):
    real = [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"p": "x"}'}}]
    ev = sse_events(world.start(world.upstream(content="", tool_calls=real))(stream=True))
    assert_valid_stream(ev)
    uses = tool_uses_of_stream(ev)
    assert len(uses) == 1 and uses[0]["input"] == {"p": "x"}


def test_non_shim_streaming_still_streams(world):
    """Off: the first text delta reaches the client while the upstream is still sending (true streaming)."""
    up = world.upstream(content="w" * 40, gap=0.15)  # 8 frames x 0.15 s
    r = world.start(up, shim=False)(stream=True)
    t0, first = time.monotonic(), None
    for line in r.iter_lines():
        if first is None and "text_delta" in line:
            first = time.monotonic() - t0
    assert first is not None and first < 0.8  # well before the ~1.2 s the whole answer takes


def test_shim_streaming_buffers_until_the_end(world):
    """The first content event comes only when the upstream is done (~1.8 s of chunks), not while it is sending."""
    up = world.upstream(content=TEXT_CALL, gap=0.1)
    r = world.start(up)(stream=True)
    t0, first = time.monotonic(), None
    for line in r.iter_lines():
        if first is None and "content_block_start" in line:
            first = time.monotonic() - t0
    assert first is not None and first > 1.0


def test_shim_streaming_keeps_pinging_while_chunks_arrive(world):
    """Chunks come every 0.1 s (< ping_interval 0.2 s): the keepalive must still be sent while buffering."""
    up = world.upstream(content=TEXT_CALL, gap=0.1)  # ~1.8 s in all = ~9 ping intervals
    r = world.start(up)(stream=True)
    pings, seen_content = 0, False
    for line in r.iter_lines():
        if "event: ping" in line and not seen_content:
            pings += 1
        elif "event: content_block_start" in line:
            seen_content = True
    assert seen_content and pings >= 5  # 1 at once + the ones while buffering


def test_tool_choice_none_disables_the_shim(world):
    post = world.start(world.upstream(content=TEXT_CALL))
    m = post(tool_choice={"type": "none"}).json()
    assert m["stop_reason"] == "end_turn" and [b["type"] for b in m["content"]] == ["text"]
    ev = sse_events(post(stream=True, tool_choice={"type": "none"}))
    assert not tool_uses_of_stream(ev)
    assert post(tool_choice={"type": "auto"}).json()["stop_reason"] == "tool_use"


def test_max_tokens_stop_reason_is_kept(world):
    post = world.start(world.upstream(content=TEXT_CALL, finish="length"))
    m = post().json()
    assert m["stop_reason"] == "max_tokens" and [b["type"] for b in m["content"]] == ["text", "tool_use"]
    ev = sse_events(post(stream=True))
    assert tool_uses_of_stream(ev)
    assert [d for n, d in ev if n == "message_delta"][0]["delta"]["stop_reason"] == "max_tokens"


def test_close_tag_inside_a_json_string_still_parses():
    text = '<tool_call>{"name": "get_weather", "arguments": {"city": "a</tool_call>b"}}</tool_call>tail'
    rest, calls = toolshim.extract(text, {"get_weather"})
    assert calls == [{"name": "get_weather", "input": {"city": "a</tool_call>b"}}] and rest == "tail"
    assert toolshim.extract('<tool_call>{"name": "get_weather", "arguments": {}} junk</tool_call>', {"get_weather"}) is None


def test_delta_without_block_start_passes_through():
    class Inner:
        complete = True

        def feed(self, chunk):
            return []

        def finish(self):
            return [("message_start", {"type": "message_start"}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 3,
                                             "delta": {"type": "text_delta", "text": TEXT_CALL}}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                    ("message_stop", {"type": "message_stop"})]

    out = toolshim.ShimStream(Inner(), {"get_weather"}).finish()
    assert [n for n, _ in out] == ["message_start", "content_block_delta", "message_delta", "message_stop"]


def test_buffer_limit_passes_the_text_through(monkeypatch, world):
    monkeypatch.setattr(toolshim, "MAX_BUFFER", 10)
    ev = sse_events(world.start(world.upstream(content=TEXT_CALL))(stream=True))
    assert_valid_stream(ev)
    text = "".join(d["delta"]["text"] for n, d in ev if n == "content_block_delta")
    assert text == TEXT_CALL and not tool_uses_of_stream(ev)
    assert [d for n, d in ev if n == "message_delta"][0]["delta"]["stop_reason"] == "end_turn"
