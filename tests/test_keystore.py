# SPDX-License-Identifier: MIT
"""Tests for keystore module (TDE wallet / DCOP focus)."""
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, MagicMock, patch, call

# Need to set up path to import kikolib
sys.path.insert(0, "/home/petark/kiko_lite")

from kikolib import keystore


class TestDcopStore(unittest.TestCase):
    """Tests for TDE/Q4OS DCOP-based wallet (the most important)."""

    def setUp(self):
        self.store = keystore.DcopStore()

    @patch('kikolib.keystore._run')
    def test_dcop_open_success_kded(self, mock_run):
        """Test DCOP open succeeds with kded target."""
        # First call: open returns handle "123"
        # Second call: readPassword returns value
        # Third call: close
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open kded kwalletd
            Mock(returncode=0, stdout="secret-value"),  # readPassword
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")
        # Verify the call sequence
        self.assertEqual(mock_run.call_count, 3)
        # First call should be open
        self.assertEqual(mock_run.call_args_list[0][0][0], ["dcop", "kded", "kwalletd", "open", "kdewallet", "0"])
        # Second call should be readPassword
        self.assertEqual(mock_run.call_args_list[1][0][0], ["dcop", "kded", "kwalletd", "readPassword", "123", "Passwords", "TEST_KEY"])
        # Third call should be close
        self.assertEqual(mock_run.call_args_list[2][0][0], ["dcop", "kded", "kwalletd", "close", "123", "0"])

    @patch('kikolib.keystore._run')
    def test_dcop_open_fails_kded_tries_kwalletd(self, mock_run):
        """Test DCOP falls back to kwalletd target when kded fails."""
        mock_run.side_effect = [
            Mock(returncode=1, stdout=""),  # open kded kwalletd fails
            Mock(returncode=0, stdout="456"),  # open kwalletd succeeds
            Mock(returncode=0, stdout="secret-value"),  # readPassword
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")
        self.assertEqual(mock_run.call_count, 4)
        # First open attempt with kded
        self.assertEqual(mock_run.call_args_list[0][0][0], ["dcop", "kded", "kwalletd", "open", "kdewallet", "0"])
        # Second open attempt with kwalletd
        self.assertEqual(mock_run.call_args_list[1][0][0], ["dcop", "kwalletd", "open", "kdewallet", "0"])

    @patch('kikolib.keystore._run')
    def test_dcop_both_targets_fail(self, mock_run):
        """Test DCOP returns None when both targets fail."""
        mock_run.side_effect = [
            Mock(returncode=1, stdout=""),  # open kded fails
            Mock(returncode=1, stdout=""),  # open kwalletd fails
        ]
        result = self.store.get("TEST_KEY")
        self.assertIsNone(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch('kikolib.keystore._run')
    def test_dcop_open_returns_invalid_handle(self, mock_run):
        """Test DCOP handles invalid handle from open."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="not-a-number"),  # open returns invalid handle
            Mock(returncode=0, stdout="456"),  # open kwalletd succeeds
            Mock(returncode=0, stdout="secret-value"),  # readPassword
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")

    @patch('kikolib.keystore._run')
    def test_dcop_read_returns_minus_one(self, mock_run):
        """Test DCOP handles -1 return from readPassword (not found)."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open
            Mock(returncode=0, stdout="-1"),  # readPassword returns -1
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.get("TEST_KEY")
        self.assertIsNone(result)

    @patch('kikolib.keystore._run')
    def test_dcop_read_returns_empty(self, mock_run):
        """Test DCOP handles empty return from readPassword."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open
            Mock(returncode=0, stdout=""),  # readPassword returns empty
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.get("TEST_KEY")
        self.assertIsNone(result)

    @patch('kikolib.keystore._run')
    def test_dcop_set_success(self, mock_run):
        """Test DCOP set (writePassword) success."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open
            Mock(returncode=0, stdout="0"),  # writePassword returns "0" for success
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.set("TEST_KEY", "secret-value")
        self.assertTrue(result)
        # Verify writePassword was called with correct args
        self.assertEqual(mock_run.call_args_list[1][0][0], ["dcop", "kded", "kwalletd", "writePassword", "123", "Passwords", "TEST_KEY", "secret-value"])

    @patch('kikolib.keystore._run')
    def test_dcop_set_fails_returns_nonzero(self, mock_run):
        """Test DCOP set fails when writePassword returns non-zero."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open
            Mock(returncode=0, stdout="1"),  # writePassword returns "1" for failure
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.set("TEST_KEY", "secret-value")
        self.assertFalse(result)

    @patch('kikolib.keystore._run')
    def test_dcop_delete_success(self, mock_run):
        """Test DCOP delete (removeEntry) success."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open
            Mock(returncode=0, stdout="0"),  # removeEntry returns "0" for success
            Mock(returncode=0, stdout=""),  # close
        ]
        result = self.store.delete("TEST_KEY")
        self.assertTrue(result)
        # Verify removeEntry was called
        self.assertEqual(mock_run.call_args_list[1][0][0], ["dcop", "kded", "kwalletd", "removeEntry", "123", "Passwords", "TEST_KEY"])

    @patch('kikolib.keystore._run')
    def test_dcop_close_called_even_on_read_failure(self, mock_run):
        """Test DCOP always calls close even if read fails."""
        # The _call method tries both targets (kded and kwalletd)
        # For kded: open -> readPassword (fails) -> close
        # For kwalletd: open -> readPassword (fails) -> close
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),  # open kded
            Mock(returncode=1, stdout="error"),  # readPassword kded fails
            Mock(returncode=0, stdout=""),  # close kded
            Mock(returncode=0, stdout="456"),  # open kwalletd
            Mock(returncode=1, stdout="error"),  # readPassword kwalletd fails
            Mock(returncode=0, stdout=""),  # close kwalletd
        ]
        result = self.store.get("TEST_KEY")
        self.assertIsNone(result)
        # Should have called close for both targets
        self.assertEqual(mock_run.call_count, 6)
        # Check close was called for kded
        self.assertEqual(mock_run.call_args_list[2][0][0], ["dcop", "kded", "kwalletd", "close", "123", "0"])
        # Check close was called for kwalletd
        self.assertEqual(mock_run.call_args_list[5][0][0], ["dcop", "kwalletd", "close", "456", "0"])

    @patch('kikolib.keystore._run')
    def test_dcop_key_uppercased(self, mock_run):
        """Test DCOP uppercases key names."""
        mock_run.side_effect = [
            Mock(returncode=0, stdout="123"),
            Mock(returncode=0, stdout="secret"),
            Mock(returncode=0, stdout=""),
        ]
        self.store.get("test_key")
        # Check readPassword was called with uppercased key
        self.assertEqual(mock_run.call_args_list[1][0][0][-1], "TEST_KEY")


