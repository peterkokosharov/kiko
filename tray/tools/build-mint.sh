#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Linux Mint 22.3 Cinnamon / GTK3. Run from anywhere in the repo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
B="$ROOT/build/mint"
cmake -S "$ROOT/tray" -B "$B" -DKT_GUI=gtk -DKT_FETCH_CJSON=OFF -DCMAKE_BUILD_TYPE=Release "$@"
cmake --build "$B" -j
ctest --test-dir "$B" --output-on-failure
echo "binary: $B/kiko-tray"
