import logging
import types
import unittest
from unittest.mock import Mock, patch

from test_toolmap_gate import RmeCompatibilityPlugin


class FirmwareReconnectBaudTests(unittest.TestCase):
    def plugin(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger(__name__)
        plugin._schedule_firmware_reconnect = lambda _: None
        plugin._printer = types.SimpleNamespace(
            get_current_connection=lambda: ("Operational", "/dev/ttyACM0", 1000000, {}),
            connect=Mock(),
        )
        return plugin

    def test_reboot_preserves_working_baud_but_not_usb_path(self):
        plugin = self.plugin()
        plugin._arm_firmware_reconnect()
        plugin._attempt_firmware_reconnect()
        plugin._printer.connect.assert_called_once_with(baudrate=1000000)

    def test_unknown_baud_keeps_octoprint_default(self):
        plugin = self.plugin()
        plugin._printer.get_current_connection = lambda: ("Operational", "AUTO", 0, {})
        plugin._arm_firmware_reconnect()
        plugin._attempt_firmware_reconnect()
        plugin._printer.connect.assert_called_once_with()

    def test_ordinary_disconnect_does_not_change_baud_or_reconnect(self):
        plugin = self.plugin()
        self.assertFalse(plugin._attempt_firmware_reconnect())
        plugin._printer.connect.assert_not_called()

    def test_authoritative_empty_status_clears_stale_stage(self):
        plugin = self.plugin()
        plugin._print_job_active = lambda: False
        plugin._persist_and_publish = lambda: None
        plugin._file_service = types.SimpleNamespace(
            firmware_status=lambda: {"candidate": 0, "armed": 0, "state": "idle"})
        plugin._state["firmware"].update(status="ready", candidate=True)
        plugin._reconcile_firmware_stage()
        self.assertEqual("idle", plugin._state["firmware"]["status"])
        self.assertFalse(plugin._state["firmware"]["candidate"])

    def test_idle_stage_recheck_is_deferred_and_rate_limited(self):
        plugin = self.plugin()
        plugin._state["session"]["active"] = True
        plugin._state["firmware"]["status"] = "ready"
        plugin._schedule_publish = lambda: None
        plugin._defer = Mock()
        record = {"record": "session", "lease": 1, "printer_state": "IDLE"}
        with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=100):
            plugin._handle_record(dict(record))
            plugin._defer.assert_called_once_with(plugin._reconcile_firmware_stage)
            plugin._defer.reset_mock()
            plugin._handle_record(dict(record))
            plugin._defer.assert_not_called()
        with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=131):
            plugin._handle_record(dict(record))
            plugin._defer.assert_called_once_with(plugin._reconcile_firmware_stage)

    def test_delayed_printer_cleanup_converges_without_hiding_real_candidate(self):
        plugin = self.plugin()
        plugin._print_job_active = lambda: False
        plugin._persist_and_publish = lambda: None
        plugin._schedule_publish = lambda: None
        plugin._defer = Mock()
        plugin._state["session"]["active"] = True
        replies = iter([
            {"candidate": 1, "armed": 0, "state": "ready", "path": "FWUPD.RME", "size": 100},
            {"candidate": 0, "armed": 0, "state": "idle"},
        ])
        plugin._file_service = types.SimpleNamespace(firmware_status=lambda: next(replies))
        with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=100):
            self.assertTrue(plugin._reconcile_firmware_stage())
        self.assertTrue(plugin._state["firmware"]["candidate"])
        with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=131):
            plugin._handle_record({"record": "session", "lease": 1, "printer_state": "IDLE"})
            plugin._defer.assert_called_once_with(plugin._reconcile_firmware_stage)
            self.assertFalse(plugin._reconcile_firmware_stage())
        self.assertFalse(plugin._state["firmware"]["candidate"])
        self.assertEqual("idle", plugin._state["firmware"]["status"])
        plugin._printer.connect.assert_not_called()
