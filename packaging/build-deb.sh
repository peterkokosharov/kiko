#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a Debian/Ubuntu/Mint .deb for the stdlib-only kiko-lite gateway.
# Usage: bash packaging/build-deb.sh
# Output: dist/kiko_<version>_all.deb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="${KIKO_DEB_VERSION:-0.3.0}"
PKG="kiko_${VER}_all"
OUT="$ROOT/dist/kiko_${VER}_all.deb"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/$PKG"

mkdir -p "$D/DEBIAN" "$D/opt/kiko/gateway" "$D/opt/kiko/tray/tools" \
         "$D/usr/bin" "$D/usr/share/doc/kiko"
mkdir -p "$ROOT/dist"

cat > "$D/DEBIAN/control" <<EOF
Package: kiko
Version: $VER
Section: utils
Priority: optional
Architecture: all
Maintainer: Peter Kokosharov <peter.kokosharov@gmail.com>
Depends: python3 (>= 3.10), libsecret-tools, curl
Description: kiko-lite local AI gateway with an Anthropic-compatible API
 Runs on 127.0.0.1 and resolves local model aliases to free provider tiers.
 Provider keys stay in the desktop keyring; use 'kiko install' as your
 normal user to set up keys, models and the systemd user service.
EOF

cat > "$D/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
echo "kiko installed. As your normal user, run: kiko install"
EOF
chmod 755 "$D/DEBIAN/postinst"

cp -a "$ROOT/gateway/kikolib" "$D/opt/kiko/gateway/"
cp -a "$ROOT/gateway/kiko_lite" "$D/opt/kiko/gateway/"
cp -a "$ROOT/tray/tools/." "$D/opt/kiko/tray/tools/"
find "$D/opt/kiko" -type d -name __pycache__ -prune -exec rm -rf {} +

cat > "$D/usr/bin/kiko" <<'EOF'
#!/usr/bin/env bash
# Installed by the kiko .deb. Lifecycle lives in /opt/kiko/tray/tools/kiko.
export KIKO_ROOT=/opt/kiko
export KIKO_SYSTEM=1
exec /opt/kiko/tray/tools/kiko "$@"
EOF
chmod 755 "$D/usr/bin/kiko"

cp "$ROOT/README.md" "$D/usr/share/doc/kiko/README.md"
[ -f "$ROOT/docs/MODELS.md" ] && cp "$ROOT/docs/MODELS.md" "$D/usr/share/doc/kiko/MODELS.md"
[ -f "$ROOT/docs/INSTALL.md" ] && cp "$ROOT/docs/INSTALL.md" "$D/usr/share/doc/kiko/INSTALL.md"

fakeroot dpkg-deb --build "$D" "$OUT" >/dev/null
echo "Built: $OUT"
dpkg-deb -I "$OUT" | head -20
