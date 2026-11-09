# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.dotenv: parse and load."""

from kikolib import dotenv


def test_parse_basic():
    text = (
        "# comment\n\nA=1\nexport B=two words\nC = spaced  \n"
        "D=val # inline\nE=a#b\nF=\nG='lit #x \\n'\nH=\"a\\nb\\t\\\"q\\\" \\\\ #x\" # c\n"
        "I='x' # c\n"
    )
    assert dotenv.parse(text) == {
        "A": "1", "B": "two words", "C": "spaced", "D": "val", "E": "a#b", "F": "",
        "G": "lit #x \\n", "H": 'a\nb\t"q" \\ #x', "I": "x",
    }


def test_no_interpolation_and_invalid_lines_skipped():
    text = "A=1\nB=$A ${A}\nnoequals\n=x\n1BAD=2\nC='unterminated\nD=\"unterminated\nE='x' junk\nOK=y\n"
    assert dotenv.parse(text) == {"A": "1", "B": "$A ${A}", "OK": "y"}


# ---- WatchedFile: reload on mtime change ----

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _bump(path, text):
    """Write text and force a different mtime (some filesystems have a coarse clock)."""
    import os
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))


def test_watched_file_reloads_when_the_file_changes(tmp_path):
    f, clock = tmp_path / ".env", _Clock()
    w = dotenv.WatchedFile(f, ttl=2.0, clock=clock)
    assert w.get("A") is None and w.version() == 0  # missing file: no values
    f.write_text("A=1\nB=2\n", encoding="utf-8")
    assert w.get("A") is None  # not re-read before the ttl is over
    clock.t = 2.5
    assert w.get("A") == "1" and w.get("B") == "2" and w.version() == 1
    _bump(f, "A=changed\n")  # replaced and removed
    clock.t = 3.0
    assert w.get("A") == "1"  # still inside the ttl
    clock.t = 5.0
    assert w.get("A") == "changed" and w.get("B") is None and w.version() == 2
    clock.t = 8.0
    assert w.version() == 2  # unchanged file: same version


def test_watched_file_deleted_file_clears_values_and_never_touches_environ(tmp_path, monkeypatch):
    f, clock = tmp_path / ".env", _Clock()
    monkeypatch.delenv("WF_ONLY_IN_FILE", raising=False)
    f.write_text("WF_ONLY_IN_FILE=x\n", encoding="utf-8")
    w = dotenv.WatchedFile(f, ttl=1.0, clock=clock)
    assert w.get("WF_ONLY_IN_FILE") == "x"
    import os
    assert "WF_ONLY_IN_FILE" not in os.environ  # values live in the object only
    f.unlink()
    clock.t = 5.0
    assert w.get("WF_ONLY_IN_FILE") is None


def test_watched_file_unreadable_content_keeps_last_good_values(tmp_path):
    f, clock = tmp_path / ".env", _Clock()
    f.write_text("A=1\n", encoding="utf-8")
    w = dotenv.WatchedFile(f, ttl=1.0, clock=clock)
    assert w.get("A") == "1"
    f.write_bytes(b"A=\xff\xfe\x00bad\n")  # not valid UTF-8
    clock.t = 5.0
    assert w.get("A") == "1"
