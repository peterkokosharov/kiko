# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Run: python -I -m unittest discover -s tools/win/tests -t tools/win  (or run this file directly)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

WIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WIN))
import portable  # noqa: E402
import winlnk  # noqa: E402


def tree(p: Path):
    return sorted(str(x.relative_to(p)) for x in p.rglob("*")) if p.exists() else []


class InstallKikolib(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.dest = self.tmp / "apps" / "kikolib"
        self.env = dict(os.environ, APPDATA=str(self.tmp / "appdata"))

    def tearDown(self):
        self._td.cleanup()

    def run_py(self, script, *args):
        return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True,
                              text=True, env=self.env)

    def install(self):
        r = self.run_py(WIN / "install.py", "kikolib", "--dest", str(self.dest))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_install_record_uninstall(self):
        self.install()
        rec = json.loads((self.dest / "install-record.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["app"], "kikolib")
        self.assertEqual(set(rec["source"]), {"repo", "commit"})
        self.assertEqual(len(rec["source"]["commit"]), 40)
        for e in rec["created"]:
            self.assertFalse(Path(e["path"]).is_absolute())
            self.assertIn(e["kind"], ("file", "dir", "shortcut"))
        self.assertTrue((self.dest / "uninstall.py").is_file())
        self.assertEqual(portable.verify(self.dest), [])
        r = self.run_py(self.dest / "uninstall.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.dest.exists(), tree(self.dest))

    def test_reinstall_keeps_data_and_purge_removes(self):
        self.install()
        (self.dest / "data").mkdir()
        (self.dest / "data" / "x.db").write_text("keep")
        self.install()  # upgrade path
        self.assertTrue((self.dest / "data" / "x.db").is_file())
        self.run_py(self.dest / "uninstall.py")
        self.assertEqual(tree(self.dest), ["data", os.path.join("data", "x.db")])
        self.install()
        self.assertEqual(self.run_py(self.dest / "uninstall.py", "--purge").returncode, 0)
        self.assertFalse(self.dest.exists())

    def test_modified_file_kept(self):
        self.install()
        changed = self.dest / "kikolib" / "config.py"
        changed.write_text("# edited by user\n")
        r = self.run_py(self.dest / "uninstall.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("KEPT (modified): kikolib/config.py", r.stdout)
        self.assertTrue(changed.is_file())
        self.assertTrue((self.dest / "install-record.json").is_file())  # still re-runnable
        self.assertFalse((self.dest / "kikolib" / "http.py").exists())  # the rest is gone

    def test_shortcut_removed_first(self):
        self.install()
        rel = "Microsoft/Windows/Start Menu/Programs/apps/t.lnk"
        sc = Path(self.env["APPDATA"]) / rel
        sc.parent.mkdir(parents=True)
        sc.write_text("x")
        recp = self.dest / "install-record.json"
        rec = json.loads(recp.read_text(encoding="utf-8"))
        rec["created"].append({"path": rel, "kind": "shortcut", "sha256": ""})
        recp.write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(self.run_py(self.dest / "uninstall.py").returncode, 0)
        self.assertFalse(sc.exists())
        self.assertFalse(sc.parent.exists())  # empty apps folder removed too

    def test_refuses_unrecorded_folder(self):
        self.dest.mkdir(parents=True)
        (self.dest / "mine.txt").write_text("x")
        r = self.run_py(WIN / "install.py", "kikolib", "--dest", str(self.dest))
        self.assertEqual(r.returncode, 1)
        self.assertTrue((self.dest / "mine.txt").is_file())


@unittest.skipUnless(sys.platform == "win32", "COM shortcuts are Windows only")
class Lnk(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            lnk = Path(td) / "sub" / "Demo.lnk"
            target = Path(sys.executable)
            res = winlnk.create_shortcut(lnk, target, args="-I -c pass", workdir=td, description="demo")
            self.assertEqual(res["kind"], "lnk", res)
            back = winlnk.read_shortcut(res["path"])
            self.assertEqual(os.path.normcase(back["target"]), os.path.normcase(str(target)))
            self.assertEqual(back["args"], "-I -c pass")
            self.assertEqual(back["description"], "demo")
            self.assertTrue(winlnk.delete_shortcut(res["path"]))
            self.assertFalse(lnk.exists())


if __name__ == "__main__":
    unittest.main()
