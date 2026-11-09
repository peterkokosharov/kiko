# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.tomlwrite: edits keep comments and order, refuse what they cannot represent, replace atomically."""
import os
import tomllib

import pytest

from kikolib import tomlwrite as tw

SAMPLE = """# my kiko settings
[kiko]
base_port = 4000   # the base

# proxy section
[proxy]
host = "127.0.0.1"  # loopback
no_auth = false

[logs]
file = "a#b.log"
"""


def test_update_keeps_comments_and_order():
    out = tw.update_text(SAMPLE, {("kiko", "base_port"): 5000, ("proxy", "host"): "0.0.0.0"})
    assert "# my kiko settings" in out and "# proxy section" in out
    assert "base_port = 5000  # the base" in out and 'host = "0.0.0.0"  # loopback' in out
    assert out.index("[kiko]") < out.index("[proxy]") < out.index("[logs]")
    assert tomllib.loads(out)["kiko"]["base_port"] == 5000


def test_comment_char_inside_a_string_is_not_a_comment():
    out = tw.update_text(SAMPLE, {("logs", "file"): "b.log"})
    assert 'file = "b.log"' in out and "a#b" not in out


def test_insert_into_existing_section_and_new_section():
    out = tw.update_text(SAMPLE, {("proxy", "port"): 4100, ("web", "port"): 4101})
    data = tomllib.loads(out)
    assert data["proxy"] == {"host": "127.0.0.1", "no_auth": False, "port": 4100} and data["web"] == {"port": 4101}
    assert out.index("port = 4100") < out.index("[logs]")  # inside [proxy], before the next section
    assert out.rstrip().endswith("port = 4101")


def test_remove_key_and_missing_key_is_noop():
    out = tw.update_text(SAMPLE, {("proxy", "no_auth"): None, ("proxy", "nope"): None, ("zzz", "k"): None})
    assert "no_auth" not in out and "# proxy section" in out
    assert tw.update_text(SAMPLE, {("proxy", "nope"): None}) == SAMPLE


def test_empty_text_and_crlf():
    assert tw.update_text("", {("a", "b"): 1}) == "[a]\nb = 1\n"
    out = tw.update_text("[a]\r\nb = 1\r\n", {("a", "c"): "x"})
    assert out == '[a]\r\nb = 1\r\nc = "x"\r\n'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "a\x00b", "a\x01", {"x": 1}, object(), (1, 2), b"x",
                                 [1, {"a": 1}]])
def test_refuses_values_it_cannot_represent(bad):
    with pytest.raises(tw.TomlWriteError):
        tw.update_text("", {("a", "b"): bad})


def test_value_formats():
    out = tw.update_text("", {("a", "s"): 'q"uote\\ \n tab\t', ("a", "f"): 1.5, ("a", "t"): True, ("a", "l"): [1, "x"]})
    assert tomllib.loads(out)["a"] == {"s": 'q"uote\\ \n tab\t', "f": 1.5, "t": True, "l": [1, "x"]}


@pytest.mark.parametrize("section,key", [("a b", "k"), ("a", "k k"), ("", "k"), ("a", ""), ("a", 'k"')])
def test_refuses_bad_names(section, key):
    with pytest.raises(tw.TomlWriteError):
        tw.update_text("", {(section, key): 1})


def test_refuses_invalid_file_and_multiline_value():
    with pytest.raises(tw.TomlWriteError):
        tw.update_text("this = = bad", {("a", "b"): 1})
    with pytest.raises(tw.TomlWriteError, match="several lines"):
        tw.update_text('[a]\nb = """\nx\n"""\n', {("a", "b"): 1})
    with pytest.raises(tw.TomlWriteError, match="several lines"):
        tw.update_text("[a]\nb = [\n 1,\n 2]\n", {("a", "b"): 1})


def test_refuses_a_key_defined_in_a_form_it_does_not_edit():
    with pytest.raises(tw.TomlWriteError):
        tw.update_text('a = {b = 1}\n', {("a", "b"): 2})


def test_file_is_replaced_atomically(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "kiko.toml"
    tw.set_value(p, "kiko", "base_port", 4500)  # created, with its folder
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"kiko": {"base_port": 4500}}
    p.write_text(SAMPLE, encoding="utf-8")
    before = p.read_bytes()
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: calls.append((a, b)) or real(a, b))
    tw.set_value(p, "kiko", "base_port", 4600)
    assert len(calls) == 1 and calls[0][1] == p and os.path.dirname(calls[0][0]) == str(p.parent)  # temp file beside it
    assert not [f for f in p.parent.iterdir() if f.name.endswith(".tmp")]
    assert p.read_bytes() != before and b"# my kiko settings" in p.read_bytes()


def test_failed_write_leaves_the_old_file_and_no_temp(tmp_path, monkeypatch):
    p = tmp_path / "kiko.toml"
    p.write_text(SAMPLE, encoding="utf-8")
    before = p.read_bytes()
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        tw.set_value(p, "kiko", "base_port", 1)
    assert p.read_bytes() == before and [f.name for f in tmp_path.iterdir()] == ["kiko.toml"]
    with pytest.raises(tw.TomlWriteError):  # a refusal never touches the file either
        tw.set_value(p, "kiko", "base_port", float("nan"))
    assert p.read_bytes() == before


def test_remove_key_file(tmp_path):
    p = tmp_path / "kiko.toml"
    p.write_text(SAMPLE, encoding="utf-8")
    tw.remove_key(p, "proxy", "host")
    assert "host" not in tomllib.loads(p.read_text(encoding="utf-8"))["proxy"]
