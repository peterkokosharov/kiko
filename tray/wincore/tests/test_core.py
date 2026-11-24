# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-5-5
"""Run: python -I -m unittest discover -s tray/wincore/tests -t tray/wincore"""
import http.server
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kiko_tray_core as core  # noqa: E402


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
        self.assertEqual(core.chain_command(c, "start"), ["py", "-I", "k.py", "--detach"])
        self.assertEqual(core.chain_command(c, "stop"), ["py", "-I", "k.py", "--stop"])
        with self.assertRaises(ValueError):
            core.chain_command(core.Config(), "start")
        with self.assertRaises(ValueError):
            core.chain_command(c, "restart")

    def test_run_chain_not_configured(self):
        ok, msg = core.run_chain(core.Config(), "start")
        self.assertFalse(ok)
        self.assertIn("no chain", msg)

    def test_run_chain_runs_script(self):
        with tempfile.TemporaryDirectory() as td:
            s = Path(td) / "k.py"
            s.write_text("import sys\nprint('hello', sys.argv[1])\n", encoding="utf-8")
            ok, msg = core.run_chain(core.Config(python=sys.executable, chain=str(s)), "stop")
            self.assertTrue(ok)
            self.assertEqual(msg, "hello --stop")


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
