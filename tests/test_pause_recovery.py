import unittest
import threading
import logging
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
from test_toolmap_gate import RmeCompatibilityPlugin, _Printer
from octoprint.events import Events


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
    def test_non_rme_or_disconnected_resume_does_not_mutate_core_or_banner(self):
        for key in ('supported', 'connected'):
            p = self.plugin('RESUMING')
            p._state[key] = False
            p._state['workflow'] = dict(workflow='filament_runout', state='waiting')
            p._pause_reason.pause('Filament change required')
            p._rme_comm._pause_mutex = threading.RLock()
            timer = p._rme_comm._pause_position_timer = Mock()
            p._rme_comm._record_pause_data = True
            p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
            p._confirmed_firmware_resume(p._rme_comm)
            timer.cancel.assert_not_called()
            self.assertTrue(p._rme_comm._record_pause_data)
            self.assertIsNotNone(p._state['workflow'])
            self.assertTrue(p._pause_reason.active)
            self.assertEqual(0, p._control_revision)

    def test_resume_does_not_clear_unrelated_workflow(self):
        p = self.plugin('RESUMING')
        p._state['workflow'] = dict(workflow='chamber_temperature', state='active')
        p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
        self.assertEqual('chamber_temperature', p._state['workflow']['workflow'])

    def test_confirmed_lcd_resume_retires_pause_timer_and_old_runout_banner(self):
        p = self.plugin('RESUMING')
        p._pause_reason.pause('Filament change required')
        p._state['workflow'] = dict(workflow='filament_runout', state='waiting', message='Filament change required')
        p._state['prompt'] = dict(workflow='filament_runout', actions=['Continue'])
        p._rme_comm._pause_mutex = threading.RLock()
        timer = p._rme_comm._pause_position_timer = Mock()
        p._rme_comm._record_pause_data = True
        p.action_command_hook(p._rme_comm, '//action:resume', 'resume')
        self.assertIsNotNone(p._state['workflow'])
        self.assertTrue(p._rme_comm._record_pause_data)
        p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
        timer.cancel.assert_called_once()
        self.assertIsNone(p._rme_comm._pause_position_timer)
        self.assertFalse(p._rme_comm._record_pause_data)
        self.assertIsNone(p._state['workflow'])
        self.assertIsNone(p._state['prompt'])
        self.assertFalse(p._state['pause']['active'])
        p.on_event(Events.PRINT_PAUSED, {}) # old timer's event arrived late
        self.assertEqual([], p._printer.forced_commands)
        self.assertFalse(p._state['pause']['active'])

    def test_resume_request_keeps_runout_reason_while_firmware_is_heating(self):
        p = self.plugin('RESUMING')
        p._state['workflow'] = dict(workflow='filament_runout', state='waiting')
        p._state['pause'] = p._pause_reason.pause('Filament change required')
        p.on_event(Events.PRINT_RESUMED, {})
        self.assertTrue(p._state['pause']['active'])
        self.assertIsNotNone(p._state['workflow'])
        p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
        self.assertFalse(p._state['pause']['active'])
        self.assertIsNone(p._state['workflow'])

    def test_old_tagged_pause_does_not_echo_after_resume_but_explicit_pause_works(self):
        p = self.plugin('RESUMING')
        p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
        p.gcode_queuing_hook(p._rme_comm, 'queuing', 'M117 old pause', None, 'M117', tags={'trigger:pause'})
        self.assertEqual([], p._printer.forced_commands)
        p.gcode_queuing_hook(p._rme_comm, 'queuing', 'M601', None, 'M601', tags={'source:api'})
        self.assertEqual('M601', p._printer.forced_commands[-1][0])

    def test_stale_deferred_pause_is_invalidated_but_new_operator_pause_survives(self):
        p = self.plugin('RESUMING')
        deferred = []
        p._defer = lambda callback, *args: deferred.append((callback, args))
        p._request_priority_control('pause')
        old = deferred.pop()
        p.action_command_hook(p._rme_comm, '//action:resume', 'resume')
        p._request_priority_control('pause') # genuinely new operator intent
        fresh = deferred.pop()
        p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
        old[0](*old[1])
        self.assertEqual([], p._printer.forced_commands)
        fresh[0](*fresh[1])
        self.assertEqual('M601', p._printer.forced_commands[-1][0])
        p._force_send_rme_control = Mock(return_value=(None,))
        _, tags = p._printer.forced_commands[-1]
        self.assertEqual((None,), p.gcode_queuing_hook(p._rme_comm, 'queuing', 'M601', None, 'M601', tags=tags))
        p._force_send_rme_control.assert_called_once()
        p.action_command_hook(p._rme_comm, '//action:resume', 'resume')
        p.gcode_queuing_hook(p._rme_comm, 'queuing', 'M601', None, 'M601', tags=tags)
        p._force_send_rme_control.assert_called_once() # stale queued fast command cannot hit the wire

    def test_old_snapshot_cannot_re_pause_after_lcd_resume(self):
        p = self.plugin('RESUMING')
        record = dict(printer_state='PAUSED', pause_phase='pausing')
        p._state['session'].update(record)
        revision = p._control_revision
        p.action_command_hook(p._rme_comm, '//action:resumed', 'resumed')
        p._reconcile_printer_pause(record, revision)
        self.assertEqual([], p._rme_comm.calls)
        p.on_event(Events.PRINT_RESUMED, {})
        p._rme_comm.state = 'PAUSING'
        p.on_event(Events.PRINT_RESUMED, {}) # stale opposite callback
        self.assertEqual([], p._printer.forced_commands)

    def test_failed_tool_offset_calibration_retains_real_firmware_action(self):
        p = self.plugin()
        p._send_priority_service = Mock()
        p._handle_record(dict(record='event', seq=1, type='workflow', workflow='indx_tool_offset_calibration', state='failed', message='Tool offset calibration'))
        self.assertEqual('waiting', p._state['workflow']['state'])
        self.assertIn('has not passed', p._state['workflow']['message'])
        p._send_priority_service.assert_called_with('@RME DIALOG QUERY', 'dialog_query')
        p._handle_record(dict(record='dialog', workflow='indx_tool_offset_calibration', phase='failed', state='waiting'))
        p._handle_record(dict(record='prompt', actions=['Continue']))
        self.assertEqual(['Continue'], p._state['prompt']['actions'])
        self.assertIn('closes this failed result', p._state['prompt']['message'])
        self.assertEqual('indx_tool_offset_calibration', p._state['prompt']['workflow'])
        p._handle_record(dict(record='prompt', actions=['Continue', 'Cancel', 'Retry']))
        self.assertEqual(['Continue', 'Cancel', 'Retry'], p._state['prompt']['actions'])
        self.assertNotIn('closes this failed result', p._state['prompt']['message'])
        p._handle_record(dict(record='dialog', workflow='printer', phase='none', state='idle'))
        p._handle_record(dict(record='prompt', actions=[]))
        self.assertIsNone(p._state['prompt'])

    def test_inert_continuousprint_helpers_do_not_control_a_firmware_job(self):
        p = self.plugin('PAUSED')
        p._state['session']['printer_state'] = 'IDLE'
        path = 'ContinuousPrint/tmp/continuousprint_start_print.gcode'
        p._printer.get_current_data = lambda: {'job': {'file': {'origin': 'local', 'path': path}}}
        with tempfile.NamedTemporaryFile(mode='w+') as stream:
            p._file_manager = SimpleNamespace(path_on_disk=lambda *a: stream.name)
            stream.write('@pause\n;skip-rme-toolmapping\n;skip_validation\n')
            stream.flush()
            self.assertTrue(p._selected_job_is_host_control_macro())
            for action in ('pause', 'resume', 'cancel'):
                self.assertFalse(p._request_priority_control(action))
            p._state['machine']['host_progress'] = 1
            p._send_command = Mock()
            p._sync_host_progress()
            p._send_command.assert_not_called()
            self.assertEqual(p._printer.forced_commands, [])
            p._state['session']['printer_state'] = 'PAUSED'
            self.assertFalse(p._selected_job_is_host_control_macro())
            self.assertTrue(p._request_priority_control('cancel'))
            p._state['session']['printer_state'] = 'IDLE'
            stream.write('G1 X20 E1\n'); stream.flush()
            self.assertFalse(p._selected_job_is_host_control_macro())
            path = 'real_print.gcode'
            self.assertFalse(p._selected_job_is_host_control_macro())

    def test_terminal_cancel_snapshot_completes_core_callback_even_while_filtering(self):
        for state in ('STOPPED', 'FINISHED', 'IDLE'):
            p = self.plugin('CANCELLING')
            p._logger = logging.getLogger('cancel-reconcile-test')
            p._rme_comm._currentFile = SimpleNamespace(done=False)
            p._rme_comm._cancel_preparation_done = Mock(side_effect=lambda **kw: setattr(p._rme_comm._currentFile, 'done', True))
            p._state['workflow'] = dict(workflow='filtration', state='active', phase='post_print')
            self.snapshot(p, 'none', state)
            p._rme_comm._cancel_preparation_done.assert_called_once_with(check_timer=False)
            self.snapshot(p, 'none', state)
            p._rme_comm._cancel_preparation_done.assert_called_once()
            self.assertEqual('CANCELLING', p._rme_comm.state) # Core marker owns state change.
            self.assertEqual([], p._printer.forced_commands)

    def test_cancel_snapshot_cannot_finalize_motion_cleanup_or_stale_state(self):
        for state, phase in (('PRINTING', 'none'), ('BUSY', 'none'), ('STOPPED', 'paused'), ('PAUSED', 'paused')):
            p = self.plugin('CANCELLING')
            p._rme_comm._currentFile = SimpleNamespace(done=False)
            p._rme_comm._cancel_preparation_done = Mock()
            self.snapshot(p, phase, state)
            p._rme_comm._cancel_preparation_done.assert_not_called()
    def test_collision_dialog_survives_missing_event_and_requires_explicit_response(self):
        p = self.plugin()
        self.snapshot(p, "paused", "ATTENTION")
        p._handle_record(dict(record="dialog", workflow="collision", phase="inspection", state="waiting"))
        p._handle_record(dict(record="prompt", actions=["Abort", "Continue"]))
        self.assertEqual(p._rme_comm.state, "PAUSED")
        self.assertEqual(p._state["prompt"]["workflow"], "collision")
        self.assertEqual(p._state["prompt"]["actions"], ["Abort", "Continue"])
        self.assertIn("part has moved", p._state["prompt"]["message"])
        self.assertEqual(p._printer.forced_commands, [])
        p._handle_record(dict(record="dialog", workflow="collision", phase="recovery", state="waiting"))
        p._handle_record(dict(record="prompt", actions=["Retry", "Abort"]))
        self.assertIn("homing", p._state["prompt"]["message"])
        self.assertEqual(p._state["prompt"]["actions"], ["Retry", "Abort"])
        self.assertEqual(p._printer.forced_commands, [])

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

    def test_cancel_and_finish_keepalive_cannot_wait_behind_discarded_job_ack(self):
        for host in ("CANCELLING", "FINISHING"):
            with self.subTest(host=host):
                p = self.plugin(host)
                p._send_command = Mock(side_effect=AssertionError("job ACK is missing"))
                for command in ("@RME SESSION KEEPALIVE", "@RME SESSION QUERY"):
                    p._send_session_command(command)
                    p._send_priority_service.assert_called_with(command, "session_recovery")
                p._send_command.assert_not_called()

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
