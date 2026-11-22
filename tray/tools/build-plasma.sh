#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# KDE Plasma / Qt6 (falls back to Qt5 >= 5.15). Run from anywhere in the repo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
B="$ROOT/build/plasma"
cmake -S "$ROOT/tray" -B "$B" -DKT_GUI=qt -DKT_FETCH_CJSON=OFF -DCMAKE_BUILD_TYPE=Release "$@"
cmake --build "$B" -j
ctest --test-dir "$B" --output-on-failure
echo "binary: $B/kiko-tray"
