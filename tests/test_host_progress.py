import types
import unittest

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.progress import progress_command


class HostProgressTests(unittest.TestCase):
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
            plugin._progress_pending_until = 0
            plugin._sync_host_progress()
        self.assertEqual(len(sent), 2)
