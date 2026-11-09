#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet, Anthropic Opus
"""Post-day verification: checks the kiko SQLite DB after a real day of use.

Usage:
    python tools/kiko_verify_day.py [--db PATH]     (default: the database kiko_lite itself uses)
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kiko_lite.db import default_path  # noqa: E402

MAX_DB_MB = 50


def check(db_path: Path) -> list:
    failures = []

    if not db_path.exists():
        return [f"DB not found: {db_path}"]

    size_mb = db_path.stat().st_size / 1_048_576
    print(f"  DB size      : {size_mb:.1f} MB")
    if size_mb > MAX_DB_MB:
        failures.append(f"DB too large: {size_mb:.1f} MB (limit {MAX_DB_MB} MB)")

    con = sqlite3.connect(str(db_path))
    try:
        try:
            count = con.execute("SELECT COUNT(*) FROM billing").fetchone()[0]
        except sqlite3.OperationalError as e:
            failures.append(f"billing table missing or unreadable: {e}")
            return failures

        print(f"  billing rows : {count}")
        if count == 0:
            failures.append("billing table is empty — no requests logged")
            return failures

        # Top models by request count
        rows = con.execute(
            "SELECT model, COUNT(*) AS n FROM billing "
            "GROUP BY model ORDER BY n DESC LIMIT 10"
        ).fetchall()
        print("  top models   :")
        for model, n in rows:
            print(f"    {n:>6}  {model}")

        # Total cost
        try:
            total = con.execute("SELECT SUM(cost_usd) FROM billing").fetchone()[0] or 0.0
            print(f"  total cost   : ${total:.6f}")
        except sqlite3.OperationalError:
            pass

    finally:
        con.close()

    return failures


def main():
    parser = argparse.ArgumentParser(description="Verify kiko DB after a day of use")
    parser.add_argument("--db", default=None, help=f"database file (default {default_path()})")
    args = parser.parse_args()

    db = default_path(args.db).expanduser()
    print(f"Checking {db} …")
    failures = check(db)

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  • {f}")
        sys.exit(1)
    else:
        print("\nOK — DB looks healthy.")


if __name__ == "__main__":
    main()
