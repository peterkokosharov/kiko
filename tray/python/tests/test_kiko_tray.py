# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""
Unit tests for tray package (config, control, app).

No pystray, Pillow, or GTK required — only stdlib logic is tested.
Pillow tests self-skip when Pillow is not installed.
"""
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tray.config import Settings, default_config_path, load_settings
from tray.control import KikoControl, KikoProcess, is_port_listening


# ─── config tests ────────────────────────────────────────────────────────────

class TestDefaultConfigPath(unittest.TestCase):

    def test_default_is_kircho_dir(self):
        env = {k: v for k, v in os.environ.items() if k != "KIKO_CONFIG"}
        with patch.dict(os.environ, env, clear=True):
            p = default_config_path()
        self.assertEqual(p, Path.home() / ".kircho" / "kiko-config.yaml")

    def test_env_override(self):
        with patch.dict(os.environ, {"KIKO_CONFIG": r"C:\custom\config.yaml"}):
            p = default_config_path()
        self.assertEqual(p, Path(r"C:\custom\config.yaml"))


class TestLoadSettings(unittest.TestCase):

    def test_defaults_when_no_file(self):
        s = load_settings(Path("/nonexistent/kiko-config.yaml"))
        self.assertEqual(s.port, 4000)
        self.assertEqual(s.kiko_executable, "kiko")
        self.assertIsNone(s.pystray_backend)

    def test_reads_port_from_yaml(self):
        yaml = "kiko_proxy:\n  port: 5001\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            tmp = Path(f.name)
        try:
            s = load_settings(tmp)
            self.assertEqual(s.port, 5001)
        finally:
            tmp.unlink()

    def test_reads_executable_from_yaml(self):
        yaml = "kiko_proxy:\n  executable: /usr/local/bin/kiko\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            tmp = Path(f.name)
        try:
            s = load_settings(tmp)
            self.assertEqual(s.kiko_executable, "/usr/local/bin/kiko")
        finally:
            tmp.unlink()

    def test_invalid_port_falls_back_to_default(self):
        yaml = "kiko_proxy:\n  port: notanumber\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            tmp = Path(f.name)
        try:
            s = load_settings(tmp)
            self.assertEqual(s.port, 4000)
        finally:
            tmp.unlink()

    def test_missing_key_uses_default(self):
        yaml = "other_section:\n  key: value\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            tmp = Path(f.name)
        try:
            s = load_settings(tmp)
            self.assertEqual(s.port, 4000)
            self.assertEqual(s.kiko_executable, "kiko")
        finally:
            tmp.unlink()

    def test_reads_pystray_backend_from_yaml(self):
        yaml = "kiko_proxy:\n  pystray_backend: gtk\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            tmp = Path(f.name)
        try:
            s = load_settings(tmp)
            self.assertEqual(s.pystray_backend, "gtk")
        finally:
            tmp.unlink()


# ─── is_port_listening tests ──────────────────────────────────────────────────

class TestIsPortListening(unittest.TestCase):

    def test_true_when_server_running(self):
        server = socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            self.assertTrue(is_port_listening("127.0.0.1", port))
        finally:
            server.shutdown()
            server.server_close()

    def test_false_when_nothing_listening(self):
        import socket as _socket
        # Bind to get a free port, then close — nothing will be listening
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(is_port_listening("127.0.0.1", port))


# ─── KikoProcess tests ────────────────────────────────────────────────────────

class TestKikoProcess(unittest.TestCase):

    def _settings(self) -> Settings:
        return Settings(config_path=Path("/fake/kiko-config.yaml"), port=4000)

    def test_pid_none_before_start(self):
        proc = KikoProcess()
        self.assertIsNone(proc.pid())

    def test_starts_with_correct_args(self):
        proc = KikoProcess()
        settings = self._settings()
        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            proc.start(settings)
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        self.assertEqual(cmd[0], "kiko")
        self.assertIn("--config", cmd)
        self.assertIn("--port", cmd)
        self.assertIn("4000", cmd)

    def test_process_stored_after_start(self):
        proc = KikoProcess()
        mock_proc = MagicMock()
        with patch("subprocess.Popen", return_value=mock_proc):
            proc.start(self._settings())
        self.assertIs(proc._proc, mock_proc)

    def test_pid_after_start(self):
        proc = KikoProcess()
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        with patch("subprocess.Popen", return_value=mock_proc):
            proc.start(self._settings())
        self.assertEqual(proc.pid(), 12345)

    def test_noop_stop_when_not_started(self):
        proc = KikoProcess()
        proc.stop()  # must not raise

    def test_stop_terminates_process(self):
        proc = KikoProcess()
        mock_proc = MagicMock()
        proc._proc = mock_proc
        proc.stop()
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(proc._proc)

    def test_stop_kills_on_timeout(self):
        proc = KikoProcess()
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("kiko", 5)
        proc._proc = mock_proc
        proc.stop()
        mock_proc.kill.assert_called_once()
        self.assertIsNone(proc._proc)

    def test_proc_cleared_after_stop(self):
        proc = KikoProcess()
        mock_proc = MagicMock()
        proc._proc = mock_proc
        proc.stop()
        self.assertIsNone(proc._proc)


# ─── KikoControl tests ────────────────────────────────────────────────────────

class TestKikoControlIsRunning(unittest.TestCase):

    def _settings(self) -> Settings:
        return Settings(config_path=Path("/fake/kiko-config.yaml"), port=4000)

    def test_false_when_port_not_listening(self):
        ctrl = KikoControl()
        with patch("tray.control.is_port_listening", return_value=False):
            self.assertFalse(ctrl.is_running(self._settings()))

    def test_true_when_port_listening(self):
        ctrl = KikoControl()
        with patch("tray.control.is_port_listening", return_value=True):
            self.assertTrue(ctrl.is_running(self._settings()))


class TestKikoControlStart(unittest.TestCase):

    def _settings(self) -> Settings:
        return Settings(config_path=Path("/fake/kiko-config.yaml"), port=4000)

    def test_noop_when_port_already_listening(self):
        """No double start: if port is busy, Popen must not be called."""
        ctrl = KikoControl()
        with patch("tray.control.is_port_listening", return_value=True):
            with patch("subprocess.Popen") as mock_popen:
                ctrl.start(self._settings())
            mock_popen.assert_not_called()

    def test_raises_file_not_found_when_executable_missing(self):
        ctrl = KikoControl()
        with patch("tray.control.is_port_listening", return_value=False):
            with patch("tray.control.shutil.which", return_value=None):
                with self.assertRaises(FileNotFoundError):
                    ctrl.start(self._settings())

    def test_starts_process_when_port_free_and_exe_found(self):
        ctrl = KikoControl()
        with patch("tray.control.is_port_listening", return_value=False):
            with patch("tray.control.shutil.which", return_value="/usr/bin/kiko"):
                with patch("subprocess.Popen") as mock_popen:
                    mock_popen.return_value = MagicMock()
                    ctrl.start(self._settings())
                mock_popen.assert_called_once()


# ─── KikoTray (app) status text ──────────────────────────────────────────────

class TestKikoTrayStatusText(unittest.TestCase):

    def test_stopped_text(self):
        from tray.app import KikoTray
        tray = KikoTray()
        with patch("tray.control.is_port_listening", return_value=False):
            self.assertIn("Stopped", tray._status_text())

    def test_running_text(self):
        from tray.app import KikoTray
        tray = KikoTray()
        with patch("tray.control.is_port_listening", return_value=True):
            self.assertIn("Running", tray._status_text())


# ─── _make_icon (Pillow, optional) ───────────────────────────────────────────

class TestMakeIcon(unittest.TestCase):

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def test_returns_correct_size_32(self):
        from tray.app import _make_icon
        img = _make_icon(32)
        self.assertEqual(img.size, (32, 32))

    def test_returns_correct_size_16(self):
        from tray.app import _make_icon
        img = _make_icon(16)
        self.assertEqual(img.size, (16, 16))

    def test_mode_is_rgba(self):
        from tray.app import _make_icon
        img = _make_icon(32)
        self.assertEqual(img.mode, "RGBA")

    def test_background_is_dark(self):
        from tray.app import _make_icon
        img = _make_icon(32)
        r, g, b, a = img.getpixel((0, 0))
        self.assertLess(r, 80)
        self.assertLess(g, 80)
        self.assertLess(b, 80)


if __name__ == "__main__":
    unittest.main()