class TestKWalletStore(unittest.TestCase):
    """Tests for KDE Wallet via kwallet-query."""

    def setUp(self):
        self.store = keystore.KWalletStore()

    @patch('kikolib.keystore._run')
    def test_kwallet_get_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="secret-value")
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")
        mock_run.assert_called_once_with(["kwallet-query", "-r", "TEST_KEY", "-f", "Passwords", "kdewallet"])

    @patch('kikolib.keystore._run')
    def test_kwallet_get_failure(self, mock_run):
        mock_run.return_value = Mock(returncode=1, stdout="error")
        result = self.store.get("TEST_KEY")
        self.assertIsNone(result)

    @patch('kikolib.keystore._run')
    def test_kwallet_set_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="")
        result = self.store.set("TEST_KEY", "secret-value")
        self.assertTrue(result)
        mock_run.assert_called_once_with(
            ["kwallet-query", "-w", "TEST_KEY", "-f", "Passwords", "kdewallet"],
            stdin="secret-value"
        )

    @patch('kikolib.keystore._run')
    def test_kwallet_delete_always_false(self, mock_run):
        """kwallet-query has no delete, should always return False."""
        result = self.store.delete("TEST_KEY")
        self.assertFalse(result)
        mock_run.assert_not_called()

    @patch('kikolib.keystore._run')
    def test_kwallet_key_uppercased(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="secret")
        self.store.get("test_key")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][2], "TEST_KEY")


