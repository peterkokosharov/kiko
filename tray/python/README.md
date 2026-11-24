<!-- SPDX-License-Identifier: MIT -->
<!-- Author: Peter Kokosharov <peter.kokosharov@gmail.com> -->
<!-- AI models: claude-sonnet-4-6 (older files); claude-sonnet-5-5 (winapp.py, launcher.py, this README) -->

# tray/python - kiko tray, pystray variant

## What is here

- `winapp.py`, `launcher.py`, `requirements.txt`: the Windows tray of the shared contract (icon colour from
  `/health` on port 4000, Start/Stop chain, Copy API token) and its launcher. Shared logic is
  `tray/wincore/kiko_tray_core.py`.
- `app.py`, `control.py`, `config.py`, `backend.py`, `roles.py`, `logs.py`, `dialogs/`: the older pystray
  tray (starts a `kiko` executable from a YAML config); kept, with its tests.
- `tests/`: unit tests of both (`tests/__init__.py` registers this folder as the package `tray`, so they run
  from the repo).

## What it is for

The pystray + Pillow tray. On Windows the installer (`python -I tools/win/install.py tray-py`) builds a private venv
`apps\kiko-tray-py\venv` from the pinned `requirements.txt` (PyPI, at install time only); nothing goes into the
system Python, and uninstall removes the venv with the app. Start-menu entry: "kiko tray (python)". Run
from the repo: `python tray/python/launcher.py` (needs pystray and Pillow in your own venv).
Not part of the "no pip" production line; it exists for comparison with the tk and native trays.

## What a human does

Install and uninstall with the commands in `docs/WINDOWS-PORTABLE-INSTALL.md` ("Three tray variants and the
swap test"); start from the Start menu. Run only one kiko tray at a time.

## What an LLM does

Run `python -I -m unittest discover -s tray/python/tests -t tray/python` and `tray/wincore/tests` from the repo
root. Green: report OK. Red: check `http://localhost:4000/health`; if kiko works, report "there is a problem"
with the test output. Never fix.
