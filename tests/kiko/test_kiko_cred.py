# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Sonnet
"""
Unit tests for KIKO credential manager integration.
Covers: _cred_get, _resolve_env with Credential Manager fallback.
No real Credential Manager calls — ctypes is mocked throughout.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_KIKO_ROOT = Path(__file__).parents[2]
if str(_KIKO_ROOT) not in sys.path:
    sys.path.insert(0, str(_KIKO_ROOT))

from litellm.proxy.kiko_server import _cred_get, _resolve_env


class TestCredGet(unittest.TestCase):

    def test_returns_empty_on_non_windows(self):
        with patch("litellm.proxy.kiko_server.sys") as mock_sys:
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_returns_empty_when_cred_not_found(self):
        with patch("litellm.proxy.kiko_server.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("ctypes.windll") as mock_windll:
                mock_windll.advapi32.CredReadW.return_value = 0
                self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_returns_empty_on_exception_during_read(self):
        # CredReadW returns 1 (found) but subsequent ctypes call raises
        with patch("litellm.proxy.kiko_server.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("ctypes.windll") as mock_windll:
                mock_windll.advapi32.CredReadW.return_value = 1
                mock_windll.advapi32.CredFree.return_value = None
                with patch("ctypes.c_void_p", side_effect=Exception("boom")):
                    self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_uses_uppercase_var_name(self):
        """Target path must be KIKO/KIKO_IDENTITY/<UPPER>."""
        calls = []

        def fake_cred_read(target, ctype, flags, ptr):
            calls.append(target)
            return 0  # not found — we just capture the target

        with patch("litellm.proxy.kiko_server.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("ctypes.windll") as mock_windll:
                mock_windll.advapi32.CredReadW.side_effect = fake_cred_read
                _cred_get("gemini_api_key")

        self.assertTrue(any("KIKO/KIKO_IDENTITY/GEMINI_API_KEY" in str(c) for c in calls))

    def test_returns_empty_on_exception(self):
        with patch("litellm.proxy.kiko_server.sys") as mock_sys:
            mock_sys.platform = "win32"
            with patch("ctypes.windll", side_effect=Exception("ctypes broken")):
                self.assertEqual(_cred_get("ANY_KEY"), "")


class TestResolveEnv(unittest.TestCase):

    def test_returns_literal_when_no_env_ref(self):
        self.assertEqual(_resolve_env("gemini/gemini-flash"), "gemini/gemini-flash")

    def test_returns_env_var_when_set(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}):
            self.assertEqual(_resolve_env("os.environ/GEMINI_API_KEY"), "from-env")

    def test_env_var_takes_priority_over_cred(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "env-wins"}):
            with patch("litellm.proxy.kiko_server._cred_get", return_value="cred-value"):
                self.assertEqual(_resolve_env("os.environ/GEMINI_API_KEY"), "env-wins")

    def test_falls_back_to_cred_when_env_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("litellm.proxy.kiko_server._cred_get", return_value="cred-key"):
                self.assertEqual(_resolve_env("os.environ/GEMINI_API_KEY"), "cred-key")

    def test_returns_empty_when_neither_env_nor_cred(self):
        env = {k: v for k, v in os.environ.items() if k != "MISSING_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with patch("litellm.proxy.kiko_server._cred_get", return_value=""):
                self.assertEqual(_resolve_env("os.environ/MISSING_KEY"), "")


class TestCredGetLinux(unittest.TestCase):
    """Tests for _cred_get non-Windows (Linux) credential chain.

    Covers secret-tool (GNOME Keyring/libsecret) and dcop kwalletd (TDE) backends.
    All tests mock sys.platform='linux' and manage DBUS_SESSION_BUS_ADDRESS via
    patch.dict so the real environment is never touched.
    """

    DBUS_ENV = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}

    def _make_result(self, returncode=0, stdout=""):
        """Return a mock CompletedProcess-like object."""
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_secret_tool_found(self):
        """DBUS set, secret-tool on PATH, returns the credential value."""
        mock_run = MagicMock(return_value=self._make_result(0, "s3cr3t\n"))
        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which",
                   side_effect=lambda x: "/usr/bin/secret-tool" if x == "secret-tool" else None), \
             patch("litellm.proxy.kiko_server.subprocess.run", mock_run), \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "s3cr3t")
        mock_run.assert_called_once_with(
            ["secret-tool", "lookup", "kiko-identity", "GEMINI_API_KEY"],
            capture_output=True, text=True, timeout=3,
        )

    def test_secret_tool_not_found_empty_output(self):
        """DBUS set, secret-tool returns empty stdout → falls through → dcop missing → ''."""
        def which_map(name):
            return "/usr/bin/secret-tool" if name == "secret-tool" else None

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run",
                   return_value=self._make_result(0, "")), \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_secret_tool_returncode_nonzero(self):
        """secret-tool returncode=1 → falls through → dcop not available → ''."""
        def which_map(name):
            return "/usr/bin/secret-tool" if name == "secret-tool" else None

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run",
                   return_value=self._make_result(1, "")), \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_secret_tool_timeout(self):
        """secret-tool raises TimeoutExpired → falls through → dcop not available → ''."""
        def which_map(name):
            return "/usr/bin/secret-tool" if name == "secret-tool" else None

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["secret-tool"], 3)), \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_no_dbus_skips_secret_tool_only(self):
        """DBUS_SESSION_BUS_ADDRESS not set → secret-tool skipped; dcop also not on PATH → ''."""
        env_without_dbus = {k: v for k, v in os.environ.items()
                            if k != "DBUS_SESSION_BUS_ADDRESS"}
        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", return_value=None), \
             patch("litellm.proxy.kiko_server.subprocess.run") as mock_run, \
             patch.dict(os.environ, env_without_dbus, clear=True):
            mock_sys.platform = "linux"
            result = _cred_get("GEMINI_API_KEY")
        self.assertEqual(result, "")
        mock_run.assert_not_called()

    def test_secret_tool_not_on_path_skips_it(self):
        """DBUS set, shutil.which('secret-tool')=None → skip secret-tool, dcop also missing → ''."""
        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", return_value=None), \
             patch("litellm.proxy.kiko_server.subprocess.run") as mock_run, \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")
        mock_run.assert_not_called()

    def test_dcop_found(self):
        """No secret-tool, dcop available, open→'42', read→'my-api-key'. No DBUS required."""
        def which_map(name):
            return "/usr/bin/dcop" if name == "dcop" else None

        def run_side_effect(cmd, **kwargs):
            op = cmd[2]
            if op == "open":
                return self._make_result(0, "42\n")
            elif op == "readPassword":
                return self._make_result(0, "my-api-key\n")
            else:  # close
                return self._make_result(0, "")

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run", side_effect=run_side_effect), \
             patch.dict(os.environ, {}, clear=True):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("MY_KEY"), "my-api-key")

    def test_dcop_open_returns_negative_one(self):
        """dcop open returns '-1' → handle invalid → skip read → ''. No DBUS required."""
        def which_map(name):
            return "/usr/bin/dcop" if name == "dcop" else None

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run",
                   return_value=self._make_result(0, "-1\n")), \
             patch.dict(os.environ, {}, clear=True):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_dcop_open_returncode_nonzero(self):
        """dcop open returncode=1 → skip read → ''. No DBUS required."""
        def which_map(name):
            return "/usr/bin/dcop" if name == "dcop" else None

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run",
                   return_value=self._make_result(1, "")), \
             patch.dict(os.environ, {}, clear=True):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_dcop_open_invalid_handle(self):
        """dcop open returns non-integer stdout → ValueError → skip read → ''. No DBUS required."""
        def which_map(name):
            return "/usr/bin/dcop" if name == "dcop" else None

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run",
                   return_value=self._make_result(0, "not-an-int\n")), \
             patch.dict(os.environ, {}, clear=True):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_dcop_close_called_in_finally(self):
        """close is called in finally even when read raises; close itself raising is swallowed."""
        def which_map(name):
            return "/usr/bin/dcop" if name == "dcop" else None

        close_calls = []

        def run_side_effect(cmd, **kwargs):
            op = cmd[2]
            if op == "open":
                return self._make_result(0, "42\n")
            elif op == "readPassword":
                raise subprocess.TimeoutExpired(cmd, 3)
            else:  # close — also raises to verify the exception is swallowed
                close_calls.append(list(cmd))
                raise subprocess.TimeoutExpired(cmd, 3)

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run", side_effect=run_side_effect), \
             patch.dict(os.environ, {}, clear=True):
            mock_sys.platform = "linux"
            result = _cred_get("GEMINI_API_KEY")

        self.assertEqual(result, "")
        self.assertEqual(len(close_calls), 1, "close should be called exactly once")
        self.assertEqual(close_calls[0][2], "close")

    def test_dcop_read_empty_output(self):
        """dcop read succeeds but returns empty stdout → ''."""
        def which_map(name):
            return "/usr/bin/dcop" if name == "dcop" else None

        def run_side_effect(cmd, **kwargs):
            op = cmd[2]
            if op == "open":
                return self._make_result(0, "99\n")
            elif op == "readPassword":
                return self._make_result(0, "")
            else:  # close
                return self._make_result(0, "")

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run", side_effect=run_side_effect), \
             patch.dict(os.environ, {}, clear=True):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "")

    def test_secret_tool_wins_over_dcop(self):
        """Both backends available; secret-tool finds value → dcop is never called."""
        def which_map(name):
            return f"/usr/bin/{name}"

        call_log = []

        def run_side_effect(cmd, **kwargs):
            call_log.append(cmd[0])
            if cmd[0] == "secret-tool":
                return self._make_result(0, "winner\n")
            raise AssertionError("dcop should not be called when secret-tool succeeds")

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run", side_effect=run_side_effect), \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            self.assertEqual(_cred_get("GEMINI_API_KEY"), "winner")
        self.assertNotIn("dcop", call_log)

    def test_uppercase_key_name(self):
        """Subprocess calls must use UPPER-cased key name (nvidia_api_key → NVIDIA_API_KEY)."""
        def which_map(name):
            return "/usr/bin/secret-tool" if name == "secret-tool" else None

        run_calls = []

        def run_side_effect(cmd, **kwargs):
            run_calls.append(list(cmd))
            return self._make_result(0, "val\n")

        with patch("litellm.proxy.kiko_server.sys") as mock_sys, \
             patch("litellm.proxy.kiko_server.shutil.which", side_effect=which_map), \
             patch("litellm.proxy.kiko_server.subprocess.run", side_effect=run_side_effect), \
             patch.dict(os.environ, self.DBUS_ENV):
            mock_sys.platform = "linux"
            _cred_get("nvidia_api_key")

        self.assertTrue(run_calls, "subprocess.run should have been called at least once")
        self.assertEqual(
            run_calls[0],
            ["secret-tool", "lookup", "kiko-identity", "NVIDIA_API_KEY"],
        )


if __name__ == "__main__":
    unittest.main()
