# SPDX-License-Identifier: MIT
"""Tests for health module."""
import unittest
from unittest.mock import Mock, MagicMock
from kiko import health


class TestParseInterval(unittest.TestCase):
    def test_parse_interval_none(self):
        """Test parsing None returns default."""
        self.assertEqual(health.parse_interval(None), 300.0)

    def test_parse_interval_empty(self):
        """Test parsing empty string returns default."""
        self.assertEqual(health.parse_interval(""), 300.0)

    def test_parse_interval_zero(self):
        """Test parsing 0 returns 0 (off)."""
        self.assertEqual(health.parse_interval("0"), 0.0)
        self.assertEqual(health.parse_interval(0), 0.0)

    def test_parse_interval_negative(self):
        """Test parsing negative returns 0 (off)."""
        self.assertEqual(health.parse_interval("-10"), 0.0)

    def test_parse_interval_below_min(self):
        """Test parsing below minimum returns minimum (10s)."""
        self.assertEqual(health.parse_interval("5"), 10.0)
        self.assertEqual(health.parse_interval("1"), 10.0)

    def test_parse_interval_valid(self):
        """Test parsing valid interval."""
        self.assertEqual(health.parse_interval("60"), 60.0)
        self.assertEqual(health.parse_interval("300"), 300.0)
        self.assertEqual(health.parse_interval("30.5"), 30.5)

    def test_parse_interval_invalid(self):
        """Test parsing invalid returns default."""
        self.assertEqual(health.parse_interval("invalid"), 300.0)


