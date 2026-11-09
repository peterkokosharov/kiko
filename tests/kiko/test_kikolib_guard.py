# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Guards: kikolib parses as Python 3.11, uses no asyncio (except http/server stubs), has headers."""
import ast
from pathlib import Path

import kikolib

KIKOLIB = Path(__file__).resolve().parents[2] / "kikolib"
FILES = sorted(KIKOLIB.glob("*.py"))
HEADER = (
    "# SPDX-License-Identifier: MIT\n"
    "# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>\n"
    "# LLM: "
)


def test_files_found_and_removed_modules_gone():
    names = {f.name for f in FILES}
    assert {"factory.py", "keystore.py", "posix.py", "yaml_lite.py"} <= names
    assert not ({"crypto.py", "jsonschema_lite.py"} & names)


def test_all_parse_as_python_311():
    for f in FILES:
        ast.parse(f.read_text(encoding="utf-8"), filename=str(f), feature_version=(3, 11))


def test_no_asyncio_outside_http_and_server():
    for f in FILES:
        if f.name in ("http.py", "server.py"):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] != "asyncio" for a in node.names), f.name
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "asyncio", f.name


def test_headers_and_no_vendor_name():
    for f in FILES:
        text = f.read_text(encoding="utf-8")
        assert text.startswith(HEADER), f.name


def test_package_metadata_and_all_modules_import():
    import importlib

    assert kikolib.__version__ == "0.1.0"
    for name in kikolib.__all__:
        importlib.import_module(f"kikolib.{name}")
