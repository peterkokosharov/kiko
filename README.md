# kiko

Personal AI gateway and system tray front-end.

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
