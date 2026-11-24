# packaging/tests

## What is here

`test_pkgkit.py`: unit tests for `packaging/pkgkit` (manifest, build layout and test exclusion, uninstall keeping a
modified file) and, on Windows, an end-to-end build + install + uninstall of a fake app in a throw-away profile.

## What it is for

Keeps pkgkit honest. The tests live here, not inside `pkgkit/`, because pkgkit is copied unmodified into other projects.

## What a human does

`python -I -m unittest discover -s packaging/tests` (or `python tools/run_tests.py`).

## What an LLM does

Run `tools/run_tests.py`. Green: report OK. Red: check that http://localhost:4000/health answers; if kiko works, report
"there is a problem" with the failing output. Never try to fix it.

Author: Peter Kokosharov. AI models: claude-sonnet-5-5
