<!-- SPDX-License-Identifier: MIT -->
<!-- Author: Peter Kokosharov <peter.kokosharov@gmail.com> -->
<!-- AI models: claude-sonnet-5-5 -->

# Testing kiko

One command runs every automated suite that can run on your machine:

```
python tools/run_tests.py
```

It prints a summary and writes `test-results.txt` in the repository root (date in UTC, git commit, OS, and per
suite: command, passed / failed / skipped / stale, then `OVERALL: PASS` or `FAIL`). Suites that cannot run on the
machine are listed as `not run here: <reason>` and do not make the result red. Exit code 0 = nothing failed.
The checked-in `test-results.txt` is the result of the last run on the author's Windows machine; CI uploads a
fresh one per OS as an artifact (Actions > run > Artifacts > `test-results-<os>`).

## Prerequisites

| | Windows 10/11 | Linux / Mint | macOS |
|---|---|---|---|
| Python 3.10+ (3.12 recommended) with `venv` | python.org or any install; `python` on PATH | `sudo apt install python3 python3-venv` | `brew install python` |
| git | yes (`git archive` is used by one test, commit lookup) | `sudo apt install git` | Xcode CLT |
| C core tests: CMake 3.16+ and a C compiler | Visual Studio Build Tools 2022 ("Desktop development with C++") or MinGW-w64, plus CMake | `sudo apt install build-essential cmake` | `xcode-select --install`, `brew install cmake` |
| GTK3 tray build (optional, Linux only) | - | `sudo apt install pkg-config libgtk-3-dev libxapp-dev` | - |
| Network | only for `pip install` of the test packages (first run) | same | same |

Production code is Python standard library only. The tests may use pip packages; they are listed in
`tools/requirements-test.txt` (pytest, pytest-asyncio, pytest-mock, respx, httpx, psutil, pyyaml).

## The virtual environment

`tools/run_tests.py` creates `.venv/` in the repository root itself and installs
`tools/requirements-test.txt` into it (git-ignored). You do not have to do anything. To do it by hand:

```
# Windows (Git Bash or cmd)
python -m venv .venv
.venv\Scripts\python -m pip install -r tools/requirements-test.txt
# Linux / macOS
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements-test.txt
```

Then `python tools/run_tests.py --no-venv` uses the interpreter you started it with (it must already have the
packages: activate the venv first). If there is no network and no packages, the two pytest suites show up as
`not run here`; the rest still runs.

Options: `--only header,win,tray-python,gateway,ctest` runs only some suites; `--out FILE` writes the result
elsewhere.

## The suites

| Suite (name in test-results.txt) | What it covers | Run it alone (from the repo root) |
|---|---|---|
| `ai-header-check` | every file with an `Author:` header also has an `AI models:` / `LLM:` line (`tools/check_ai_header.py`) | `python -I tools/check_ai_header.py` |
| `windows tools (unittest)` | `tools/win/`: portable install / uninstall, kikolib copy and manifest, kiko-chain, `.lnk` creation, the AI-header checker. Stdlib `unittest`; takes about 2 minutes (it starts real subprocesses) | `python -I -m unittest discover -s tools/win/tests` |
| `tray-python (pytest)` | `tray/python/`: tray state logic, backend, roles, logs, dialogs. No GUI toolkit needed. The folder is the Python package `tray`, so the runner copies it to a temp dir under that name | see note below |
| `gateway ... (pytest)` | `gateway/tests/kiko`: kiko_lite app and routes, failover, Anthropic/OpenAI/Gemini/Responses fronts, kikolib (http, retry, server, websocket, keystore, yaml, WinHTTP), web admin UI, spy. About 9 minutes on Windows | `cd gateway` then `PYTHONPATH=<repo>:<repo>/gateway python -m pytest tests/kiko --continue-on-collection-errors` (use `;` instead of `:` in PYTHONPATH on Windows) |
| `tray C core (ctest)` | `tray/common/tests`: JSON, HTTP client, secret masking, ini parser, utilities and icon drawing, presenter state machine, socket test. No GUI toolkit | `cmake -S tray -B build -DKT_BUILD_GUI=OFF` then `cmake --build build --config Release` then `ctest --test-dir build -C Release --output-on-failure` |

Notes:

