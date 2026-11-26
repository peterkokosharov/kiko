# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Put the models of a catalog XML file into the kiko database: ``python tools/inject.py FILE.xml [--dry-run] [--db PATH]``.

The same as ``kiko-lite import-models --xml FILE.xml``: the file is the one ``kiko-lite export-models --xml`` writes
(schema docs/schema/kiko-models.xsd). Models are upserted by alias in ONE transaction after the whole file checked
out (every problem is listed, nothing is written on any); an existing database is backed up first; a new model stays
disabled unless the file says enabled="true" and it answers a ping through the running kiko. Only key NAMES are
accepted. ``--dry-run`` shows added / changed / unchanged and writes nothing. Standard library only.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("gateway", ""):  # the installed kiko-lite keeps kiko_lite next to this script's parent
    path = str(ROOT / sub) if sub else str(ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="inject.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("xml", metavar="path_to_xml_file")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--db", metavar="PATH", help="database file (default: the kiko default)")
    args = ap.parse_args(argv)
    from kiko_lite import cli
    cmd = ["import-models", "--xml", args.xml] + (["--dry-run"] if args.dry_run else [])
    if args.db:
        cmd += ["--db", args.db]
    return cli.main(cmd)


if __name__ == "__main__":
    sys.exit(main())