class TestHealthProber(unittest.TestCase):
    def setUp(self):
        self.gateway = Mock()
        self.gateway.cooldowns = Mock()
        self.gateway.cooldowns.clock = lambda: 1000.0
        self.gateway.cooldowns.remaining = Mock(return_value=0.0)
        self.gateway.cooldowns.since_ok = Mock(return_value=None)
        self.gateway.probe = Mock(return_value=True)

        self.catalog = Mock()
        self.catalog.snapshot = Mock(return_value=Mock(
            settings={},
            models=[]
        ))

        self.on_status = Mock()
        self.prober = health.HealthProber(
            self.gateway, self.catalog, self.on_status,
            interval=60.0, clock=lambda: 1000.0
        )

    def test_probe_once_empty(self):
        """Test probe_once with no models."""
        snap = self.catalog.snapshot()
        snap.models = []
        result = self.prober.probe_once()
        self.assertEqual(result, {})
        self.assertEqual(self.prober.last_sent, 0)

    def test_probe_once_skips_ok(self):
        """Test probe_once skips models with status ok."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "ok", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.assertEqual(result, {})
        self.gateway.probe.assert_not_called()

    def test_probe_once_skips_disabled(self):
        """Test probe_once skips disabled models."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": False, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.assertEqual(result, {})
        self.gateway.probe.assert_not_called()

    def test_probe_once_skips_health_no_probe(self):
        """Test probe_once skips models in health_no_probe setting."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        snap = self.catalog.snapshot()
        snap.settings = {"health_no_probe": "model1"}
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.assertEqual(result, {})
        self.gateway.probe.assert_not_called()

    def test_probe_once_skips_paid_unknown(self):
        """Test probe_once skips paid models with unknown status."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "unknown", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.assertEqual(result, {})
        self.gateway.probe.assert_not_called()

    def test_probe_once_probes_free_unknown(self):
        """Test probe_once probes free models with unknown status."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "unknown", "price_in_per_mtok": 0.0}
        model_info.pname = "db/model1"

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.gateway.probe.assert_called_once_with("db/model1", "gpt-4o")
        self.assertEqual(result, {"model1": True})

    def test_probe_once_probes_error(self):
        """Test probe_once probes models with error status."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.gateway.probe.assert_called_once_with("db/model1", "gpt-4o")
        self.assertEqual(result, {"model1": True})

    def test_probe_once_skips_cooling(self):
        """Test probe_once skips models in cooldown."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        self.gateway.cooldowns.remaining = Mock(return_value=30.0)

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once()
        self.assertEqual(result, {})
        self.gateway.probe.assert_not_called()

    def test_probe_once_skips_recent_live_success(self):
        """Test probe_once skips models with recent live success."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        self.gateway.cooldowns.since_ok = Mock(return_value=30.0)

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once(interval=60.0)
        self.assertEqual(result, {})
        self.gateway.probe.assert_not_called()

    def test_probe_once_backoff_on_failure(self):
        """Test probe_once backs off on failure."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        self.gateway.probe = Mock(return_value=False)

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once(interval=60.0)
        self.assertEqual(result, {"model1": False})
        # Check backoff was set
        key = ("db/model1", "gpt-4o")
        self.assertIn(key, self.prober._delay)
        self.assertEqual(self.prober._delay[key], 60.0)
        self.assertIn(key, self.prober._next)

    def test_probe_once_resets_backoff_on_success(self):
        """Test probe_once resets backoff on success after failure."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        # First call fails
        self.gateway.probe = Mock(side_effect=[False, True])

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        # First probe - fails
        result1 = self.prober.probe_once(interval=60.0)
        self.assertEqual(result1, {"model1": False})
        self.assertEqual(self.prober._delay[("db/model1", "gpt-4o")], 60.0)

        # Advance clock past backoff
        self.prober.clock = lambda: 1061.0
        self.gateway.cooldowns.remaining = Mock(return_value=0.0)

        # Second probe - succeeds
        result2 = self.prober.probe_once(interval=60.0)
        self.assertEqual(result2, {"model1": True})
        # Backoff should be cleared
        self.assertNotIn(("db/model1", "gpt-4o"), self.prober._delay)
        self.assertNotIn(("db/model1", "gpt-4o"), self.prober._next)

    def test_probe_once_none_verdict(self):
        """Test probe_once handles None verdict (no status change, but backoff)."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        self.gateway.probe = Mock(return_value=None)

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once(interval=60.0)
        self.assertEqual(result, {})  # None verdict not included
        self.on_status.assert_not_called()  # No status write
        # But backoff should still be set
        self.assertIn(("db/model1", "gpt-4o"), self.prober._delay)

    def test_probe_once_probe_exception(self):
        """Test probe_once handles probe exceptions."""
        model_info = Mock()
        model_info.mapped = Mock(model_id="gpt-4o")
        model_info.row = {"alias": "model1", "enabled": True, "status": "error", "price_in_per_mtok": 1.0}
        model_info.pname = "db/model1"

        self.gateway.probe = Mock(side_effect=Exception("probe failed"))

        snap = self.catalog.snapshot()
        snap.models = [model_info]

        result = self.prober.probe_once(interval=60.0)
        self.assertEqual(result, {})
        self.on_status.assert_not_called()
        # Backoff should be set
        self.assertIn(("db/model1", "gpt-4o"), self.prober._delay)

    def test_start_stop(self):
        """Test start and stop."""
        # Start with interval > 0
        self.prober._thread = None
        result = self.prober.start()
        self.assertTrue(result)
        self.assertIsNotNone(self.prober._thread)

        # Stop
        self.prober.stop(timeout=0.1)
        self.assertTrue(self.prober._stop.is_set())

    def test_start_already_running(self):
        """Test start when already running returns False."""
        self.prober._thread = Mock()
        result = self.prober.start()
        self.assertFalse(result)

    def test_start_interval_zero(self):
        """Test start with interval 0 returns False."""
        prober = health.HealthProber(
            self.gateway, self.catalog, self.on_status,
            interval=0.0, clock=lambda: 1000.0
        )
        result = prober.start()
        self.assertFalse(result)

    def test_start_reads_setting(self):
        """Test start reads health_check_interval from settings."""
        self.catalog.snapshot = Mock(return_value=Mock(
            settings={"health_check_interval": "120"},
            models=[]
        ))
        prober = health.HealthProber(
            self.gateway, self.catalog, self.on_status,
            interval=None, clock=lambda: 1000.0
        )
        result = prober.start()
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()