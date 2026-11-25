# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5, claude-opus-4-8
"""Install targets tray-tk and tray-py (temp folders; the venv is mocked except in one network test).

Run: python -I -m unittest discover -s tools/win/tests -t tools/win
The files come from git HEAD (like the other targets), so commit the tray/ sources before running.
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WIN))
import install  # noqa: E402
import portable  # noqa: E402
import uninstall  # noqa: E402
import winlnk  # noqa: E402


def fake_create_shortcut(lnk_path, target, args="", workdir="", description=""):
    """Stands in for winlnk.create_shortcut where there is no COM (Linux, macOS): same contract, a plain file."""
    p = Path(lnk_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{target}|{args}|{workdir}|{description}", encoding="utf-8")
    return {"kind": "lnk", "path": str(p)}


def tree(p: Path):
    return sorted(str(x.relative_to(p)) for x in p.rglob("*")) if p.exists() else []


def fake_venv(venv: Path, requirements: Path) -> None:
    """Stands in for create_venv: a folder with many files, like a real venv."""
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "pythonw.exe").write_bytes(b"MZ")
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    sp = venv / "Lib" / "site-packages" / "pystray"
    sp.mkdir(parents=True)
    for i in range(5):
        (sp / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._td.name).resolve()
        self.apps = self.tmp / "apps"
        self.appdata = self.tmp / "appdata"
        (self.apps / "kiko" / "data").mkdir(parents=True)
        (self.apps / "kiko" / "install-record.json").write_text("{}", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        env.start()
        self.addCleanup(env.stop)
        if os.name != "nt":  # the installer logic is tested everywhere; only real .lnk writing needs Windows
            sc = mock.patch.object(winlnk, "create_shortcut", side_effect=fake_create_shortcut)
            sc.start()
            self.addCleanup(sc.stop)

    def tearDown(self):
        self._td.cleanup()

    def run_install(self, target, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = install.main([target, "--dest", str(self.apps / install.TRAY_VARIANTS[target][0]), *extra])
        return rc, out.getvalue()

    def run_uninstall(self, folder):
        out = []
        rc = uninstall.uninstall(self.apps / folder, out=out.append)
        return rc, out

    def lnks(self):
        return sorted(p.name for p in self.appdata.rglob("*") if p.is_file())


class TrayTk(Base):
    def test_install_files_ini_shortcut_uninstall(self):
        before = tree(self.apps)
        rc, out = self.run_install("tray-tk")
        self.assertEqual(rc, 0, out)
        d = self.apps / "kiko-tray-tk"
        for f in ("kiko_tk/__main__.py", "kiko_tk/app.py", "kiko_tk/model.py", "kiko_tk/client.py",
                  "kiko_tray_core.py", "kiko_tray_tk.pyw", "kiko-tray.ini", "uninstall.py", "install-record.json"):
            self.assertTrue((d / f).is_file(), f)
        self.assertFalse((d / "kiko_tk" / "launcher.py").exists())
        self.assertFalse((d / "kiko_tk" / "tests").exists())
        ini = (d / "kiko-tray.ini").read_text(encoding="utf-8")
        self.assertIn("port = 4000", ini)
        self.assertIn(str(self.apps / "kiko" / "kiko_chain.py"), ini)
        self.assertIn("api-token", ini)
        self.assertEqual(self.lnks(), ["kiko tray (tk).lnk"])
        rec = json.loads((d / "install-record.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["app"], "kiko-tray-tk")
        self.assertEqual(portable.verify(d), [])
        rc, out = self.run_uninstall("kiko-tray-tk")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tree(self.apps), before)
        self.assertEqual(self.lnks(), [])

    def test_requires_kiko(self):
        (self.apps / "kiko" / "install-record.json").unlink()
        with self.assertRaises(SystemExit):
            self.run_install("tray-tk")
        self.assertFalse((self.apps / "kiko-tray-tk").exists())


class TrayPy(Base):
    def test_install_venv_recorded_and_removed(self):
        before = tree(self.apps)
        with mock.patch.object(install, "create_venv", side_effect=fake_venv) as cv:
            rc, out = self.run_install("tray-py")
        self.assertEqual(rc, 0, out)
        cv.assert_called_once()
        d = self.apps / "kiko-tray-py"
        for f in ("tray/winapp.py", "tray/app.py", "kiko_tray_core.py", "kiko_tray_py.pyw", "requirements.txt",
                  "kiko-tray.ini", "venv/pyvenv.cfg"):
            self.assertTrue((d / f).exists(), f)
        self.assertFalse((d / "tray" / "launcher.py").exists())
        req = (d / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(req, r"(?m)^pystray==\d")
        self.assertRegex(req, r"(?m)^Pillow==\d")
        rec = json.loads((d / "install-record.json").read_text(encoding="utf-8"))
        self.assertIn({"path": "venv", "kind": "tree", "sha256": ""}, rec["created"])
        self.assertEqual(self.lnks(), ["kiko tray (python).lnk"])
        self.assertEqual(portable.verify(d), [])
        rc, out = self.run_uninstall("kiko-tray-py")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tree(self.apps), before)
        self.assertEqual(self.lnks(), [])

    def test_failed_venv_rolls_back(self):
        before = tree(self.apps)

        def broken(venv, req):
            fake_venv(venv, req)
            raise SystemExit("pip failed")
        with mock.patch.object(install, "create_venv", side_effect=broken):
            with self.assertRaises(SystemExit):
                self.run_install("tray-py")
        self.assertEqual(tree(self.apps), before)
        self.assertEqual(self.lnks(), [])

    def test_uninstall_tree_outside_is_refused(self):
        with mock.patch.object(install, "create_venv", side_effect=fake_venv):
            self.run_install("tray-py")
        d = self.apps / "kiko-tray-py"
        rec = json.loads((d / "install-record.json").read_text(encoding="utf-8"))
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("x", encoding="utf-8")
        rec["created"].append({"path": "../victim", "kind": "tree", "sha256": ""})
        (d / "install-record.json").write_text(json.dumps(rec), encoding="utf-8")
        rc, out = self.run_uninstall("kiko-tray-py")
        self.assertEqual(rc, 1, out)
        self.assertTrue((victim / "keep.txt").is_file())


class Swap(Base):
    def test_other_tray_warning_and_swap_cycle(self):
        before = tree(self.apps)
        with mock.patch.object(install, "create_venv", side_effect=fake_venv):
            rc, out = self.run_install("tray-tk")
            self.assertEqual(rc, 0)
            self.assertNotIn("WARNING", out)
            rc, out = self.run_install("tray-py")  # tk still installed: warn, do not block
            self.assertEqual(rc, 0, out)
            self.assertIn("WARNING: another kiko tray is installed: kiko tray (tk)", out)
            self.assertEqual(self.lnks(), ["kiko tray (python).lnk", "kiko tray (tk).lnk"])
            self.assertEqual(self.run_uninstall("kiko-tray-tk")[0], 0)
            self.assertEqual(self.run_uninstall("kiko-tray-py")[0], 0)
            self.assertEqual(tree(self.apps), before)
            self.assertEqual(self.lnks(), [])
            for t in ("tray-tk", "tray-py", "tray-tk"):  # reinstall cycle leaves nothing behind either
                self.assertEqual(self.run_install(t)[0], 0)
                self.assertEqual(self.run_uninstall(install.TRAY_VARIANTS[t][0])[0], 0)
            self.assertEqual(tree(self.apps), before)

    def test_other_trays_lists_native(self):
        n = self.apps / "kiko-tray"
        n.mkdir()
        (n / "install-record.json").write_text("{}", encoding="utf-8")
        self.assertEqual(len(install.other_trays(self.apps / "kiko-tray-tk")), 1)
        self.assertEqual(install.other_trays(n), [])


def pypi_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
        return True
    except OSError:
        return False


def pip_list() -> str:
    return subprocess.run([sys.executable, "-m", "pip", "list", "--format=freeze", "--disable-pip-version-check"],
                          capture_output=True, text=True).stdout


@unittest.skipUnless(pypi_reachable(), "no network to PyPI")
class TrayPyReal(Base):
    def test_real_venv_end_to_end(self):
        system_before = pip_list()
        rc, out = self.run_install("tray-py")
        self.assertEqual(rc, 0, out)
        d = self.apps / "kiko-tray-py"
        vpy = d / "venv" / "Scripts" / "python.exe"
        if not vpy.exists():
            vpy = d / "venv" / "bin" / "python"
        # pystray selects its GUI backend at import; on headless Linux that is _xorg.py, which raises
        # DisplayNameError with no DISPLAY (macOS=darwin, Windows=win32 need none). Smoke-import PIL always,
        # and pystray only where a display can actually back it, so the venv is still proven on headless CI.
        if sys.platform == "linux" and not os.environ.get("DISPLAY"):
            code = "import PIL; print('PIL', PIL.__version__)"
        else:
            code = "import pystray, PIL; print(pystray.__name__, PIL.__version__)"
        r = subprocess.run([str(vpy), "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pip_list(), system_before)  # the system Python got nothing
        rc, out = self.run_uninstall("kiko-tray-py")
        self.assertEqual(rc, 0, out)
        self.assertFalse(d.exists())
        self.assertEqual(self.lnks(), [])
        self.assertEqual(pip_list(), system_before)


if __name__ == "__main__":
    unittest.main()
