#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
# Install kiko-lite as a systemd USER service (Linux Mint, Q4OS/TDE, any systemd distribution).
#   ./install-service.sh              install, or update and restart if already installed
#   ./install-service.sh --uninstall  stop, disable and remove
# Run it from the unpacked kiko-lite folder; the folder may be anywhere. After unpacking a new zip or moving the
# folder, run this again: it rewrites the unit and restarts the service onto the new code and path.
set -e

NAME=kiko-lite
mode=install
case "${1:-}" in
    "") [ $# -eq 0 ] || mode=bad ;;
    --uninstall) [ $# -eq 1 ] || mode=bad; [ "$mode" = bad ] || mode=uninstall ;;
    *) mode=bad ;;
esac
if [ "$mode" = bad ]; then
    echo "usage: $0 [--uninstall]" >&2
    exit 2
fi

me="$0"
if real=$(readlink -f "$me" 2>/dev/null) && [ -n "$real" ]; then me="$real"; fi
here=$(CDPATH= cd -- "$(dirname -- "$me")" && pwd)
case "$here" in
    *'
'*) echo "The folder name contains a newline; move kiko-lite to a normal folder and run again." >&2; exit 1 ;;
esac
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/$NAME.service"
# Every path and the port come from kiko's own config (kikolib.config: $KIKO_CONFIG_DIR or the default folder,
# kiko.toml): ask the launcher, do not guess here.
cfg() { "$here/$NAME" config --get "$1" 2>/dev/null || true; }

no_systemd() {
    if [ "$mode" = uninstall ]; then
        echo "systemctl --user is not available here, so there is no user service to remove." >&2
        echo "If you started kiko-lite by hand, stop it with Ctrl+C in its terminal." >&2
    else
        echo "systemctl --user is not available here. Start kiko-lite by hand instead:" >&2
        echo "  $here/kiko-lite serve --no-auth     (docs/LINUX-INSTALL.md, step 6)" >&2
    fi
    exit 1
}
command -v systemctl >/dev/null 2>&1 || no_systemd
systemctl --user show-environment >/dev/null 2>&1 || no_systemd

if [ "$mode" = uninstall ]; then
    systemctl --user disable --now "$NAME" 2>/dev/null || true
    rm -f "$unit"
    systemctl --user daemon-reload
    data_dir=$(cfg config_dir)
    echo "$NAME service removed. Your data${data_dir:+ in $data_dir} was not touched."
    exit 0
fi

if [ ! -f "$here/$NAME.service" ]; then
    echo "$NAME.service not found next to this script ($here)" >&2
    exit 1
fi
if [ ! -f "$here/$NAME" ]; then
    echo "launcher $here/$NAME not found" >&2
    exit 1
fi
[ -x "$here/$NAME" ] || chmod +x "$here/$NAME"

data_dir=$(cfg config_dir)
env_file=$(cfg env_path)
db_file=$(cfg database_path)
toml_file=$(cfg toml_path)
host=$(cfg proxy_host)
port=$(cfg proxy_port)
if [ -z "$data_dir" ] || [ -z "$env_file" ] || [ -z "$port" ]; then
    echo "cannot read the configuration: '$here/$NAME config --get config_dir' printed nothing" >&2
    exit 1
fi

# .env template: key NAMES only, you add the values. Never overwritten.
mkdir -p "$data_dir"
chmod 700 "$data_dir"
if [ ! -f "$env_file" ]; then
    (umask 077; printf '%s\n' \
        '# API keys for kiko-lite: NAME=value, one per line, no quotes. Lines starting with # are ignored.' \
        '# The names must match the --api-key-env names of your models (see add-model).' \
        'GEMINI_API_KEY=' \
        'NVIDIA_API_KEY=' > "$env_file")
    echo "Created $env_file (mode 600): put your API keys in it."
fi
chmod 600 "$env_file"

# Write the real folder into the unit. Unit text: % -> %%; inside the quoted ExecStart also \ " $ are escaped.
# The second sed protects \ & | for the sed replacement text below.
plain=$(printf '%s' "$here" | sed -e 's/%/%%/g' | sed -e 's/[\\&|]/\\&/g')
quoted=$(printf '%s' "$here" | sed -e 's/%/%%/g' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\$/$$/g' | sed -e 's/[\\&|]/\\&/g')
# A non-default config folder (env KIKO_CONFIG_DIR at install time) is written into the unit, so the service uses
# the same folder, kiko.toml and ports as the commands you run by hand. Without it the line stays a comment.
cfg_line='# @KIKO_CONFIG_ENV@'
if [ -n "${KIKO_CONFIG_DIR:-}" ]; then
    case "$KIKO_CONFIG_DIR" in
        *'
'*) echo "KIKO_CONFIG_DIR contains a newline" >&2; exit 1 ;;
    esac
    cfg_q=$(printf '%s' "$KIKO_CONFIG_DIR" | sed -e 's/%/%%/g' -e 's/\\/\\\\/g' -e 's/"/\\"/g' | sed -e 's/[\\&|]/\\&/g')
    cfg_line="Environment=\"KIKO_CONFIG_DIR=$cfg_q\""
fi
mkdir -p "$unit_dir"
sed -e "s|@KIKO_DIR_Q@|$quoted|g" -e "s|@KIKO_DIR@|$plain|g" -e "s|^# @KIKO_CONFIG_ENV@\$|$cfg_line|" "$here/$NAME.service" > "$unit"

systemctl --user daemon-reload
systemctl --user enable "$NAME"
systemctl --user restart "$NAME"  # also on a re-run: picks up the new code, unit and path
systemctl --user status "$NAME" --no-pager || true

echo
echo "Unit:   $unit"
echo "Keys:   $env_file"
echo "Data:   $data_dir (database $db_file)"
echo "Serves: http://$host:$port  (settings: $toml_file; clients: ANTHROPIC_BASE_URL=http://$host:$port)"
echo "Logs:   journalctl --user -u $NAME -f"
echo "Stop:   systemctl --user stop $NAME     Remove: $0 --uninstall"
echo
echo "Optional: to start at boot without logging in, run once:  loginctl enable-linger \"\$USER\""
