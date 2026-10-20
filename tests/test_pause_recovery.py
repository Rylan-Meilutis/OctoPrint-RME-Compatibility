import unittest
import threading
from unittest.mock import Mock
from test_toolmap_gate import RmeCompatibilityPlugin, _Printer


class Comm:
    STATE_PAUSING = "PAUSING"
    STATE_PAUSED = "PAUSED"

    def __init__(self, state):
        self.state = state
        self.calls = []

    def getState(self):
        return self.state

    def setPause(self, pause, local_handling=True):
        self.calls.append((pause, local_handling))
        self.state = "PAUSING" if pause else "RESUMING"

    def _pause_preparation_done(self, **kwargs):
        self.calls.append(kwargs)

    def _changeState(self, state):
        self.state = state


class PauseRecovery(unittest.TestCase):
    def plugin(self, state="PRINTING"):
        p = RmeCompatibilityPlugin()
        p._printer = _Printer()
        p._rme_comm = Comm(state)
        p._printer.get_state_id = lambda: p._rme_comm.state
        p._state.update(connected=True, supported=True)
        p._state["session"].update(active=True, lease=1)
        p._schedule_publish = Mock()
        p._send_priority_service = Mock()
        p._defer = lambda callback, *args: callback(*args)
        return p

    def snapshot(self, p, phase, state="PAUSED"):
        p._handle_record(dict(record="session", lease=1, legacy=0,
                             printer_state=state, pause_phase=phase))

    def test_missed_action_reconciles_only_stable_pause(self):
        p = self.plugin()
        self.snapshot(p, "pausing")
        self.assertEqual(p._rme_comm.state, "PAUSING")
        self.assertEqual(p._rme_comm.calls, [(True, True)])
        self.snapshot(p, "paused")
        self.assertEqual(p._rme_comm.state, "PAUSED")
        self.assertEqual(p._rme_comm.calls[-1], dict(check_timer=False, suppress_script=True))
        self.assertIn("pause", p._firmware_completed_controls)
        self.assertEqual(p._printer.forced_commands, [])

    def test_stable_pause_releases_host_marker_wait_not_job_moves(self):
        p = self.plugin("PAUSING")
        self.snapshot(p, "paused")
        self.assertEqual(p._rme_comm.state, "PAUSED")
        calls = len(p._rme_comm.calls)
        self.snapshot(p, "paused")
        self.assertEqual(calls, len(p._rme_comm.calls))

    def test_resuming_snapshot_does_not_re_pause_or_resume_early(self):
        p = self.plugin("RESUMING")
        self.snapshot(p, "resuming")
        self.assertEqual(p._rme_comm.calls, [])
        self.snapshot(p, "none", "PRINTING")
        self.assertEqual(p._rme_comm.calls, [(False, False)])

    def test_terminal_host_and_stale_records_never_resurrect_job(self):
        for host in ("CANCELLING", "OPERATIONAL", "CLOSED", "FINISHING"):
            p = self.plugin(host)
            self.snapshot(p, "paused")
            self.assertEqual(p._rme_comm.calls, [])
        p = self.plugin()
        p._state["session"].update(printer_state="PRINTING", pause_phase="none")
        p._reconcile_printer_pause(dict(printer_state="PAUSED", pause_phase="paused"))
        self.assertEqual(p._rme_comm.calls, [])

    def test_legacy_ambiguous_paused_snapshot_is_not_completion(self):
        p = self.plugin()
        p._handle_record(dict(record="session", lease=1, printer_state="PAUSED"))
        self.assertEqual(p._rme_comm.calls, [])

    def test_cancel_and_pause_macros_cannot_retract_empty_head(self):
        p = self.plugin()
        p.gcode_script_hook(None, "gcode", "afterPrintCancelled")
        for tag in ("script:afterPrintCancelled", "script:afterPrintPaused", "script:beforePrintResumed"):
            self.assertEqual(p.gcode_queuing_hook(None, "queuing", "G1 E-6.5", None, "G1", tags={tag}), (None,))
        self.assertIsNone(p.gcode_queuing_hook(None, "queuing", "G1 E1", None, "G1", tags={"source:file"}))
        p = self.plugin()
        p._state["supported"] = False
        p.gcode_script_hook(None, "gcode", "afterPrintCancelled")
        self.assertIsNone(p.gcode_queuing_hook(None, "queuing", "G1 E-6.5", None, "G1", tags={"script:afterPrintCancelled"}))

    def test_keepalive_bypasses_missing_ok_and_preserves_fault_reason(self):
        p = self.plugin()
        p._send_session_command("@RME SESSION KEEPALIVE")
        p._send_priority_service.assert_called_with("@RME SESSION KEEPALIVE", "session_recovery")
        p._pause_reason.pause("Spaghetti detected", now=0)
        self.snapshot(p, "paused")
        self.snapshot(p, "paused")
        self.assertEqual(p._state["pause"]["reason"], "Spaghetti detected")

    def test_detected_spaghetti_reason_and_rejected_resume_are_recoverable(self):
        p = self.plugin("RESUMING")
        self.assertTrue(p._pause_reason.observe("//action:notification Spaghetti detected"))
        p._priority_controls_sent.add("resume")
        self.snapshot(p, "paused")
        self.assertEqual(p._rme_comm.state, "PAUSED")
        self.assertNotIn("resume", p._priority_controls_sent)
        self.assertEqual(p._state["pause"]["reason"], "Spaghetti detected")

    def test_recovery_does_not_consume_job_line_number_at_full_fifo(self):
        p = self.plugin()
        comm = Mock()
        comm._sendingLock = threading.RLock()
        comm._serial_encoding = "ascii"
        comm._use_up_clear.return_value = False
        comm._current_line = 200798
        for command, gcode in (("M604", "M604"), ("@RME SESSION QUERY", "@RME")):
            self.assertEqual(p._force_send_rme_control(comm, command, gcode), (None,))
            comm._do_send_without_checksum.assert_called_with(command.encode("ascii"))
        comm._do_send.assert_not_called()
        self.assertEqual(comm._current_line, 200798)


if __name__ == "__main__":
    unittest.main()
