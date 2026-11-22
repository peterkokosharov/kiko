#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# macOS (Cocoa). Needs Xcode command line tools + CMake. Run from anywhere in the repo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
B="$ROOT/build/macos"
cmake -S "$ROOT/tray" -B "$B" -DKT_GUI=mac -DKT_FETCH_CJSON=OFF -DCMAKE_BUILD_TYPE=Release "$@"
cmake --build "$B" -j
ctest --test-dir "$B" --output-on-failure
echo "app: $B/kiko-tray.app"