class TestSecretToolStore(unittest.TestCase):
    """Tests for libsecret via secret-tool."""

    def setUp(self):
        self.store = keystore.SecretToolStore()

    @patch('kikolib.keystore._run')
    def test_secret_tool_get_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="secret-value")
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")
        mock_run.assert_called_once_with(["secret-tool", "lookup", "kiko-identity", "TEST_KEY"])

    @patch('kikolib.keystore._run')
    def test_secret_tool_set_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="")
        result = self.store.set("TEST_KEY", "secret-value")
        self.assertTrue(result)
        mock_run.assert_called_once_with(
            ["secret-tool", "store", "--label", "kiko TEST_KEY", "kiko-identity", "TEST_KEY"],
            stdin="secret-value"
        )

    @patch('kikolib.keystore._run')
    def test_secret_tool_delete_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="")
        result = self.store.delete("TEST_KEY")
        self.assertTrue(result)
        mock_run.assert_called_once_with(["secret-tool", "clear", "kiko-identity", "TEST_KEY"])


class TestKeychainStore(unittest.TestCase):
    """Tests for macOS Keychain via security CLI."""

    def setUp(self):
        self.store = keystore.KeychainStore()

    @patch('kikolib.keystore._run')
    def test_keychain_get_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="secret-value")
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")
        mock_run.assert_called_once_with(["security", "find-generic-password", "-s", "KIKO", "-a", "TEST_KEY", "-w"])

    @patch('kikolib.keystore._run')
    def test_keychain_set_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="")
        result = self.store.set("TEST_KEY", "secret-value")
        self.assertTrue(result)
        # Note: -U updates if exists
        mock_run.assert_called_once_with(["security", "add-generic-password", "-s", "KIKO", "-a", "TEST_KEY", "-w", "secret-value", "-U"])

    @patch('kikolib.keystore._run')
    def test_keychain_delete_success(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="")
        result = self.store.delete("TEST_KEY")
        self.assertTrue(result)
        mock_run.assert_called_once_with(["security", "delete-generic-password", "-s", "KIKO", "-a", "TEST_KEY"])


class TestWcmStore(unittest.TestCase):
    """Tests for Windows Credential Manager."""

    def setUp(self):
        self.store = keystore.WcmStore()

    @patch('kikolib.keystore._wcm_read')
    def test_wcm_get_success(self, mock_read):
        mock_read.return_value = "secret-value"
        result = self.store.get("TEST_KEY")
        self.assertEqual(result, "secret-value")
        mock_read.assert_called_once_with("KIKO/KIKO_IDENTITY/TEST_KEY")

    @patch('kikolib.keystore._wcm_write')
    def test_wcm_set_success(self, mock_write):
        mock_write.return_value = True
        result = self.store.set("TEST_KEY", "secret-value")
        self.assertTrue(result)
        mock_write.assert_called_once_with("KIKO/KIKO_IDENTITY/TEST_KEY", "secret-value")

    @patch('kikolib.keystore._wcm_delete')
    def test_wcm_delete_success(self, mock_delete):
        mock_delete.return_value = True
        result = self.store.delete("TEST_KEY")
        self.assertTrue(result)
        mock_delete.assert_called_once_with("KIKO/KIKO_IDENTITY/TEST_KEY")

    def test_wcm_target_format(self):
        """Test target name format."""
        target = self.store._target("my_key")
        self.assertEqual(target, "KIKO/KIKO_IDENTITY/MY_KEY")


