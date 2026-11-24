# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Claude Sonnet 5.5
"""Run: python -I -m unittest discover -s tools/win/tests -t tools/win  (or run this file directly)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

WIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WIN))
import install  # noqa: E402
import kiko_chain  # noqa: E402
import portable  # noqa: E402
import uninstall  # noqa: E402
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

    def test_reinstall_with_modified_file_aborts_intact(self):
        self.install()
        changed = self.dest / "kikolib" / "config.py"
        changed.write_text("# edited by user\n")
        before = tree(self.dest)
        r = self.run_py(WIN / "install.py", "kikolib", "--dest", str(self.dest))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nothing touched", r.stdout)
        self.assertEqual(tree(self.dest), before)  # nothing removed, nothing added
        self.assertEqual(changed.read_text(), "# edited by user\n")

    def test_reinstall_with_bad_dir_or_shortcut_entry_aborts_intact(self):
        self.install()
        recp = self.dest / "install-record.json"
        rec = json.loads(recp.read_text(encoding="utf-8"))
        rec["created"] += [{"path": "../evil", "kind": "dir", "sha256": ""},
                           {"path": "../../x.lnk", "kind": "shortcut", "sha256": ""}]
        recp.write_text(json.dumps(rec), encoding="utf-8")
        before, rec_before = tree(self.dest), recp.read_bytes()
        r = self.run_py(WIN / "install.py", "kikolib", "--dest", str(self.dest))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nothing touched", r.stdout)
        self.assertEqual(r.stdout.count("unsafe path"), 2, r.stdout)
        self.assertEqual(tree(self.dest), before)
        self.assertEqual(recp.read_bytes(), rec_before)

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


class Confinement(unittest.TestCase):
    """Record entries are not trusted: nothing outside the app folder (or the Start Menu) is touched."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.app = self.tmp / "apps" / "x"
        self.app.mkdir(parents=True)
        self.outside = self.tmp / "apps" / "victim.txt"
        self.outside.write_text("precious")
        self.appdata = self.tmp / "appdata"
        self.appdata.mkdir()
        self._env = mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._td.cleanup()

    def record(self, created):
        (self.app / "install-record.json").write_text(json.dumps({"app": "x", "created": created}), encoding="utf-8")

    def run_uninstall(self):
        lines = []
        rc = uninstall.uninstall(self.app, False, lines.append)
        return rc, "\n".join(lines)

    def test_inside_rejects(self):
        for bad in ("", ".", "..", "../victim.txt", "a/../../victim.txt", "/etc/passwd", "C:/x", "C:x",
                    "\\\\host\\share\\f", "a/..", None):
            self.assertIsNone(uninstall._inside(self.app, bad), bad)
        self.assertEqual(uninstall._inside(self.app, "a/b.txt"), self.app / "a" / "b.txt")

    def test_traversal_absolute_and_empty_sha_skipped(self):
        keep = self.app / "keep.txt"
        keep.write_text("no sha")
        sha = portable.sha256_bytes(b"precious")
        self.record([
            {"path": "../victim.txt", "kind": "file", "sha256": sha},
            {"path": str(self.outside), "kind": "file", "sha256": sha},
            {"path": "keep.txt", "kind": "file", "sha256": ""},
            {"path": ".", "kind": "dir", "sha256": ""},
            {"path": "..", "kind": "dir", "sha256": ""},
            {"path": "../../x.lnk", "kind": "shortcut", "sha256": ""},
        ])
        (self.tmp / "apps" / "__pycache__").mkdir()  # an rglob on '..' would delete this
        (self.app / "__pycache__").mkdir()
        rc, out = self.run_uninstall()
        self.assertEqual(rc, 1, out)
        self.assertEqual(out.count("SKIPPED"), 6, out)
        self.assertEqual(self.outside.read_text(), "precious")
        self.assertTrue(keep.is_file())
        self.assertTrue((self.tmp / "apps" / "__pycache__").is_dir())
        self.assertTrue((self.app / "__pycache__").is_dir())
        self.assertTrue((self.app / "install-record.json").is_file())  # kept: not clean

    def test_shortcut_outside_start_menu_skipped(self):
        stray = self.appdata / "stray.txt"
        stray.write_text("x")
        self.record([{"path": "stray.txt", "kind": "shortcut", "sha256": ""}])
        rc, out = self.run_uninstall()
        self.assertEqual(rc, 1, out)
        self.assertTrue(stray.is_file())

    def test_verify_flags_bad_entries(self):
        self.record([{"path": "../victim.txt", "kind": "file", "sha256": "ab"},
                     {"path": "k.txt", "kind": "file", "sha256": ""}])
        (self.app / "k.txt").write_text("x")
        probs = portable.verify(self.app)
        self.assertTrue(any(p.startswith("unsafe path") for p in probs), probs)
        self.assertTrue(any(p.startswith("no sha256") for p in probs), probs)

    def test_rollback_confined(self):
        inst = portable.Installation("x", "1", self.app)
        inst.created.append({"path": "../victim.txt", "kind": "file", "sha256": ""})
        inst.rollback()
        self.assertEqual(self.outside.read_text(), "precious")

    def test_symlinked_entry_skipped(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "f.txt").write_text("precious")
        try:
            os.symlink(target, self.app / "link", target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            made = sys.platform == "win32" and subprocess.run(  # a junction needs no privilege
                ["cmd", "/c", "mklink", "/J", str(self.app / "link"), str(target)], capture_output=True).returncode == 0
            if not made:
                self.skipTest("cannot create symlinks here")
        sha = portable.sha256_bytes(b"precious")
        self.record([{"path": "link", "kind": "dir", "sha256": ""},
                     {"path": "link/f.txt", "kind": "file", "sha256": sha}])
        rc, out = self.run_uninstall()
        self.assertEqual(rc, 1, out)
        self.assertEqual((target / "f.txt").read_text(), "precious")
        self.assertTrue(target.is_dir())


class PurgeLink(unittest.TestCase):
    def test_purge_skips_linked_data(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            app, real = tmp / "app", tmp / "real"
            app.mkdir()
            real.mkdir()
            (real / "keep.txt").write_text("precious")
            try:
                os.symlink(real, app / "data", target_is_directory=True)
            except (OSError, NotImplementedError):
                if not (sys.platform == "win32" and subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(app / "data"), str(real)],
                        capture_output=True).returncode == 0):
                    self.skipTest("cannot create links here")
            (app / "install-record.json").write_text(json.dumps({"created": []}), encoding="utf-8")
            lines = []
            uninstall.uninstall(app, True, lines.append)
            self.assertEqual((real / "keep.txt").read_text(), "precious")
            self.assertTrue(any("SKIPPED data" in x for x in lines), lines)


