#!/bin/sh
# start-web.sh - Launch kiko-lite web-ui from anywhere
# Usage: chmod +x start-web.sh && ./start-web.sh
# Or: exec ~/kiko_lite/start-web.sh
# Or add alias: alias kiko-web='~/kiko_lite/start-web.sh'
#
# Run from ANY directory - it finds its own location.
# Config is loaded from ~/.kiko/kiko.toml (webui.host, webui.port)

me="$0"
if real=$(readlink -f "$me" 2>/dev/null) && [ -n "$real" ]; then me="$real"; fi
here=$(CDPATH= cd -- "$(dirname -- "$me")" && pwd)

# Load .env if exists (for API keys)
if [ -f "/home/petark/.kiko/.env" ]; then
    export $(grep -v '^#' /home/petark/.kiko/.env | xargs) 2>/dev/null || true
fi

# Must run from parent of kiko_lite (where kiko_lite is importable)
# with PYTHONPATH including kiko_lite and kikolib
PARENT=$(dirname "$here")
exec env PYTHONPATH="$PARENT:$here/kikolib${PYTHONPATH:+:$PYTHONPATH}" python3 -m kiko_lite.web-ui.main "$@"