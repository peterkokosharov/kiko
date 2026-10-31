#!/bin/sh
# SPDX-License-Identifier: MIT
# run_all_tests.sh - Run all 3 tiers of tests
# Usage: ./run_all_tests.sh [tier]
#   tier: "kikolib", "kiko_lite", "web-ui", or "all" (default)

set -eu

# Resolve root directory
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$ROOT_DIR")"

run_kiko_lite() {
    echo "============================================"
    echo "Tier 2: kiko_lite (proxy core) - 237 tests"
    echo "============================================"
    cd "$ROOT_DIR"
    export PYTHONPATH="$PARENT_DIR:$ROOT_DIR/kikolib${PYTHONPATH:+:$PYTHONPATH}"
    python3 -m unittest discover -s tests -v
}

run_web_ui() {
    echo "============================================"
    echo "Tier 3: web-ui (admin UI) - 16 tests"
    echo "============================================"
    # Check if server is running
    if ! curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4008/ 2>/dev/null | grep -q "200"; then
        echo "WARNING: web-ui server not running on port 4008"
        echo "Start it with: $ROOT_DIR/start-web.sh &"
        echo "Skipping web-ui tests..."
        return 0
    fi
    cd "$ROOT_DIR/web-ui/static/tests"
    node --test api.test.js
}

run_kikolib() {
    echo "============================================"
    echo "Tier 1: kikolib (core library)"
    echo "============================================"
    echo "kikolib tests are integrated into kiko_lite tests"
    echo "via the keystore module tests (54 tests in test_keystore.py)"
    echo "Run: ./run_all_tests.sh kiko_lite"
}

case "${1:-all}" in
    kikolib)
        run_kikolib
        ;;
    kiko_lite)
        run_kiko_lite
        ;;
    web-ui)
        run_web_ui
        ;;
    all)
        run_kiko_lite
        echo ""
        run_web_ui
        ;;
    *)
        echo "Usage: $0 [kikolib|kiko_lite|web-ui|all]"
        exit 1
        ;;
esac