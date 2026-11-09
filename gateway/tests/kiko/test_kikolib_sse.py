# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.sse: encoding and the SSE parser."""
import json

from kikolib import sse
from kikolib.sse import Event, Parser, iter_events


def test_encode_dict_compact_json():
    assert sse.encode("message_start", {"type": "x", "n": [1, 2]}) == (
        b'event: message_start\ndata: {"type":"x","n":[1,2]}\n\n')


def test_encode_no_event_and_str_and_unicode():
    assert sse.encode(None, "hello") == b"data: hello\n\n"
    assert sse.encode(None, {"t": "ж"}) == 'data: {"t":"ж"}\n\n'.encode("utf-8")


def test_encode_multiline_str_and_crlf():
    assert sse.encode("e", "a\nb\r\nc") == b"event: e\ndata: a\ndata: b\ndata: c\n\n"
    assert sse.encode(None, "") == b"data: \n\n"


def test_ping_is_anthropic_style():
    out = sse.ping()
    assert out == b'event: ping\ndata: {"type":"ping"}\n\n'
    assert json.loads(out.split(b"data: ")[1]) == {"type": "ping"}


def test_encode_roundtrip_through_parser():
    frames = sse.encode("a", {"x": 1}) + sse.encode(None, "l1\nl2")
    evs = list(iter_events(frames.decode().split("\n")))
    assert evs == [Event("a", '{"x":1}'), Event(None, "l1\nl2")]


def test_parser_basic_and_spacing():
    lines = ["event: msg", "data: one", "", "data:two", "", "data:  three", ""]
    assert list(iter_events(lines)) == [Event("msg", "one"), Event(None, "two"), Event(None, " three")]


def test_comments_and_unknown_fields_ignored():
    lines = [": keepalive", "foo: bar", "retry: 5", "data: x", ": c", ""]
    assert list(iter_events(lines)) == [Event(None, "x")]


def test_multi_data_lines_joined():
    assert list(iter_events(["data: a", "data: b", "data:", "data: c", ""])) == [Event(None, "a\nb\n\nc")]


def test_final_event_without_blank_line():
    assert list(iter_events(["event: e", "data: tail"])) == [Event("e", "tail")]


def test_blank_lines_without_data_dispatch_nothing_and_event_resets():
    lines = ["", "", "event: lonely", "", "data: y", ""]
    assert list(iter_events(lines)) == [Event(None, "y")]


def test_done_marker_passes_through():
    lines = ['data: {"a":1}', "", "data: [DONE]", ""]
    assert [e.data for e in iter_events(lines)] == ['{"a":1}', "[DONE]"]


def test_line_endings_stripped_and_field_without_colon():
    lines = ["data: a\r\n", "data\n", "\n"]
    assert list(iter_events(lines)) == [Event(None, "a\n")]


def test_id_persists_per_spec():
    lines = ["id: 7", "data: a", "", "data: b", "", "id: 9", "data: c", ""]
    assert [e.id for e in iter_events(lines)] == ["7", "7", "9"]


def test_parser_incremental_and_finish():
    p = Parser()
    assert p.feed("data: a") is None
    assert p.feed("") == Event(None, "a")
    assert p.feed("data: b") is None
    assert p.finish() == Event(None, "b")
    assert p.finish() is None
