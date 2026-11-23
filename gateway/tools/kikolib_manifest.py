# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Release manifest for kikolib (standard library only).

    python kikolib_manifest.py write [PKG_DIR]   # (re)write PKG_DIR/SHA256SUMS
    python kikolib_manifest.py check [PKG_DIR]   # exit 1 if any file differs, is missing or is extra

PKG_DIR defaults to ../kikolib next to this script. Format: "<sha256>  <relative/posix/path>" per line,
sorted by path, LF endings. __pycache__ and SHA256SUMS itself are excluded. Hashes are over the bytes on
disk, so the tree must be LF (.gitattributes forces eol=lf for gateway/kikolib).
"""
import hashlib
import sys
from pathlib import Path

NAME = "SHA256SUMS"


def compute(pkg: Path) -> dict[str, str]:
    out = {}
    for p in sorted(pkg.rglob("*")):
        rel = p.relative_to(pkg)
        if not p.is_file() or "__pycache__" in rel.parts or rel.as_posix() == NAME:
            continue
        out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def render(sums: dict[str, str]) -> bytes:
    return "".join(f"{h}  {n}\n" for n, h in sorted(sums.items())).encode("utf-8")


def check(pkg: Path) -> list[str]:
    """Return a list of problems (empty = manifest matches)."""
    mf = pkg / NAME
    if not mf.is_file():
        return [f"{NAME} missing"]
    want = {}
    for line in mf.read_text(encoding="utf-8").splitlines():
        h, _, n = line.partition("  ")
        want[n] = h
    have = compute(pkg)
    problems = [f"missing: {n}" for n in sorted(want.keys() - have.keys())]
    problems += [f"not in manifest: {n}" for n in sorted(have.keys() - want.keys())]
    problems += [f"changed: {n}" for n in sorted(want.keys() & have.keys()) if want[n] != have[n]]
    return problems


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("write", "check"):
        print(__doc__)
        return 2
    pkg = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "kikolib"
    if argv[0] == "write":
        (pkg / NAME).write_bytes(render(compute(pkg)))
        print(f"wrote {pkg / NAME}")
        return 0
    problems = check(pkg)
    for p in problems:
        print(p)
    print("OK" if not problems else "MANIFEST MISMATCH: fix in the kiko repo, bump version, re-write, release")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
