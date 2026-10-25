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
        for line in ("echo:busy: processing", "Loading filament", "Waiting for hotend", "Print paused",
                     "Loadcell filament runout detection off", "Loadcell filament runout detection on"):
            self.assertFalse(reason.observe(line))
        reason.pause("Operator requested pause")
        self.assertEqual(reason.reason, "Operator requested pause")
        reason.reset()
        self.assertEqual(reason.snapshot(), {"active": False, "reason": "", "source": ""})

    def test_bounded_message(self):
        reason = PauseReason()
        reason.pause("a" * 10000)
        self.assertEqual(len(reason.reason), 300)

    def test_normal_tool_change_is_not_a_pause_cause(self):
        reason = PauseReason()
        for message in ("Tool change in progress", "Tool change complete", "Waiting", "Homing"):
            self.assertFalse(reason.evidence(message, "Firmware workflow"))
        self.assertEqual(reason.pause("Tool change in progress")["source"], "unreported")
        self.assertTrue(reason.evidence("Tool change failed: lock not engaged", "Firmware workflow"))
        self.assertIn("lock not engaged", reason.reason)

    def test_obico_detection_survives_generic_pause_and_expires(self):
        reason = PauseReason()
        self.assertTrue(reason.observe_obico(dict(cmd="pause", initiator="system"), now=1))
        self.assertFalse(reason.active)
        snapshot = reason.pause(now=2)
        self.assertIn("spaghetti detection", snapshot['reason'])
        self.assertEqual(snapshot['source'], 'Obico automatic failure detection')
        reason.resume()
        self.assertEqual(reason.pause(now=3)['source'], 'unreported')
        reason.observe_obico(dict(cmd="pause", initiator="system"), now=4)
        self.assertEqual(reason.pause(now=100)['source'], 'unreported')

    def test_obico_manual_and_unknown_commands_are_not_spaghetti(self):
        for initiator in ('user', 'api', None, 'unknown'):
            reason = PauseReason()
            reason.observe_obico(dict(cmd='pause', initiator=initiator))
            self.assertNotIn('spaghetti', reason.pause()['reason'])
        for command in (None, [], {}, dict(cmd='resume', initiator='system'), dict(cmd='cancel')):
            self.assertFalse(PauseReason().observe_obico(command))

    def test_obico_late_evidence_updates_an_existing_unknown_pause(self):
        reason = PauseReason()
        reason.pause()
        reason.observe_obico(dict(cmd='pause', initiator='system'))
        self.assertIn('spaghetti detection', reason.snapshot()['reason'])
        reason.reset()
        self.assertEqual(reason.pause()['source'], 'unreported')
