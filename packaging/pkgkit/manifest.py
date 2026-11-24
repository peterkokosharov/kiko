# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""SHA256 manifests (standard library only).

    python manifest.py write [DIR]   # (re)write DIR/SHA256SUMS
    python manifest.py check [DIR]   # exit 1 if any file differs, is missing or is extra

DIR defaults to the folder of this script (pkgkit checks itself, like kikolib). Format: "<sha256>  <relative/posix/path>"
per line, sorted by path, LF endings. __pycache__ and the manifest file itself are excluded. Hashes are over the
bytes on disk, so the tree must be LF (consumers add `eol=lf` to .gitattributes).
The same functions check a package payload against _pkg/MANIFEST (see install.py).
"""
import hashlib
import sys
from pathlib import Path

NAME = "SHA256SUMS"


def compute(pkg: Path, skip: tuple = (NAME,)) -> dict:
    out = {}
    for p in sorted(Path(pkg).rglob("*")):
        rel = p.relative_to(pkg)
        if not p.is_file() or "__pycache__" in rel.parts or rel.as_posix() in skip:
            continue
        out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def render(sums: dict) -> bytes:
    return "".join(f"{h}  {n}\n" for n, h in sorted(sums.items())).encode("utf-8")


def parse(path: Path) -> dict:
    want = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        h, _, n = line.partition("  ")
        if n:
            want[n] = h
    return want


def check(pkg: Path, manifest: "Path | None" = None, skip: tuple = (NAME,)) -> list:
    """Problems found (empty list = the tree matches the manifest)."""
    pkg = Path(pkg)
    mf = Path(manifest) if manifest else pkg / NAME
    if not mf.is_file():
        return [f"{mf.name} missing"]
    want, have = parse(mf), compute(pkg, skip)
    problems = [f"missing: {n}" for n in sorted(want.keys() - have.keys())]
    problems += [f"not in manifest: {n}" for n in sorted(have.keys() - want.keys())]
    problems += [f"changed: {n}" for n in sorted(want.keys() & have.keys()) if want[n] != have[n]]
    return problems


def main(argv: list) -> int:
    if not argv or argv[0] not in ("write", "check"):
        print(__doc__)
        return 2
    pkg = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent
    if argv[0] == "write":
        (pkg / NAME).write_bytes(render(compute(pkg)))
        print(f"wrote {pkg / NAME}")
        return 0
    problems = check(pkg)
    for p in problems:
        print(p)
    print("OK" if not problems else "MANIFEST MISMATCH: fix it in the kiko-src repo, bump the version, re-write, re-copy")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
