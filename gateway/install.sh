#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
set -e
command -v python3.12 >/dev/null 2>&1 || { echo "ERROR: python3.12 not found."; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
pip install "$SCRIPT_DIR"/kiko-*.whl --quiet
kiko --config ~/.kircho/kiko-config.yaml --init-db
mkdir -p ~/.config/systemd/user
cp "$SCRIPT_DIR/kiko.service" ~/.config/systemd/user/kiko.service
systemctl --user daemon-reload
systemctl --user enable kiko
systemctl --user start kiko
echo "KIKO running. Status: systemctl --user status kiko"
