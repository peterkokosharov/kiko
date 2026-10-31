#!/bin/sh
# SPDX-License-Identifier: MIT
# Run kiko-lite tests with stdlib Python (no pytest, no deps)
# Usage: ./run_tests.sh [test_module]

set -eu

# kiko package is at /home/petark/kiko/kiko
# Its parent /home/petark/kiko must be in PYTHONPATH for "from kiko import ..." to work
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PARENT_DIR="$(dirname "$ROOT_DIR")"
export PYTHONPATH="$PARENT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Run from the package root
cd "$ROOT_DIR"

if [ $# -eq 0 ]; then
    python3 -m unittest discover -s tests -v
else
    python3 -m unittest "$1" -v
fi