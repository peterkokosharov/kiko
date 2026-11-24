# pkgkit changes

## 0.1.0 - first release (Windows part only)

Build a `<name>-<ver>-win-x64.zip` from a project's `app.toml`; `install.cmd` / `uninstall.cmd` with a recorded,
exact uninstall (modified files kept, locked files retried then kept, `/purge` for data and logs).
Files: build.py, install.py, uninstall.py, winlnk.py, manifest.py, install.cmd.in, uninstall.cmd.in.
winlnk.py, the uninstall logic and the record format are copied from tools/win (portable.py, uninstall.py, winlnk.py)
of kiko-src; the migration of tools/win to pkgkit is a later step. Linux/macOS part: not yet.

Author: Peter Kokosharov. AI models: claude-sonnet-5-5
