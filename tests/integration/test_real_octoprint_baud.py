"""Run separately in a real OctoPrint environment, not the stubbed unit suite.

python -m unittest discover -s tests/integration -p test_real_octoprint_baud.py
"""
import tempfile
import unittest

import octoprint.plugin
from octoprint.settings import settings
import octoprint.util.comm as comm
from octoprint.printer import PrinterInterface

from octoprint_rme_compatibility.baud_policy import install


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
