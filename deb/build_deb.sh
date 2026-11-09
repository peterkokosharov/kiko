#!/bin/bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp ../kiko.service "$SCRIPT_DIR/kiko_0.2.0_amd64/usr/lib/systemd/user/kiko.service"
# Copy wheel into data area
mkdir -p "$SCRIPT_DIR/kiko_0.2.0_amd64/usr/share/kiko"
cp ../releases/kiko-0.2.0-py3-none-any.whl "$SCRIPT_DIR/kiko_0.2.0_amd64/usr/share/kiko/"
# Compress changelog
gzip -9 -c ../CHANGELOG.md > "$SCRIPT_DIR/kiko_0.2.0_amd64/usr/share/doc/kiko/changelog.Debian.gz"
# Fix permissions
chmod 755 "$SCRIPT_DIR/kiko_0.2.0_amd64/DEBIAN/postinst"
# Build
dpkg-deb --build "$SCRIPT_DIR/kiko_0.2.0_amd64"
echo "Built: kiko_0.2.0_amd64.deb"