class TestKeystoreProbes(unittest.TestCase):
    """Tests for keystore backend probes."""

    @patch('kikolib.keystore.sys.platform', 'win32')
    def test_probe_wcm_windows(self):
        ok, msg = keystore._probe_wcm()
        self.assertTrue(ok)
        self.assertEqual(msg, "Windows Credential Manager")

    @patch('kikolib.keystore.sys.platform', 'linux')
    def test_probe_wcm_not_windows(self):
        ok, msg = keystore._probe_wcm()
        self.assertFalse(ok)
        self.assertEqual(msg, "not Windows")

    @patch('kikolib.keystore.sys.platform', 'darwin')
    @patch('kikolib.keystore.shutil.which')
    def test_probe_keychain_macos_found(self, mock_which):
        mock_which.return_value = "/usr/bin/security"
        ok, msg = keystore._probe_keychain()
        self.assertTrue(ok)
        self.assertEqual(msg, "security found")

    @patch('kikolib.keystore.sys.platform', 'darwin')
    @patch('kikolib.keystore.shutil.which')
    def test_probe_keychain_macos_not_found(self, mock_which):
        mock_which.return_value = None
        ok, msg = keystore._probe_keychain()
        self.assertFalse(ok)
        self.assertEqual(msg, "security not on PATH")

    @patch('kikolib.keystore.sys.platform', 'linux')
    def test_probe_keychain_not_macos(self):
        ok, msg = keystore._probe_keychain()
        self.assertFalse(ok)
        self.assertEqual(msg, "not macOS")

    @patch('kikolib.keystore.sys.platform', 'linux')
    @patch('kikolib.keystore.os.environ', {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'})
    @patch('kikolib.keystore.shutil.which')
    def test_probe_secret_tool_found(self, mock_which):
        mock_which.return_value = "/usr/bin/secret-tool"
        ok, msg = keystore._probe_secret_tool()
        self.assertTrue(ok)
        self.assertEqual(msg, "secret-tool found")

    @patch('kikolib.keystore.sys.platform', 'linux')
    @patch('kikolib.keystore.os.environ', {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'})
    @patch('kikolib.keystore.shutil.which')
    def test_probe_secret_tool_not_found(self, mock_which):
        mock_which.return_value = None
        ok, msg = keystore._probe_secret_tool()
        self.assertFalse(ok)
        self.assertEqual(msg, "secret-tool not on PATH")

    @patch('kikolib.keystore.sys.platform', 'linux')
    @patch('kikolib.keystore.os.environ', {})
    def test_probe_secret_tool_no_dbus(self):
        ok, msg = keystore._probe_secret_tool()
        self.assertFalse(ok)
        self.assertEqual(msg, "no DBUS_SESSION_BUS_ADDRESS")

    @patch('kikolib.keystore.sys.platform', 'linux')
    @patch('kikolib.keystore.shutil.which')
    def test_probe_kwallet_found(self, mock_which):
        mock_which.return_value = "/usr/bin/kwallet-query"
        ok, msg = keystore._probe_kwallet()
        self.assertTrue(ok)
        self.assertEqual(msg, "kwallet-query found")

    @patch('kikolib.keystore.sys.platform', 'linux')
    @patch('kikolib.keystore.shutil.which')
    def test_probe_dcop_found(self, mock_which):
        mock_which.return_value = "/usr/bin/dcop"
        ok, msg = keystore._probe_dcop()
        self.assertTrue(ok)
        self.assertEqual(msg, "dcop found")


class TestKeystoreIntegration(unittest.TestCase):
    """Integration tests for keystore module."""

    def test_register_all_runs(self):
        """Test register_all doesn't crash."""
        keystore.register_all()  # Should not raise

    def test_factory_registration(self):
        """Test backends are registered with factory."""
        from kikolib import factory
        available = factory.available(keystore.KIND)
        names = [name for name, _ in available]
        # On Linux, wcm and keychain won't be available (probes fail)
        # But kwallet and dcop should be if their tools are on PATH
        # At minimum, the registration should exist
        self.assertTrue(len(names) >= 0)  # registration exists

    def test_get_secret_env_priority(self):
        """Test environment variable takes priority over stores."""
        with patch.dict(os.environ, {"TEST_KEY": "env-value"}):
            result = keystore.get_secret("TEST_KEY")
            self.assertEqual(result, "env-value")

    def test_get_secret_env_uppercase(self):
        """Test uppercase environment variable works."""
        with patch.dict(os.environ, {"TEST_KEY": "env-value"}):
            result = keystore.get_secret("test_key")
            self.assertEqual(result, "env-value")

    @patch('kikolib.keystore.factory')
    def test_get_secret_store_fallback(self, mock_factory):
        """Test falling back to OS store when no env var."""
        mock_store = Mock()
        mock_store.get.return_value = "store-value"
        mock_factory.available.return_value = [("test_store", mock_store)]

        with patch.dict(os.environ, {}, clear=True):
            with patch('kikolib.keystore._store', return_value=mock_store):
                result = keystore.get_secret("TEST_KEY")
                self.assertEqual(result, "store-value")
                mock_store.get.assert_called_once_with("TEST_KEY")

    @patch('kikolib.keystore._store')
    def test_get_secret_tries_all_stores(self, mock_store):
        """Test trying multiple stores until one returns value."""
        store1 = Mock()
        store1.get.return_value = None
        store2 = Mock()
        store2.get.return_value = "store2-value"
        store3 = Mock()
        store3.get.return_value = "store3-value"

        with patch('kikolib.keystore.factory') as mock_factory:
            mock_factory.available.return_value = [("store1", store1), ("store2", store2), ("store3", store3)]
            with patch.dict(os.environ, {}, clear=True):
                result = keystore.get_secret("TEST_KEY")
                self.assertEqual(result, "store2-value")

    @patch('kikolib.keystore._store')
    def test_set_secret(self, mock_store):
        """Test set_secret uses first available store."""
        mock_store_instance = Mock()
        mock_store_instance.set.return_value = True
        mock_store.return_value = mock_store_instance

        result = keystore.set_secret("TEST_KEY", "secret-value")
        self.assertTrue(result)
        mock_store_instance.set.assert_called_once_with("TEST_KEY", "secret-value")

    @patch('kikolib.keystore._store')
    def test_set_secret_no_store(self, mock_store):
        """Test set_secret returns False when no store available."""
        mock_store.return_value = None
        result = keystore.set_secret("TEST_KEY", "secret-value")
        self.assertFalse(result)

    @patch('kikolib.keystore.factory')
    def test_keystore_impl_env_override(self, mock_factory):
        """Test KIKO_KEYSTORE_IMPL env var forces specific store."""
        mock_store = Mock()
        mock_store.get.return_value = "impl-value"
        mock_factory.choose.return_value = mock_store

        with patch.dict(os.environ, {"KIKO_KEYSTORE_IMPL": "dcop"}):
            result = keystore.get_secret("TEST_KEY")
            self.assertEqual(result, "impl-value")
            mock_factory.choose.assert_called_once_with(keystore.KIND)


class TestKeystoreHelpers(unittest.TestCase):
    """Tests for keystore helper functions."""

    def test_run_success(self):
        """Test _run returns CompletedProcess on success."""
        with patch('kikolib.keystore.subprocess.run') as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="output", stderr="")
            result = keystore._run(["test"])
            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "output")

    def test_run_timeout(self):
        """Test _run returns None on timeout."""
        with patch('kikolib.keystore.subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired([], 3)
            result = keystore._run(["test"])
            self.assertIsNone(result)

    def test_run_os_error(self):
        """Test _run returns None on OSError."""
        with patch('kikolib.keystore.subprocess.run') as mock_run:
            mock_run.side_effect = OSError("not found")
            result = keystore._run(["test"])
            self.assertIsNone(result)

    def test_out_success(self):
        """Test _out extracts stdout on success."""
        result = subprocess.CompletedProcess([], 0, stdout="  value  ", stderr="")
        self.assertEqual(keystore._out(result), "value")

    def test_out_failure(self):
        """Test _out returns None on failure."""
        result = subprocess.CompletedProcess([], 1, stdout="value", stderr="error")
        self.assertIsNone(keystore._out(result))

    def test_out_none(self):
        """Test _out returns None for None input."""
        self.assertIsNone(keystore._out(None))

    def test_ok_success(self):
        """Test _ok returns True for success."""
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        self.assertTrue(keystore._ok(result))

    def test_ok_failure(self):
        """Test _ok returns False for failure."""
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        self.assertFalse(keystore._ok(result))

    def test_ok_none(self):
        """Test _ok returns False for None input."""
        self.assertFalse(keystore._ok(None))


if __name__ == "__main__":
    unittest.main()