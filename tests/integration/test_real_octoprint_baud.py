"""Run separately in a real OctoPrint environment, not the stubbed unit suite.

python -m unittest discover -s tests/integration -p test_real_octoprint_baud.py
"""
import tempfile
import unittest
import logging
import types
from pathlib import Path
import yaml
from unittest.mock import patch

import octoprint.plugin
from octoprint.settings import settings
from core_compat import core as comm, ConnectedSerialPrinter
from octoprint.printer import PrinterInterface

from octoprint_rme_compatibility.baud_policy import install
from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


class RealOctoPrintBaudTests(unittest.TestCase):
    def options(self):
        if ConnectedSerialPrinter is not None:
            return ConnectedSerialPrinter.connection_options()["baudrate"]
        return PrinterInterface.get_connection_options()["baudrates"]
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.config = settings(init=True, basedir=cls.directory.name)
        if ConnectedSerialPrinter is not None:
            from octoprint.plugins.serial_connector import SerialConnectorPlugin
            # The real server initializes this bundled plugin before
            # connection options. Supply its actual schema defaults here.
            cls.config.add_overlay({"plugins": {"serial_connector": SerialConnectorPlugin().get_settings_defaults()}}, key="test.serial_connector")
        octoprint.plugin.plugin_manager(init=True, plugin_folders=[],
                                        plugin_bases=[], plugin_entry_points=[])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_real_connection_options_without_new_host_constant(self):
        # This is the real host function used by GET /api/settings, including
        # serial enumeration and configured preferred-baud handling.
        install(lambda: True)
        rates = self.options()
        self.assertIn(1000000, rates)
        self.assertEqual(1000000, comm.baudrateList()[0])

    def test_real_adapter_failure_falls_back_to_native_options(self):
        from unittest.mock import patch
        install(lambda: True)
        with patch("octoprint_rme_compatibility.baud_policy.ordered_candidates",
                   side_effect=RuntimeError("injected adapter failure")):
            rates = self.options()
        self.assertIn(115200, rates)
        self.assertNotIn(1000000, rates)

    def test_real_startup_autoconnect_uses_auto_and_fastest_candidate(self):
        import octoprint.server as server
        from octoprint.plugin import PluginSettings

        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                modern = ConnectedSerialPrinter is not None
                if modern:
                    self.config.setBoolean(["printerConnection", "autoconnect"], True)
                    self.config.set(["printerConnection", "preferred"], {
                        "connector": "serial", "parameters": {"port": "AUTO", "baudrate": 115200}})
                else:
                    self.config.setBoolean(["serial", "autoconnect"], True)
                    self.config.set(["serial", "port"], "AUTO")
                    self.config.setInt(["serial", "baudrate"], 115200)
                plugin = RmeCompatibilityPlugin()
                plugin._logger = logging.getLogger(__name__)
                plugin._settings = PluginSettings(self.config, "rme_compatibility",
                                                  defaults=plugin.get_settings_defaults())
                plugin._settings.set_boolean(["auto_highest_baud"], enabled)
                plugin._settings.set_boolean(["baud_auto_migrated"], True)
                observed = []

                class Printer:
                    get_connection_options = staticmethod(PrinterInterface.get_connection_options)

                    def connect(self, *args, **kwargs):
                        # Capture what the REAL server autoconnect method
                        # passes to the connector, not just dropdown options.
                        observed.append((kwargs, comm.baudrateList()[0]))

                service = types.SimpleNamespace(_settings=self.config, _logger=plugin._logger)
                profiles = types.SimpleNamespace(get_default=lambda: {"id": "_default"})
                plugin.initialize()  # implementation initialization precedes autoconnect
                if enabled:
                    persisted = yaml.safe_load((Path(self.directory.name) / "config.yaml").read_text())
                    saved = persisted["printerConnection"]["preferred"]["parameters"] if modern else persisted["serial"]
                    self.assertEqual(0, saved["baudrate"])
                with patch.object(server, "printer", Printer()), patch.object(server, "printerProfileManager", profiles):
                    if modern:
                        with patch.object(comm, "serialList", return_value=["/dev/ttyTEST"]), patch(
                                "octoprint.plugins.serial_connector.connector.serialList", return_value=["/dev/ttyTEST"]):
                            server.Server._start_printer_autoconnect(service)
                    else:
                        server.Server._start_serial_autoconnect(service)
                self.assertEqual(1, len(observed))
                parameters = observed[0][0]["parameters"] if modern else observed[0][0]
                self.assertEqual(0 if enabled else 115200, parameters["baudrate"])
                self.assertEqual(1000000 if enabled else 115200, observed[0][1])
                # OctoPrint calls on_startup only AFTER autoconnect, so no
                # corrective disconnect/reconnect is needed or permitted.
                plugin.on_startup("127.0.0.1", 5000)
