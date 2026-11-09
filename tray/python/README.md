# tray/python — pystray development layer

Python-only system tray front-end using `pystray` + `Pillow`.
Works on all platforms without compiling the native C/C++ layers.
**For development and testing only — not production.**

## Dependencies (pip)

- `pystray` — system tray icon (xorg backend on Linux)
- `Pillow` — draws the K icon in memory

## TODO: replace pip dependencies with stdlib/ctypes

Goal: zero pip dependencies, like the rest of kiko.

- **Icon**: bundle a pre-drawn `icon.png`; drop Pillow.
- **Tray on Windows**: ctypes + Shell_NotifyIconW (mirrors `tray/win/`).
- **Tray on Linux**: ctypes + GTK3 (mirrors `tray/gtk3/`).

Until then, install with:

    pip install pystray Pillow
    python -m tray
