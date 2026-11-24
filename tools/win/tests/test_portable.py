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


class InstallKiko(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.apps = self.tmp / "apps"
        self.dest = self.apps / "kiko"
        self.env = dict(os.environ, APPDATA=str(self.tmp / "appdata"))
        self.rex = self.tmp / "rex"  # fake rex repo: only the two files the installer copies
        (self.rex / "scripts").mkdir(parents=True)
        (self.rex / "runtimelib").mkdir()
        (self.rex / "scripts" / "claude_cli_gateway.py").write_text("# fake gateway\n")
        (self.rex / "runtimelib" / "procutil.py").write_text("# fake procutil\n")
        g = ["git", "-C", str(self.rex), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*g, "init", "-q"], check=True)
        subprocess.run([*g, "add", "."], check=True)
        subprocess.run([*g, "commit", "-q", "-m", "x"], check=True)
        # a working-tree edit must not leak: the installer reads git HEAD
        (self.rex / "scripts" / "claude_cli_gateway.py").write_text("# edited, uncommitted\n")

    def tearDown(self):
        self._td.cleanup()

    def run_py(self, script, *args):
        return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True,
                              text=True, env=self.env)

    def install_kikolib(self):
        r = self.run_py(WIN / "install.py", "kikolib", "--dest", str(self.apps / "kikolib"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def install(self):
        r = self.run_py(WIN / "install.py", "kiko", "--dest", str(self.dest), "--rex-src", str(self.rex))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_requires_kikolib(self):
        r = self.run_py(WIN / "install.py", "kiko", "--dest", str(self.dest), "--rex-src", str(self.rex))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("kikolib is not installed", r.stdout + r.stderr)
        self.assertFalse(self.dest.exists())

    def test_install_layout_data_uninstall(self):
        self.install_kikolib()
        self.install()
        d = self.dest
        self.assertTrue((d / "kiko-lite" / "kiko_lite" / "cli.py").is_file())
        self.assertFalse((d / "kiko-lite" / "kikolib").exists())  # kikolib comes from apps/kikolib
        self.assertEqual((d / "gateway" / "scripts" / "claude_cli_gateway.py").read_text(), "# fake gateway\n")
        self.assertTrue((d / "gateway" / "runtimelib" / "procutil.py").is_file())
        self.assertTrue((d / "kiko_chain.py").is_file())
        cmd = (d / "kiko-chain.cmd").read_text()
        self.assertIn(f'"{sys.executable}" -I', cmd)  # absolute Python
        self.assertEqual(portable.verify(d), [])
        rec = json.loads((d / "install-record.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["app"], "kiko")
        self.assertFalse([e for e in rec["created"] if e["path"].startswith("data")])
        # data: random tokens and a database with the three Anthropic models only
        for n in ("gwtoken", "masterkey"):
            self.assertGreaterEqual(len((d / "data" / n).read_text().strip()), 32)
        self.assertTrue((d / "data" / "kiko.db").is_file())
        env = dict(self.env, PYTHONPATH=os.pathsep.join([str(d / "kiko-lite"), str(self.apps / "kikolib")]),
                   KIKO_CONFIG_DIR=str(d / "data"), KIKO_DB_PATH=str(d / "data" / "kiko.db"))
        r = subprocess.run([sys.executable, "-P", "-m", "kiko_lite", "list-models"], env=env, cwd=str(d / "data"),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for m in ("haiku", "sonnet", "opus"):
            self.assertIn(m, r.stdout)
        self.assertNotIn("gemini", r.stdout.lower())
        self.assertEqual(portable.verify(d), [])  # reading did not add files
        # reinstall keeps the secrets
        tok = (d / "data" / "gwtoken").read_text()
        self.install()
        self.assertEqual((d / "data" / "gwtoken").read_text(), tok)
        self.assertEqual(self.run_py(d / "uninstall.py").returncode, 0)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["data"])
        self.install()
        self.assertEqual(self.run_py(d / "uninstall.py", "--purge").returncode, 0)
        self.assertFalse(d.exists())

    def test_missing_rex_file_fails_clean(self):
        self.install_kikolib()
        r = self.run_py(WIN / "install.py", "kiko", "--dest", str(self.dest), "--rex-src", str(self.tmp / "nope"))
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.dest.exists())


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
