<!-- SPDX-License-Identifier: MIT -->
<!-- Author: Peter Kokosharov <peter.kokosharov@gmail.com> -->
<!-- AI models: claude-sonnet-5-5 -->

# tray/tk - kiko tray, Tk 9 variant (stdlib only)

## What is here

- `__main__.py`, `app.py`, `client.py`, `model.py`: the `kiko_tk` package (window with one tab per kiko-lite
  panel, plus a real system-tray icon through the Tk 9 `tk systray` command).
- `launcher.py`: staged by the installer as `kiko_tray_tk.pyw`; starts the tray hidden, logs start errors.
- `tests/test_model.py`: unit tests of the model logic (state, tooltip, table rows); no Tk needed.
- The Models tab ends its first columns with `context` and `max out` (`1M`, `262k`, `?` = unknown; `model.fmt_tokens`, a mirror of `kiko_lite.db.fmt_tokens`, data from `/ui/api/status`).
- Shared logic (health state, Start/Stop of the chain, token) is `tray/wincore/kiko_tray_core.py`.

## What it is for

Windows tray that needs no package: Python 3.14 with Tk 9 is enough. Icon colour follows `/health` on port 4000
(green ok, red down, grey waiting; amber when the panels list errors). Menu: Open window, Open web UI,
Start chain (runs `apps\kiko\kiko_chain.py` as a child), Stop chain (only that child; a chain started outside
the tray: "kiko was started outside the tray", nothing killed), Copy API token, Quit (also ends the child). Without Tk 9 (8.6)
there is no tray, only the window. Install: `python -I tools/win/install.py tray-tk`
(see `docs/WINDOWS-PORTABLE-INSTALL.md`, "Three tray variants and the swap test").

## What a human does

Install and uninstall with the commands above; start it from the Start menu entry "kiko tray (tk)". Run only
one kiko tray at a time. Port: 4000 by default, `kiko-tray.ini` or env `KIKO_PORT`; `--url` overrides all.
The panel tabs need a kiko-lite client key (`--key` or env `KIKO_TK_KEY`); without it the icon is still green
when `/health` answers, and the tooltip says the panels need a key.

## What an LLM does

Run `python -I -m unittest discover -s tray/tk/tests -t tray` from the repo root. Green: report OK. Red: check
`http://localhost:4000/health`; if kiko works, report "there is a problem" with the test output. Never fix.
