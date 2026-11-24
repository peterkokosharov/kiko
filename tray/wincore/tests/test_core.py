# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Run: python -I -m unittest discover -s tray/wincore/tests -t tray/wincore"""
import http.server
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kiko_tray_core as core  # noqa: E402

FAKE_CHAIN = """import os, sys, time
from pathlib import Path
d = Path(__file__).parent / 'data'
if '--stop' in sys.argv:
    import signal
    pid = int((d / 'chain.pid').read_text().split()[0])
    (d / 'chain.pid').unlink()
    os.kill(pid, signal.SIGTERM)
    print('stopped')
else:
    d.mkdir(exist_ok=True)
    (d / 'chain.pid').write_text(str(os.getpid()) + '\\n0\\n')
    time.sleep(60)
"""


class Ini(unittest.TestCase):
    def test_parse(self):
        text = "\r\n".join(["# c", 'python = "C:/p y/python.exe"', "port = 4001", "; x", "token_file = C:/t", ""])
        d = core.parse_ini(text)
        self.assertEqual(d["python"], "C:/p y/python.exe")
        self.assertEqual(d["port"], "4001")
        self.assertEqual(d["token_file"], "C:/t")

    def test_config_defaults_and_env(self):
        self.assertEqual(core.load_config(None, {}).port, 4000)
        self.assertEqual(core.load_config(None, {"KIKO_PORT": "4100"}).port, 4100)
        self.assertEqual(core.load_config(None, {"KIKO_PORT": "junk"}).port, 4000)
        self.assertEqual(core.load_config(None, {"KIKO_PORT": "99999"}).port, 4000)

    def test_config_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / core.INI_NAME).write_text(core.tray_ini_text("py", "chain.py", "tok", 4005), encoding="utf-8")
            c = core.load_config(Path(td), {})
            self.assertEqual((c.python, c.chain, c.port, c.token_file), ("py", "chain.py", 4005, "tok"))
            self.assertEqual(c.base, "http://127.0.0.1:4005")

    def test_token(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "api-token"
            c = core.Config(token_file=str(f))
            self.assertEqual(core.read_token(c, {}), "")
            f.write_text("abc\nsecond\n", encoding="utf-8")
            self.assertEqual(core.read_token(c, {}), "abc")
            self.assertEqual(core.read_token(c, {"KIKO_API_TOKEN": " zz "}), "zz")


class Chain(unittest.TestCase):
    def test_commands(self):
        c = core.Config(python="py", chain="k.py")
        self.assertEqual(core.chain_command(c, "start"), ["py", "-I", "k.py"])
        self.assertEqual(core.chain_command(c, "stop"), ["py", "-I", "k.py", "--stop"])
        with self.assertRaises(ValueError):
            core.chain_command(core.Config(), "start")
        with self.assertRaises(ValueError):
            core.chain_command(c, "restart")

    def test_not_configured(self):
        ok, msg = core.ChainOwner(core.Config()).run("start")
        self.assertFalse(ok)
        self.assertIn("no chain", msg)

    def fake_chain(self, td):
        """A kiko_chain.py stand-in: run mode writes data/chain.pid and waits; --stop kills that pid."""
        s = Path(td) / "kiko_chain.py"
        s.write_text(FAKE_CHAIN, encoding="utf-8")
        return core.Config(python=sys.executable, chain=str(s), port=1)

    def wait_pidfile(self, cfg, pid):
        for _ in range(100):
            if core.pidfile_pid(cfg) == pid:
                return True
            time.sleep(0.1)
        return False

    def test_stop_own_child(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.fake_chain(td)
            o = core.ChainOwner(cfg)
            with mock.patch.object(core, "health", return_value=False):
                self.assertEqual(o.run("start"), (True, "starting"))
            self.assertTrue(self.wait_pidfile(cfg, o.proc.pid))
            self.assertTrue(o.owns())
            ok, msg = o.run("stop")
            self.assertTrue(ok, msg)
            self.assertFalse(o.owns())

    def test_outside_chain_not_stopped(self):
        o = core.ChainOwner(core.Config(python="py", chain="k.py"))
        with mock.patch.object(core, "health", return_value=True),                 mock.patch.object(core.subprocess, "run") as run:
            ok, msg = o.run("stop")
        self.assertFalse(ok)
        self.assertEqual(msg, "kiko was started outside the tray")
        run.assert_not_called()

    def test_start_refused_when_already_running(self):
        o = core.ChainOwner(core.Config(python="py", chain="k.py"))
        with mock.patch.object(core, "health", return_value=True),                 mock.patch.object(core.subprocess, "Popen") as popen:
            self.assertFalse(o.run("start")[0])
        popen.assert_not_called()

    def test_pidfile_naming_other_pid_refused(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.fake_chain(td)
            o = core.ChainOwner(cfg)
            with mock.patch.object(core, "health", return_value=False):
                o.run("start")
            self.assertTrue(self.wait_pidfile(cfg, o.proc.pid))
            (Path(td) / "data" / "chain.pid").write_text("999999\n0\n")
            ok, msg = o.run("stop")
            self.assertFalse(ok)
            self.assertIn("nothing killed", msg)
            self.assertTrue(o.owns())
            o.quit()  # own child is still ended on quit
            self.assertFalse(o.owns())

    def test_quit_without_child_is_noop(self):
        with mock.patch.object(core.subprocess, "run") as run:
            core.ChainOwner(core.Config()).quit()
        run.assert_not_called()


class Health(unittest.TestCase):
    def serve(self, body: bytes):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_ok(self):
        self.assertEqual(core.state(self.serve(b'{"status":"ok"}')), "ok")

    def test_not_ok_body(self):
        self.assertEqual(core.state(self.serve(b'{"status":"bad"}')), "down")
        self.assertEqual(core.state(self.serve(b"not json")), "down")
        self.assertEqual(core.state(self.serve(b"[1]")), "down")

    def test_down(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(core.state(port, 0.5), "down")


if __name__ == "__main__":
    unittest.main()
