# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Run every test suite that can run on this machine and write test-results.txt.

Usage (from anywhere):  python tools/run_tests.py [--no-venv] [--only NAME[,NAME]] [--out FILE]

The runner itself is stdlib only.  The pytest suites need pytest and friends (tests may use pip);
by default the runner creates <repo>/.venv and installs tools/requirements-test.txt there.
--no-venv uses the current interpreter instead (it must already have those packages).
Exit code 0 = no suite failed (suites that could not run here do not count as failures).
"""
import argparse
import datetime
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A pytest error/failure whose message matches this is NOT counted as a failure: it means the test needs the
# old "full" proxy stack (litellm, fastapi - not part of the stdlib-only gateway) or a file that no longer
# exists in the repo (gateway/web-ui, gateway/dist).  They are counted separately as "stale" and every stale
# test is listed by name in test-results.txt, so none hides unseen.
# kiko_tk is deliberately NOT here: it is a real package (tray/tk) that its tests stage themselves.
STALE = re.compile(r"No module named '(litellm|fastapi)'|"
                   r"(FileNotFoundError|not found).{0,200}[\\/](web-ui|dist)[\\/]|dist/\S+ not found")


class Result:
    def __init__(self, name, command=""):
        self.name = name
        self.command = command
        self.passed = self.failed = self.skipped = self.stale = 0
        self.stale_names = []
        self.status = "PASS"       # PASS | FAIL | NOT RUN
        self.note = ""
        self.tail = ""


def run(cmd, cwd=None, env=None, timeout=3000):
    p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def show(cmd):
    return " ".join("python" if str(c) == sys.executable else str(c) for c in cmd)   # no machine paths in the report


def count(text, word):
    m = re.findall(r"(\d+) " + word, text)
    return int(m[-1]) if m else 0


def parse_junit(res, xml_path, out, rc):
    """Fill res from a pytest --junitxml file; classify full-stack/stale errors (see STALE)."""
    import xml.etree.ElementTree as ET
    total = 0
    bad = []
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        res.status, res.note = "FAIL", "pytest wrote no result file (did it start?)"
        res.tail = "\n".join(out.strip().splitlines()[-25:])
        return
    for tc in root.iter("testcase"):
        total += 1
        node = tc.get("classname", "") + "::" + tc.get("name", "")
        kids = {k.tag: k for k in tc}
        if "skipped" in kids:
            res.skipped += 1
        elif "failure" in kids or "error" in kids:
            k = kids["failure"] if "failure" in kids else kids["error"]
            if STALE.search((k.get("message") or "") + (k.text or "")):
                res.stale += 1
                res.stale_names.append(node)
            else:
                res.failed += 1
                bad.append("FAILED " + node + " - " + (k.get("message") or "")[:120].replace("\n", " "))
    res.passed = total - res.failed - res.skipped - res.stale
    if res.failed or total == 0:
        res.status = "FAIL"
    if total == 0:
        res.note = "no tests were counted"
    more = ["... (%d more)" % (len(bad) - 40)] if len(bad) > 40 else []
    res.tail = "\n".join(bad[:40] + more)


def venv_python(use_venv):
    """Python that has pytest.  Returns (path, error text)."""
    if not use_venv:
        return sys.executable, ""
    venv = ROOT / ".venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        rc, out = run([sys.executable, "-m", "venv", str(venv)])
        if rc:
            return None, "cannot create .venv: " + out[-300:]
    req = ROOT / "tools" / "requirements-test.txt"
    rc, out = run([str(py), "-m", "pip", "install", "-q", "-r", str(req)])
    if rc:
        return None, "pip install failed (no network?): " + out[-300:]
    return str(py), ""


# ---- suites ----------------------------------------------------------------------------------

def suite_header():
    cmd = [sys.executable, "-I", "tools/check_ai_header.py"]
    r = Result("ai-header-check", show(cmd))
    rc, out = run(cmd, cwd=ROOT)
    r.passed, r.failed = (1, 0) if rc == 0 else (0, 1)
    r.status = "PASS" if rc == 0 else "FAIL"
    r.tail = out.strip()[-1500:]
    return r


def suite_unittest(name, folder, top=None):
    cmd = [sys.executable, "-I", "-m", "unittest", "discover", "-s", folder] + (["-t", top] if top else [])
    r = Result(name, show(cmd))
    rc, out = run(cmd, cwd=ROOT)
    ran = int((re.findall(r"Ran (\d+) test", out) or ["0"])[-1])
    r.skipped = int((re.findall(r"skipped=(\d+)", out) or ["0"])[-1])
    bad = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", out))
    r.failed = bad
    r.passed = max(ran - bad - r.skipped, 0)
    r.status = "PASS" if rc == 0 and ran > 0 else "FAIL"
    r.tail = "\n".join(out.strip().splitlines()[-25:])
    return r


def suite_tray_python(py):
    """tray/python is the Python package 'tray' (its tests do `import tray.config`): stage a copy under that name."""
    r = Result("tray-python (pytest)", "pytest tray/python/tests  (tray/python staged as package 'tray')")
    stage = Path(tempfile.mkdtemp(prefix="kiko_traytest_"))
    try:
        shutil.copytree(ROOT / "tray" / "python", stage / "tray",
                        ignore=shutil.ignore_patterns("__pycache__"))
        # the tray tests import the shared core (kiko_tray_core) that lives in tray/wincore
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT / "tray" / "wincore"))
        xml = stage / "r.xml"
        rc, out = run([py, "-m", "pytest", "tray/tests", "-q", "-p", "no:cacheprovider", "--junitxml", str(xml)],
                      cwd=stage, env=env)
        parse_junit(r, xml, out, rc)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return r


def suite_gateway(py):
    r = Result("gateway kiko_lite/kikolib/web/spy (pytest)",
               "cd gateway && PYTHONPATH=<repo>:<repo>/gateway pytest tests/kiko --continue-on-collection-errors")
    xml = Path(tempfile.mkdtemp(prefix="kiko_gwtest_")) / "r.xml"
    cmd = [py, "-m", "pytest", "tests/kiko", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors",
           "--junitxml", str(xml)]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "gateway")]),
               PYTHONDONTWRITEBYTECODE="1")
    rc, out = run(cmd, cwd=ROOT / "gateway", env=env)
    parse_junit(r, xml, out, rc)
    shutil.rmtree(xml.parent, ignore_errors=True)
    r.note = ("the vendored LiteLLM test tree under gateway/tests (everything except tests/kiko) is not run")
    return r


def parse_ctest(out):
    """(passed, failed) from the ctest summary line, or None when there is none.  Forms seen:
    "100% tests passed, 0 tests failed out of 7" (older ctest, Linux/Windows),
    "100% tests passed out of 7" and "86% tests passed, 1 tests failed out of 7" (CTest 4.x / mixed)."""
    m = re.search(r"(\d+)% tests passed(?:, (\d+) tests? failed)? out of (\d+)", out)
    if not m:
        return None
    total = int(m.group(3))
    failed = int(m.group(2)) if m.group(2) is not None else round(total * (100 - int(m.group(1))) / 100)
    return total - failed, failed


def suite_ctest():
    r = Result("tray C core (ctest)", "cmake -S tray -B <tmp> -DKT_BUILD_GUI=OFF && cmake --build && ctest")
    cmake, ctest = shutil.which("cmake"), shutil.which("ctest")
    if not cmake or not ctest:
        r.status, r.note = "NOT RUN", "cmake/ctest not installed"
        return r
    build = Path(tempfile.mkdtemp(prefix="kiko_ctest_"))
    try:
        rc, out = run([cmake, "-S", str(ROOT / "tray"), "-B", str(build), "-DKT_BUILD_GUI=OFF",
                       "-DKT_FETCH_CJSON=OFF"])
        if rc:
            if "No CMAKE_C_COMPILER" in out or "No CMAKE_CXX_COMPILER" in out:
                r.status, r.note = "NOT RUN", "no C compiler found"
            else:
                r.status, r.tail = "FAIL", out[-1500:]
                r.failed = 1
            return r
        rc, out = run([cmake, "--build", str(build), "--config", "Release"])
        if rc:
            r.status, r.failed, r.tail = "FAIL", 1, out[-1500:]
            return r
        rc, out = run([ctest, "--test-dir", str(build), "-C", "Release", "--output-on-failure"])
        counts = parse_ctest(out)
        if counts:
            r.passed, r.failed = counts
        r.status = "PASS" if rc == 0 and r.passed and not r.failed else "FAIL"
        r.tail = "\n".join(out.strip().splitlines()[-25:])
    finally:
        shutil.rmtree(build, ignore_errors=True)
    return r


# ---- main ------------------------------------------------------------------------------------

def git_commit():
    try:
        rc, out = run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"])
        dirty = run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"])[1].strip()
        return out.strip() + (" (+uncommitted changes)" if dirty else "") if rc == 0 else "unknown"
    except OSError:
        return "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-venv", action="store_true", help="use the current interpreter for the pytest suites")
    ap.add_argument("--only", help="comma separated: header,win,tray-python,gateway,ctest")
    ap.add_argument("--out", default=str(ROOT / "test-results.txt"))
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None

    def want(n):
        return only is None or n in only

    results = []
    if want("header"):
        results.append(suite_header())
    if want("win"):
        results.append(suite_unittest("windows tools (unittest)", "tools/win/tests"))
        results.append(suite_unittest("test runner (unittest)", "tools/tests"))
        results.append(suite_unittest("pkgkit (unittest)", "packaging/tests"))
        results.append(suite_unittest("tk tray model (unittest)", "tray/tk/tests", "tray"))
        results.append(suite_unittest("windows tray core (unittest)", "tray/wincore/tests", "tray/wincore"))
    if want("tray-python") or want("gateway"):
        py, err = venv_python(not a.no_venv)
        for key, fn, nm in (("tray-python", suite_tray_python, "tray-python (pytest)"),
                            ("gateway", suite_gateway, "gateway kiko_lite/kikolib/web/spy (pytest)")):
            if not want(key):
                continue
            if py is None:
                r = Result(nm)
                r.status, r.note = "NOT RUN", err
                results.append(r)
            else:
                results.append(fn(py))
    if want("ctest"):
        results.append(suite_ctest())

    ran = [r for r in results if r.status != "NOT RUN"]
    overall = "PASS" if ran and all(r.status == "PASS" for r in ran) else "FAIL"
    lines = [
        "kiko test results",
        "date (UTC): " + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "git commit: " + git_commit(),
        "os: %s %s (%s), python %s" % (platform.system(), platform.release(), platform.machine(),
                                       platform.python_version()),
        "",
    ]
    for r in results:
        if r.status == "NOT RUN":
            lines.append("not run here: %s: %s" % (r.name, r.note))
            continue
        lines += ["suite: " + r.name, "  command: " + r.command,
                  "  passed: %d  failed: %d  skipped: %d  stale: %d  -> %s"
                  % (r.passed, r.failed, r.skipped, r.stale, r.status)]
        if r.note:
            lines.append("  note: " + r.note)
        if r.stale_names:
            lines.append("  stale (needs litellm/fastapi or a removed web-ui/dist file; not counted as failure):")
            lines += ["    " + n for n in r.stale_names]
    lines += ["", "OVERALL: " + overall]
    text = "\n".join(lines) + "\n"
    Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    print(text)
    for r in ran:
        if r.status == "FAIL":
            print("---- %s: last output ----\n%s\n" % (r.name, r.tail))
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