class FinishFailure(unittest.TestCase):
    def test_failure_in_finish_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "apps" / "fake"

            def fake_target(d):
                inst = portable.Installation("fake", "1", d)
                inst.add_file("sub/a.txt", b"a")
                return inst

            with mock.patch.dict(install.TARGETS, {"fake": fake_target}), \
                    mock.patch.object(portable.Installation, "finish", side_effect=OSError("disk full")), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError):
                    install.main(["fake", "--dest", str(dest)])
            self.assertFalse(dest.exists(), tree(dest))

    def test_failure_in_verify_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "apps" / "fake"

            def fake_target(d):
                inst = portable.Installation("fake", "1", d)
                inst.add_file("sub/a.txt", b"a")
                return inst

            with mock.patch.dict(install.TARGETS, {"fake": fake_target}), \
                    mock.patch.object(portable, "verify", return_value=["missing: a.txt"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(install.main(["fake", "--dest", str(dest)]), 1)
            self.assertFalse(dest.exists(), tree(dest))


class ChainStop(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.pidfile = Path(self._td.name) / "chain.pid"
        self._p = mock.patch.object(kiko_chain, "PIDFILE", self.pidfile)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._td.cleanup()

    def stop(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            return kiko_chain.stop(), err.getvalue()

    def test_other_kiko_chain_path_not_killed(self):
        self.pidfile.write_text("4242\n")
        with mock.patch.object(kiko_chain, "proc_cmdline",
                               return_value=("python.exe", "python -I D:\\elsewhere\\kiko_chain.py")), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 1)
        kill.assert_not_called()

    def test_start_time_mismatch_not_killed(self):
        self.pidfile.write_text("4242\n111\n")
        with mock.patch.object(kiko_chain, "proc_cmdline",
                               return_value=("python.exe", str(kiko_chain.HERE / "kiko_chain.py"))), \
                mock.patch.object(kiko_chain, "proc_start", return_value="222"), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 1)
        kill.assert_not_called()

    def test_foreign_process_not_killed(self):
        self.pidfile.write_text("4242\n")
        with mock.patch.object(kiko_chain, "proc_cmdline", return_value=("python.exe", "python other.py")), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            rc, err = self.stop()
        self.assertEqual(rc, 1)
        self.assertIn("NOT stopping", err)
        kill.assert_not_called()
        self.assertTrue(self.pidfile.is_file())

    def test_not_python_not_killed(self):
        self.pidfile.write_text("4242\n")
        with mock.patch.object(kiko_chain, "proc_cmdline", return_value=("notepad.exe", "x kiko_chain.py")), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 1)
        kill.assert_not_called()

    def test_our_chain_is_killed(self):
        self.pidfile.write_text("4242\n")
        with mock.patch.object(kiko_chain, "proc_cmdline",
                               return_value=("python.exe", f'"python.exe" -I "{kiko_chain.HERE / "kiko_chain.py"}"')), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 0)
        kill.assert_called_once_with(4242)
        self.assertFalse(self.pidfile.exists())

    def test_unverifiable_without_start_time_refused(self):
        self.pidfile.write_text("4242\n")  # old format: no creation time
        with mock.patch.object(kiko_chain, "proc_cmdline", return_value=None), \
                mock.patch.object(kiko_chain, "proc_image", return_value="python.exe"), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 1)
        kill.assert_not_called()

    def test_fallback_with_matching_start_time(self):
        self.pidfile.write_text("4242\n999\n")
        with mock.patch.object(kiko_chain, "proc_cmdline", return_value=None), \
                mock.patch.object(kiko_chain, "proc_image", return_value="python.exe"), \
                mock.patch.object(kiko_chain, "proc_start", return_value="999"), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 0)
        kill.assert_called_once_with(4242)

    def test_stale_pid_removed(self):
        self.pidfile.write_text("4242\n")
        with mock.patch.object(kiko_chain, "proc_cmdline", return_value=None), \
                mock.patch.object(kiko_chain, "proc_image", return_value=None), \
                mock.patch.object(kiko_chain, "kill_tree") as kill:
            self.assertEqual(self.stop()[0], 0)
        kill.assert_not_called()
        self.assertFalse(self.pidfile.exists())

    def test_real_foreign_python_survives(self):
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        try:
            self.pidfile.write_text(f"{child.pid}\n")
            rc, err = self.stop()
            self.assertEqual(rc, 1, err)
            self.assertIsNone(child.poll())  # still running
        finally:
            child.kill()
            child.wait()


@unittest.skipUnless(sys.platform == "win32", "COM shortcuts are Windows only")
class Lnk(unittest.TestCase):
    def test_url_fallback(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            lnk = Path(td) / "Demo.lnk"
            with mock.patch.object(winlnk, "_com_write", side_effect=OSError("no COM")), \
                    self.assertLogs("winlnk", "WARNING"):
                res = winlnk.create_shortcut(lnk, Path(sys.executable))
            self.assertEqual(res["kind"], "url", res)
            self.assertTrue(res["path"].endswith(".url"))
            self.assertTrue(Path(res["path"]).is_file())
            self.assertFalse(lnk.exists())
            self.assertEqual(winlnk.read_shortcut(res["path"])["kind"], "url")

    def test_roundtrip(self):
        # ignore_cleanup_errors: Windows may hold the fresh .lnk briefly (scanner/shell) after COM release
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
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
