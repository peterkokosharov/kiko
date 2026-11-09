# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""kikolib.yaml_lite: compare with PyYAML (test-only dependency) and reject unsupported YAML."""
from pathlib import Path

import pytest

from kikolib import yaml_lite
from kikolib.yaml_lite import YamlError

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["kiko-config.yaml", "kiko-sample.yaml"])
def test_repo_files_match_pyyaml(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert yaml_lite.load(text) == yaml.safe_load(text)
    assert yaml_lite.load_file(ROOT / name) == yaml.safe_load(text)


SAMPLES = [
    "",
    "# only comment\n",
    "---\na: 1\nb: 2.5\nc: true\nd: null\ne: ~\nf:\ng: 'it''s'\nh: \"x\\ty\\u00e9\"\n",
    "a: [1, two, 'three', [4, 5]]\nb: {x: 1, y: [a, b], z: }\n",
    "- 1\n- a: 1\n  b: 2\n- - x\n  - y\n-\n  k: v\n- \n",
    "k:\n- a\n- b\nm:\n  - c\n",
    "a:\n  b:\n    c: 1  # trailing\n  d: 'x # not comment'\n",
    "a: 0x1F\nb: 0755\nc: 1_000\nd: .5\ne: 1.5e+3\nf: 1e3\ng: .inf\nh: yes\ni: Off\nj: 007x\n",
    "url: https://a.b/c:d\nq: \"a: b\"\n'quoted key': 1\n\"d q\": 2\n3: three\n",
    "a: b#c\nd: don't # x\n",
    "scalar\n",
    "- a\n- b\n",
    "a:\r\n  b: 1\r\n",
    "a: -1\nb: +2\nc: -.5\n",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_matches_pyyaml(text):
    assert yaml_lite.load(text) == yaml.safe_load(text)


@pytest.mark.parametrize("text,line", [
    ("a: &x 1\n", 1),
    ("a: *x\n", 1),
    ("a: !!str 1\n", 1),
    ("a: |\n  text\n", 1),
    ("a: >\n  text\n", 1),
    ("a: 1\n---\nb: 2\n", 2),
    ("a: 1\n...\n", 2),
    ("? a\n: b\n", 1),
    ("a: 1\n\tb: 2\n", 2),
    ("a:\n\tb: 2\n", 2),
    ("a: [1, 2\n", 1),
    ("a: 'open\n", 1),
    ("a: 1\n  b: 2\n", 2),
    ("a: b: c\n", 1),
    ("a: 1\nnokey\n", 2),
])
def test_unsupported_raises_with_line(text, line):
    with pytest.raises(YamlError) as ei:
        yaml_lite.load(text)
    assert ei.value.line == line
    assert f"line {line}" in str(ei.value)
