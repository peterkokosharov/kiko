# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Tests for packaging/pkgkit. Run: python -I -m unittest discover -s packaging/tests
The end-to-end test (build, install, uninstall) runs only on Windows; it uses a throw-away USERPROFILE/APPDATA."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parents[1] / "pkgkit"
sys.path.insert(0, str(KIT))
import build  # noqa: E402
import manifest  # noqa: E402
import uninstall  # noqa: E402


def make_app(root: Path):
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (root / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "app" / "tests").mkdir()
    (root / "app" / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    (root / "app" / "test_y.py").write_text("y = 1\n", encoding="utf-8")
    (root / "app" / "conftest.py").write_text("z = 1\n", encoding="utf-8")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "main.cpython-314.pyc").write_bytes(b"\0")
    (root / "fake.cmd.in").write_text("@echo off\necho fake 1.2.3\n", encoding="utf-8")
    toml = root / "app.toml"
    toml.write_text(
        '[app]\nname = "fake"\ndescription = "d"\nmin_python = "3.11"\nversion_file = "app/__init__.py"\n'
        '[payload]\ndir = "fk"\ninclude = ["app"]\n[[payload.file]]\nsrc = "fake.cmd.in"\ndest = "fake.cmd"\n'
        '[shortcut]\nname = "fake"\ntarget = "fk/fake.cmd"\nargs = "--version"\nconsole = true\n'
        '[check]\ncmd = ["fk/fake.cmd", "--version"]\n', encoding="utf-8")
    return toml


class Kit(unittest.TestCase):
    def test_own_manifest_matches(self):
        self.assertEqual(manifest.check(KIT), [])

    def test_is_test_path(self):
        for bad in ("a/tests/x.py", "a/test_x.py", "conftest.py", "a/__pycache__/x.pyc", "a/x.pyc", ".git/config"):
            self.assertTrue(build.is_test_path(bad), bad)
        for ok in ("a/main.py", "a/latest.py", "a/attestation.py", "vendor/kikolib/http.py"):
            self.assertFalse(build.is_test_path(ok), ok)

    def test_build_layout_and_no_tests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            root.mkdir()
            toml = make_app(root)
            zpath = build.build(root, toml, Path(td) / "dist", [])
            self.assertEqual(zpath.name, "fake-1.2.3-win-x64.zip")
            with zipfile.ZipFile(zpath) as z:
                names = [n.split("/", 1)[1] for n in z.namelist()]
                top = {n.split("/")[0] for n in z.namelist()}
                self.assertEqual(top, {"fake-1.2.3-win-x64"})
                for need in ("install.cmd", "uninstall.cmd", "README.txt", "payload/fk/app/main.py",
                             "payload/fk/fake.cmd", "_pkg/install.py", "_pkg/uninstall.py", "_pkg/MANIFEST",
                             "_pkg/app.json"):
                    self.assertIn(need, names)
                self.assertFalse([n for n in names if build.is_test_path(n)])
                self.assertIn(b"\r\n", z.read("fake-1.2.3-win-x64/install.cmd"))
                self.assertNotIn(b"\n", z.read("fake-1.2.3-win-x64/install.cmd").replace(b"\r\n", b""))
            self.assertEqual(build.list_zip(zpath), 0)

    def test_test_file_in_zip_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "src"
            root.mkdir()
            toml = make_app(root)
            (root / "t.py").write_text("1\n", encoding="utf-8")
            zpath = build.build(root, toml, Path(td) / "dist", [f"{root / 't.py'}=sub/test_t.py"])
            self.assertEqual(build.list_zip(zpath), 1)


class Uninstall(unittest.TestCase):
    def test_modified_file_is_kept_and_unsafe_path_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            app, ad = Path(td) / "app", Path(td) / "appdata"
            app.mkdir()
            ad.mkdir()
            (app / "a.txt").write_text("one", encoding="utf-8")
            (app / "b.txt").write_text("two", encoding="utf-8")
            rec = {"created": [
                {"path": "a.txt", "kind": "file", "sha256": manifest.hashlib.sha256(b"one").hexdigest()},
                {"path": "b.txt", "kind": "file", "sha256": manifest.hashlib.sha256(b"two").hexdigest()},
                {"path": "../x", "kind": "file", "sha256": "0"}]}
            (app / "install-record.json").write_text(json.dumps(rec), encoding="utf-8")
            (app / "b.txt").write_text("changed", encoding="utf-8")
            lines = []
            old = os.environ.get("APPDATA")
            os.environ["APPDATA"] = str(ad)
            try:
                rc = uninstall.uninstall(app, False, out=lines.append, tries=1)
            finally:
                if old is None:
                    del os.environ["APPDATA"]
                else:
                    os.environ["APPDATA"] = old
            self.assertEqual(rc, 1)
            self.assertFalse((app / "a.txt").exists())
            self.assertTrue((app / "b.txt").exists())
            self.assertTrue((app / "install-record.json").exists())
            self.assertIn("KEPT (modified): b.txt", lines)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class EndToEnd(unittest.TestCase):
    def test_install_uninstall(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            root = td / "src"
            root.mkdir()
            zpath = build.build(root, make_app(root), td / "dist", [])
            ex = td / "unz"
            with zipfile.ZipFile(zpath) as z:
                z.extractall(ex)
            pkg = ex / zpath.stem
            prof = td / "profile"
            appdata = prof / "AppData" / "Roaming"
            appdata.mkdir(parents=True)
            env = {**os.environ, "USERPROFILE": str(prof), "APPDATA": str(appdata), "TEMP": str(td)}
            r = subprocess.run(["cmd", "/c", str(pkg / "install.cmd"), "/quiet"], env=env, capture_output=True,
                               text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("OK fake 1.2.3 installed in", r.stdout)
            dest = prof / "apps" / "fake"
            self.assertTrue((dest / "fk" / "app" / "main.py").is_file())
            self.assertTrue((dest / "uninstall.cmd").is_file())
            lnk = appdata / "Microsoft/Windows/Start Menu/Programs/apps/fake.lnk"
            self.assertTrue(lnk.is_file())
            r = subprocess.run(["cmd", "/c", str(dest / "uninstall.cmd"), "/quiet"], env=env, capture_output=True,
                               text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(dest.exists())
            self.assertFalse((prof / "apps").exists())  # created by the install, so removed with it
            self.assertFalse(lnk.exists())
            self.assertFalse((lnk.parent).exists())


if __name__ == "__main__":
    unittest.main()
