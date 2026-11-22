#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build kiko-tray-gtk3_<version>_amd64.deb — the native GTK3 tray for Mint/Ubuntu.
# Usage: bash packaging/build-tray-gtk3-deb.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="${KIKO_TRAY_VERSION:-0.1.0}"
PKG="kiko-tray-gtk3_${VER}_amd64"
OUT="$ROOT/dist/kiko-tray-gtk3_${VER}_amd64.deb"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
D="$STAGE/$PKG"

# Prefer the system GTK3; fall back to the local toolchain used on this dev box.
if ! pkg-config --exists gtk+-3.0 2>/dev/null; then
  R="${KIKO_DEPS_ROOT:-/tmp/opencode/mint-deps/root}"
  if [ ! -d "$R" ]; then
    echo "missing GTK3: install libgtk-3-dev (or set KIKO_DEPS_ROOT)" >&2
    exit 1
  fi
  export PATH="$R/usr/bin:$PATH"
  export LD_LIBRARY_PATH="$R/usr/lib/x86_64-linux-gnu:$R/lib/x86_64-linux-gnu"
  export PKG_CONFIG_PATH="$R/usr/lib/x86_64-linux-gnu/pkgconfig:$R/usr/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  export PKG_CONFIG_SYSROOT_DIR="$R"
fi

command -v cmake >/dev/null 2>&1 || { echo "cmake not found" >&2; exit 1; }
bash "$ROOT/tray/tools/build-mint.sh"   # -DKT_GUI=gtk -DCMAKE_SKIP_RPATH=ON, plus ctest 6/6
BIN="$ROOT/build/mint/kiko-tray"
[ -x "$BIN" ] || { echo "tray binary missing: $BIN" >&2; exit 1; }

mkdir -p "$D/DEBIAN" "$D/usr/bin" "$D/etc/xdg/autostart" \
         "$D/usr/share/icons/hicolor/22x22/apps" "$D/usr/share/doc/kiko-tray-gtk3"
mkdir -p "$ROOT/dist"

cp "$BIN" "$D/usr/bin/kiko-tray"
chmod 755 "$D/usr/bin/kiko-tray"
cp "$ROOT"/tray/tqt3/icons/Kiko_*.png "$D/usr/share/icons/hicolor/22x22/apps/"

cat > "$D/etc/xdg/autostart/kiko-tray.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=kiko tray
Comment=Control the local kiko gateway from the system tray
Exec=/usr/bin/kiko-tray
Icon=Kiko_running
Categories=Utility;
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF

cat > "$D/usr/share/doc/kiko-tray-gtk3/README.md" <<EOF
# kiko-tray-gtk3 $VER

Native GTK3 status icon for the kiko gateway on Linux Mint / Ubuntu / XFCE.
Built from \`tray/gtk3/\` with \`-DKT_GUI=gtk\`; cJSON 1.7.18 is vendored, so no
network access is needed at build time. The 6 CTest core tests pass.

* Binary: \`/usr/bin/kiko-tray\`
* Autostart: \`/etc/xdg/autostart/kiko-tray.desktop\`
* Reads \`KIKO_PORT\` (default 4000) and the management token from
  \`~/.kiko/api-token\` (or \`KIKO_API_TOKEN\`).
* Needs the gateway: install \`kiko\` and run \`kiko install\`.
EOF

cat > "$D/DEBIAN/control" <<EOF
Package: kiko-tray-gtk3
Version: $VER
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Peter Kokosharov <peter.kokosharov@gmail.com>
Depends: libc6 (>= 2.34), libgtk-3-0 (>= 3.24), libgdk-pixbuf-2.0-0, libglib2.0-0
Recommends: kiko
Description: native GTK3 system tray for the kiko gateway
 Status icon with start/stop/restart and model status for Linux Mint,
 Ubuntu and other GTK3 desktops. Uses GtkStatusIcon (or XApp when present).
 No RPATH: links only against the distribution's GTK3 libraries.
EOF

fakeroot dpkg-deb --build "$D" "$OUT" >/dev/null
echo "Built: $OUT"
dpkg-deb -I "$OUT" | head -20
