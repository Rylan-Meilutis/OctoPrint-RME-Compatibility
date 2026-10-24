import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from octoprint_rme_compatibility.tool_changes import read_selections, remaining


class ToolChangeTests(unittest.TestCase):
    def test_backend_publishes_from_current_position_without_waiting_for_scan(self):
        from test_toolmap_gate import RmeCompatibilityPlugin
        plugin = RmeCompatibilityPlugin()
        plugin._object_context = lambda: dict(active=True, file='part.gcode', disk='/part.gcode',
                                              job_id='job1', position=30)
        plugin._state['machine']['logical_tools'] = 3
        plugin._state['toolmap'] = dict(enabled=True, mapping={0: 1, 1: 2, 2: 0})
        result = None
        plugin._startup_analysis = SimpleNamespace(request=lambda *args: result)
        plugin._schedule_publish = Mock()
        plugin._sync_tool_changes()
        self.assertIsNone(plugin._state['tool_changes']['remaining'])
        result = ((10, 0), (20, 1), (40, 2))
        plugin._sync_tool_changes()
        self.assertEqual(1, plugin._state['tool_changes']['remaining'])
        self.assertEqual('job1', plugin._state['tool_changes']['job_id'])
    def test_exact_offsets_repeats_parking_mapping_and_shared_tower(self):
        text = b'; T7 is a comment\r\nT0\r\nT0 S1\r\nP0\r\nT255\r\nT0\r\nT2\r\nG1 X99 E2 ; prime tower\r\nT1\r\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.gcode'
            path.write_bytes(text)
            selections = read_selections(path, 8)
        first = text.index(b'T2\r\n') + len(b'T2\r\n')
        self.assertEqual(dict(total=2, completed=0, remaining=2), remaining(selections, 0))
        self.assertEqual(1, remaining(selections, first)['remaining'])
        self.assertEqual(0, remaining(selections, len(text))['remaining'])
        self.assertEqual(1, remaining(selections, 0, {0: 0, 2: 0, 1: 1})['total'])
        self.assertIsNone(remaining(selections, 0, {2: 255})['remaining'])

    def test_empty_and_numbered_compact_commands(self):
        self.assertEqual(0, remaining((), 0)['remaining'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.gcode'
            path.write_bytes(b'N1 T0*0\nN2 T1S1*0\nM117 T7\n')
            self.assertEqual(1, remaining(read_selections(path), 0)['remaining'])