- The runner is the supported way to run the gateway and tray-python suites; their exact invocation is in
  `suite_gateway` and `suite_tray_python` in `tools/run_tests.py` (a few lines each).
- **Stale tests:** part of `gateway/tests/kiko` still targets the old full proxy: it needs `litellm` /
  `fastapi` / `kiko_tk` (not installed, not part of the stdlib-only `kiko_lite`) or files that no longer exist
  (`gateway/web-ui/`, `gateway/dist/`). The runner does not count those as failures; it counts them in the
  `stale:` column of `test-results.txt`. A stale number that grows means more tests lost their target:
  fix or delete them. The large vendored LiteLLM test tree in `gateway/tests/` (everything except
  `tests/kiko`) is not run; it needs the full LiteLLM stack, keys and network, and is not part of kiko.
- Some tests are Windows-only (WinHTTP, Credential Manager) and report as skipped on Linux/macOS.
- GTK3 tray GUI layer: only compiled in CI (job `gtk3-tray`), not unit-tested. Build it yourself with
  `cmake -S tray -B build && cmake --build build -j && ctest --test-dir build --output-on-failure` after
  installing the GTK3 packages above. Details: [`tray/gtk3/README.md`](tray/gtk3/README.md).
- Windows, macOS and Qt tray GUI layers are built only by `win-tray.yml` (Windows). macOS and Qt are unverified.

## What red means and where to look

- `OVERALL: FAIL` and the console prints "last output" for each failed suite: read the first `FAILED` /
  `Error` line there. Re-run just that suite with `--only <name>` and, for pytest, add `-x -vv` to the command
  from the table.
- `ai-header-check` red: the listed file has an author header but no `AI models:` line. Add the line
  (the model that wrote or edited the file), see the README section "AI authorship".
- `windows tools` red on a clean checkout: usually `git` missing from PATH or a path with unusual characters;
  the failing test name tells which tool (`install`, `portable`, `kiko_chain`, `winlnk`).
- `tray C core` red at configure: missing compiler/CMake ("not run here" if no compiler is found at all);
  red at ctest: a core test failed, the failing test and its assertion are in the ctest output.
- `gateway` red: tests start local servers on random ports. A firewall prompt, or a proxy set in
  `HTTP_PROXY`, can break them; try with the proxy variables unset.
- A suite reporting `no tests were counted` means the suite did not start (import error): look at the
  printed output.
- In CI: open the failed job, read the step log, and download `test-results-<os>` for the summary.

## Manual test plans (need a real desktop; not automated)

| Plan | For |
|---|---|
| [`docs/TEST-PLAN-WINDOWS.md`](docs/TEST-PLAN-WINDOWS.md) | native Windows tray |
| [`docs/TEST-PLAN-MINT.md`](docs/TEST-PLAN-MINT.md) | GTK3 tray on Linux Mint Cinnamon |
| [`docs/TEST-PLAN-PLASMA.md`](docs/TEST-PLAN-PLASMA.md) | Qt tray on KDE Plasma |
| [`docs/TEST-PLAN-MACOS.md`](docs/TEST-PLAN-MACOS.md) | macOS tray |
| [`docs/gateway/HOME-TEST.md`](docs/gateway/HOME-TEST.md) | gateway at home with real provider keys |
| [`docs/gateway/LINUX-KEYSTORE-TEST.md`](docs/gateway/LINUX-KEYSTORE-TEST.md) | Linux keyring storage |

## Tests and releases

Tests live in the repository only. The release artifacts are built by scripts that copy named files:
`.github/workflows/win-tray.yml` (exe + LICENSE), `packaging/build-deb.sh` (gateway `kikolib`, `kiko_lite`,
`tray/tools`), `packaging/build-tray-gtk3-deb.sh` (the built binary + icons), `tools/win/install.py` (takes
`gateway/kikolib` and its manifest from git). None of them includes a `tests/` folder.

## CI

`.github/workflows/tests.yml` runs on every push and pull request: job `suites` runs
`python tools/run_tests.py` on Ubuntu, Windows and macOS and uploads `test-results.txt`; job `gtk3-tray`
installs the GTK3 packages, builds the tray with the GUI layer and runs ctest. `win-tray.yml` still builds,
tests and packages the Windows tray.
