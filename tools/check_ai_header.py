# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Fail if a file with an author header has no attribution line.

Author header: "Author: Peter Kokosharov" in any file, or "Copyright ... Kokosharov" in a code file.
Attribution line: "AI models:" or the older "LLM:" convention, within the first lines of the file.

Usage: python -I tools/check_ai_header.py [repo-root]   (stdlib only; exit 1 on a missing line)
"""
import subprocess
import sys
from pathlib import Path

SKIP = ("vendor/", "third_party/", "tvision/", "node_modules/")
CODE_EXT = {".py", ".sh", ".bash", ".c", ".h", ".cpp", ".hpp", ".m", ".js", ".css", ".html", ".cmd", ".bat"}
HEAD_LINES = 20


def tracked(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, check=True).stdout
        return out.decode("utf-8", "replace").splitlines()
    except (OSError, subprocess.CalledProcessError):
        return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def missing(root: Path) -> list[str]:
    bad = []
    for rel in tracked(root):
        if any(s in rel for s in SKIP):
            continue
        try:
            with open(root / rel, "rb") as fh:
                head = [fh.readline() for _ in range(HEAD_LINES)]
        except OSError:
            continue
        text = b"".join(head).decode("utf-8", "replace")
        has_header = "Author: Peter Kokosharov" in text or (
            Path(rel).suffix.lower() in CODE_EXT and "Copyright" in text and "Kokosharov" in text
        )
        if has_header and "AI models:" not in text and "LLM:" not in text:
            bad.append(rel)
    return bad


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    bad = missing(root)
    for rel in bad:
        print("missing attribution line:", rel)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
