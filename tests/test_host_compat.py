import types
import unittest
from unittest.mock import Mock

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility import host_compat


class HostCompatTests(unittest.TestCase):
    def test_modern_serial_uses_live_connector_not_cache(self):
        active, stale = object(), object()
        printer = types.SimpleNamespace(current_connection=types.SimpleNamespace(connector="serial", _comm=active))
        self.assertIs(active, host_compat.serial_comm(printer, stale))
        printer.current_connection = None
        self.assertIsNone(host_compat.serial_comm(printer, stale))
        printer.current_connection = types.SimpleNamespace(connector="network", _comm=stale)
        self.assertIsNone(host_compat.serial_comm(printer, stale))

    def test_modern_does_not_touch_legacy_comm_or_connection_method(self):
        class Modern:
            current_connection = types.SimpleNamespace(connector="serial", _comm="active", connection_parameters={"baudrate":1000000})
            @property
            def _comm(self):
                raise AssertionError("deprecated comm accessed")
            def get_current_connection(self):
                raise AssertionError("deprecated connection tuple accessed")
        self.assertEqual("active", host_compat.serial_comm(Modern()))
        self.assertEqual({"connector":"serial", "parameters":{"baudrate":1000000}}, host_compat.reconnect_options(Modern()))

    def test_legacy_comm_and_reconnect_preserve_behavior(self):
        printer = types.SimpleNamespace(_comm="legacy", get_current_connection=lambda:("Operational", "/dev/old", 250000, {}))
        self.assertEqual("legacy", host_compat.serial_comm(printer))
        self.assertEqual({"baudrate":250000}, host_compat.reconnect_options(printer))

    def test_modern_nonserial_or_offline_cannot_arm_serial_reconnect(self):
        for connection in (None, types.SimpleNamespace(connector="network")):
            with self.subTest(connection=connection):
                with self.assertRaises(RuntimeError):
                    host_compat.reconnect_options(types.SimpleNamespace(current_connection=connection))

    def test_modern_reconnect_never_reuses_usb_path_and_auto_is_nested(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = types.SimpleNamespace(current_connection=types.SimpleNamespace(connector="serial",
            connection_parameters={"port":"/dev/old", "baudrate":1000000}))
        plugin._settings = types.SimpleNamespace(get_boolean=lambda p:True, global_get=lambda p:
            "serial" if p[-1]=="connector" else {"baudrate":0})
        plugin._arm_firmware_reconnect()
        self.assertEqual({"connector":"serial", "parameters":{"baudrate":0}}, plugin._firmware_reconnect_options)

    def test_startup_recognizes_new_printer_file_origin(self):
        plugin = RmeCompatibilityPlugin()
        for origin in ("sdcard", "printer"):
            current = types.SimpleNamespace(getFilename=lambda:"part.gcode", getFileLocation=lambda:origin)
            result = plugin._startup_file_info(types.SimpleNamespace(_currentFile=current))
            self.assertEqual("part.gcode", result["path"])
            self.assertTrue(host_compat.is_printer_file(result["origin"]))

    def test_unrelated_storage_is_not_treated_as_printer(self):
        for origin in ("local", "cloud", None, ""):
            self.assertFalse(host_compat.is_printer_file(origin))
