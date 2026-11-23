# kiko

Personal AI gateway and system tray front-end.

## Install

```bash
git clone https://github.com/peterkokosharov/kiko.git ~/kiko
cd ~/kiko && bash tray/tools/kiko install
kiko doctor
```

No sudo for the install itself; keys go into the desktop keyring. For `.deb` packages:

```bash
bash packaging/build-deb.sh           # gateway -> kiko_0.3.0_all.deb
bash packaging/build-tray-gtk3-deb.sh # GTK3 tray -> kiko-tray-gtk3_0.1.0_amd64.deb
sudo apt install -y ./dist/kiko_0.3.0_all.deb ./dist/kiko-tray-gtk3_0.1.0_amd64.deb
```

See [`docs/INSTALL.md`](docs/INSTALL.md) for keys, Claude Code, the GTK3 build prerequisites and uninstall.
See [`docs/KIKO-AND-CLI.md`](docs/KIKO-AND-CLI.md) (and `.bg.md`) for Claude Code, aider, goose, rex, OpenCode, Codex and Gemini through the gateway.

## Components

| Folder | What |
|---|---|
| `gateway/` | AI proxy (`kiko_lite` + `kikolib`) — Python, stdlib only |
| `web/` | Admin web UI — Python, stdlib only |
| `spy/` | Live log viewer — Python, stdlib only |
| `tray/` | System tray front-end (see below) |
| `embedded/` | OpenWrt target — planned |

## Tray

| Folder | What |
|---|---|
| `tray/python/` | Dev layer — pystray + Pillow |
| `tray/common/` | Shared C11 core (state machine, HTTP, icon) |
| `tray/win/` | Windows — Win32 + Shell_NotifyIconW |
| `tray/gtk3/` | Linux Mint/Cinnamon — GTK3 |
| `tray/qt6/` | KDE Plasma — Qt6 |
| `tray/tqt3/` | Q4OS/Trinity — TQt3 |
| `tray/tk/` | Any platform — Python tkinter, stdlib only |
| `tray/mac/` | macOS — NSStatusItem |

## License

MIT — Copyright Peter Kokosharov <peter.kokosharov@gmail.com>
