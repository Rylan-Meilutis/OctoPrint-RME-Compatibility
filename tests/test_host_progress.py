import types
import unittest
from unittest.mock import patch

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.progress import progress_command


class HostProgressTests(unittest.TestCase):
    def test_counts_are_capability_checked_and_match_current_file(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state['machine'].update(host_progress=1, host_tool_changes=1)
        plugin._state['session']['active'] = True
        data = self.data()
        data['job']['file']['path'] = 'part.gcode'
        plugin._printer = types.SimpleNamespace(get_current_data=lambda: data,
            get_state_id=lambda: 'PRINTING', is_paused=lambda: False)
        sent = []
        plugin._send_priority_service = lambda command, trigger: sent.append(command)
        for left, total, file, expected in (
            (3, 9, 'part.gcode', 'tools_remaining=3 tools_total=9'),
            (0, 0, 'part.gcode', 'tools_remaining=0 tools_total=0'),
            (1, 9, 'old.gcode', 'tools_remaining=unknown tools_total=unknown'),
            (None, None, 'part.gcode', 'tools_remaining=unknown tools_total=unknown'),
            (10, 9, 'part.gcode', 'tools_remaining=unknown tools_total=unknown')):
            plugin._state['tool_changes'] = dict(active=True, file=file, remaining=left, total=total)
            plugin._progress_pending_until = 0
            plugin._progress_last_command = None
            plugin._sync_host_progress()
            self.assertIn(expected, sent[-1])
            self.assertTrue(sent[-1].endswith('paused=0'))
        plugin._state['machine']['host_tool_changes'] = 0
        plugin._progress_pending_until = 0
        plugin._progress_last_command = None
        plugin._sync_host_progress()
        self.assertNotIn('tools_', sent[-1])

    def data(self, percent=23.8, remaining=134):
        return {"job": {"file": {"origin": "local"}}, "progress": {"completion": percent, "printTimeLeft": remaining}}

    def test_host_estimate_and_pause(self):
        self.assertEqual(progress_command(self.data(), True), "@RME PROGRESS SET percent=23 remaining=134 paused=1")
        self.assertIn("remaining=unknown", progress_command(self.data(0, None)))
        self.assertIn("remaining=0", progress_command(self.data(100, 0)))
        self.assertIn("remaining=unknown", progress_command(self.data(23, 0)))

    def test_bad_estimates_and_media_are_not_fabricated(self):
        for percent in (-1, 101, None, float("nan"), float("inf"), True):
            self.assertIsNone(progress_command(self.data(percent)))
        for remaining in (-1, None, float("nan"), float("inf"), True):
            self.assertIn("remaining=unknown", progress_command(self.data(50, remaining)))
        data = self.data()
        data["job"]["file"]["origin"] = "sdcard"
        self.assertIsNone(progress_command(data))

    def test_capability_and_outstanding_frame_bound(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = types.SimpleNamespace(is_printing=lambda: True, is_paused=lambda: False, get_current_data=self.data, get_state_id=lambda: "PRINTING")
        sent = []
        plugin._send_priority_service = lambda command, trigger: sent.append(command)
        plugin._sync_host_progress()
        self.assertEqual(sent, [])
        plugin._state["machine"]["host_progress"] = 1
        plugin._state["session"]["active"] = True
        for _ in range(1000):
            plugin._sync_host_progress()
        self.assertEqual(len(sent), 1)

    def test_starting_does_not_steal_m110_reset_line(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["machine"]["host_progress"] = 1
        plugin._state["session"]["active"] = True
        plugin._state["supported"] = True
        sent = []
        plugin._send_priority_service = lambda command, trigger: sent.append(command)
        plugin._printer = types.SimpleNamespace(
            is_printing=lambda: True, is_paused=lambda: False,
            get_current_data=self.data, get_state_id=lambda: "STARTING")
        for state in ("STARTING", "OPERATIONAL", "RESUMING", "CANCELLING", "ERROR"):
            plugin._printer.get_state_id = lambda: state
            plugin._sync_host_progress()
        self.assertEqual(sent, [])
        plugin._printer.get_state_id = lambda: "STARTING"
        plugin._progress_pending_until = 999999999
        self.assertEqual(plugin.gcode_queuing_hook(
            None, "queuing", "@RME PROGRESS SET percent=0 remaining=unknown paused=0",
            None, None, tags={"rme:priority_control", "trigger:rme.progress"}), (None,))
        self.assertEqual(plugin._progress_pending_until, 0)
        for state in ("PRINTING", "PAUSED"):
            plugin._printer.get_state_id = lambda: state
            plugin._printer.is_paused = lambda: state == "PAUSED"
            plugin._progress_pending_until = 0
            plugin._sync_host_progress()
        self.assertEqual(len(sent), 2)

    def test_acknowledged_progress_is_coalesced_but_pause_is_immediate(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["machine"]["host_progress"] = 1
        plugin._state["session"]["active"] = True
        paused = False
        remaining = 134
        plugin._printer = types.SimpleNamespace(
            get_state_id=lambda: "PRINTING", is_paused=lambda: paused,
            get_current_data=lambda: self.data(23, remaining))
        sent = []
        plugin._send_priority_service = lambda command, trigger: sent.append(command)
        for now, expected in ((100, 1), (102, 1), (119, 1), (120, 2)):
            plugin._progress_pending_until = 0
            with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=now):
                plugin._sync_host_progress()
            self.assertEqual(len(sent), expected)
        remaining = 130
        for now, expected in ((122, 2), (125, 3)):
            plugin._progress_pending_until = 0
            with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=now):
                plugin._sync_host_progress()
            self.assertEqual(len(sent), expected)
        paused = True
        plugin._progress_pending_until = 0
        with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=126):
            plugin._sync_host_progress()
        self.assertEqual(len(sent), 4)
        self.assertTrue(sent[-1].endswith("paused=1"))

    def test_periodic_tune_skips_startup_and_recovery_and_is_rate_limited(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["machine"]["tune"] = 1
        state = "STARTING"
        plugin._printer = types.SimpleNamespace(get_state_id=lambda: state)
        sent = []
        plugin._send_command = sent.append
        for state in ("STARTING", "RESUMING", "CANCELLING", "ERROR"):
            plugin._query_tune(periodic=True)
        self.assertEqual(sent, [])
        state = "PRINTING"
        for now, expected in ((100, 1), (101, 1), (102, 2), (110, 3)):
            plugin._tune_query_pending = False
            with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=now):
                plugin._query_tune(periodic=True)
            self.assertEqual(len(sent), expected)
        plugin._tune_query_pending = False
        with patch("octoprint_rme_compatibility.plugin.time.monotonic", return_value=111):
            plugin._query_tune()
        self.assertEqual(len(sent), 4)  # User controls retain immediate refresh.
