import logging
import time
import types
import unittest
from unittest.mock import patch

from test_toolmap_gate import RmeCompatibilityPlugin, _Printer, _Settings


class SpooljoinPreflightTests(unittest.TestCase):
    def plugin(self):
        p = RmeCompatibilityPlugin()
        p._settings = _Settings()
        p._settings.values['prompt_toolmap_on_print'] = False
        p._printer = _Printer()
        p._printer.get_current_data = lambda: {'job': {'file': {
            'origin': 'local', 'path': 'part.gcode', 'date': time.time() - 30}}}
        p._file_manager = types.SimpleNamespace(path_on_disk=lambda *args: 'part.gcode')
        p._logger = logging.getLogger('spooljoin-test')
        p._defer = lambda *args: None
        p._selected_job_has_executable_gcode = lambda: True
        p._configure_validator_mapping = lambda *args: None
        p._state.update(supported=True, machine={'logical_tools': 2},
                        spooljoin={'supported': True, 'entries': []},
                        toolmap={'enabled': False, 'mapping': {}},
                        loaded_filaments=[{'tool': i, 'material': 'PLA', 'color': '#ffffff'} for i in range(2)])
        return p

    def prepare(self, p):
        with patch('octoprint_rme_compatibility.plugin.read_requirements', return_value=[
                {'logical': 0, 'material': 'PLA', 'color': '#ffffff'}]):
            p._prepare_toolmap_prompt()

    def test_recent_job_still_holds_for_fallback_review(self):
        p = self.plugin()
        self.prepare(p)
        prompt = p._state['prompt']
        self.assertTrue(prompt['skip_mapping'])
        self.assertTrue(prompt['spooljoin_review'])
        self.assertIsNone(prompt['deadline'])
        p._expire_toolmap_prompt()
        self.assertEqual(p._printer.holds, [True])
        with self.assertRaises(ValueError):
            p._apply_toolmap({0: 0, 1: 1}, False, release_hold=True)
        self.assertEqual(p._printer.command_batches, [])

    def test_chains_queued_before_job_release_and_not_reused(self):
        p = self.plugin()
        self.prepare(p)
        events = []
        p._send_commands = lambda commands: events.append(list(commands))
        p._release_toolmap_hold = lambda: events.append('release')
        p._apply_toolmap({0: 0, 1: 1}, False, release_hold=True, fallbacks={'0': [1]})
        self.assertEqual(events[-1], 'release')
        self.assertIn('@RME SPOOLJOIN ADD from=0 to=1', events[0])
        events.clear()
        p._apply_toolmap({0: 0, 1: 1}, False, release_hold=True, fallbacks={})
        self.assertIn('@RME SPOOLJOIN RESET', events[0])
        self.assertFalse(any('SPOOLJOIN ADD' in cmd for cmd in events[0]))
