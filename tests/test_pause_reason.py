import unittest
from octoprint_rme_compatibility.pause_reason import PauseReason


class PauseReasonTests(unittest.TestCase):
    def test_preserves_fault_through_generic_paused_action(self):
        reason = PauseReason()
        self.assertTrue(reason.observe("//action:notification Loadcell detected filament runout"))
        reason.observe("//action:paused firmware_pause")
        self.assertEqual(reason.snapshot()["reason"], "Loadcell detected filament runout")
        self.assertTrue(reason.active)
        reason.resume()
        self.assertFalse(reason.active)
        self.assertIn("runout", reason.reason)

    def test_stale_notice_is_not_blame_for_new_pause(self):
        reason = PauseReason()
        reason.evidence("Stuck filament", "firmware", now=1)
        self.assertEqual(reason.pause(now=100)["source"], "unreported")
        reason.evidence("Wastebin full", "firmware", now=101)
        self.assertEqual(reason.reason, "Wastebin full")

    def test_job_reset_and_normal_chatter(self):
        reason = PauseReason()
        for line in ("echo:busy: processing", "Loading filament", "Waiting for hotend", "Print paused"):
            self.assertFalse(reason.observe(line))
        reason.pause("Operator requested pause")
        self.assertEqual(reason.reason, "Operator requested pause")
        reason.reset()
        self.assertEqual(reason.snapshot(), {"active": False, "reason": "", "source": ""})

    def test_bounded_message(self):
        reason = PauseReason()
        reason.pause("a" * 10000)
        self.assertEqual(len(reason.reason), 300)
