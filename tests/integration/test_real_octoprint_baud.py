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
import octoprint.util.comm as comm
from octoprint.printer import PrinterInterface

from octoprint_rme_compatibility.baud_policy import install
from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


class RealOctoPrintBaudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.config = settings(init=True, basedir=cls.directory.name)
        octoprint.plugin.plugin_manager(init=True, plugin_folders=[],
                                        plugin_bases=[], plugin_entry_points=[])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_real_connection_options_without_new_host_constant(self):
        # This is the real host function used by GET /api/settings, including
        # serial enumeration and configured preferred-baud handling.
        install(lambda: True)
        rates = PrinterInterface.get_connection_options()["baudrates"]
        self.assertIn(1000000, rates)
        self.assertEqual(1000000, comm.baudrateList()[0])

    def test_real_adapter_failure_falls_back_to_native_options(self):
        from unittest.mock import patch
        install(lambda: True)
        with patch("octoprint_rme_compatibility.baud_policy.ordered_candidates",
                   side_effect=RuntimeError("injected adapter failure")):
            rates = PrinterInterface.get_connection_options()["baudrates"]
        self.assertIn(115200, rates)
        self.assertNotIn(1000000, rates)

    def test_real_startup_autoconnect_uses_auto_and_fastest_candidate(self):
        import octoprint.server as server
        from octoprint.plugin import PluginSettings

        for enabled in (True, False):
            with self.subTest(enabled=enabled):
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

                    def connect(self, **kwargs):
                        # Capture what the REAL server autoconnect method
                        # passes to the connector, not just dropdown options.
                        observed.append((kwargs, comm.baudrateList()[0]))

                service = types.SimpleNamespace(_settings=self.config, _logger=plugin._logger)
                profiles = types.SimpleNamespace(get_default=lambda: {"id": "_default"})
                plugin.initialize()  # implementation initialization precedes autoconnect
                if enabled:
                    persisted = yaml.safe_load((Path(self.directory.name) / "config.yaml").read_text())
                    self.assertEqual(0, persisted["serial"]["baudrate"])
                with patch.object(server, "printer", Printer()), patch.object(server, "printerProfileManager", profiles):
                    server.Server._start_serial_autoconnect(service)
                self.assertEqual(1, len(observed))
                self.assertEqual(0 if enabled else 115200, observed[0][0]["baudrate"])
                self.assertEqual(1000000 if enabled else 115200, observed[0][1])
                # OctoPrint calls on_startup only AFTER autoconnect, so no
                # corrective disconnect/reconnect is needed or permitted.
                plugin.on_startup("127.0.0.1", 5000)
