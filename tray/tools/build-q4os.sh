#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Q4OS 6 / Trinity (TQt3 kicker applet). Separate CMake project.
# Pass extra defines if SQLite headers are not system-wide, e.g.
#   -DSQLITE3_LIB=/tmp/kiko-deps/usr/lib/x86_64-linux-gnu/libsqlite3.so
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
B="$ROOT/build/q4os"
cmake -S "$ROOT/tray/tqt3" -B "$B" -DCMAKE_BUILD_TYPE=Release "$@"
cmake --build "$B" -j
if [ -f "$B/CTestTestfile.cmake" ]; then
  ctest --test-dir "$B" --output-on-failure
fi
echo "applet: $B/kiko_applet.so"
