"""Exercise real core action dispatch, pause timer and resume queue markers."""
import ast
import inspect
import logging
import queue
import textwrap
import threading
import types
import unittest
from unittest.mock import Mock

from octoprint.events import Events
from octoprint.util import CountedEvent
from core_compat import core, MachineCom, QueueMarker, SendQueueMarker
from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


def actual_action_dispatch():
    """Extract the unmodified monitor action branch, not a recreated dispatcher."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(MachineCom._monitor)))
    branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                  and isinstance(node.test, ast.Call)
                  and isinstance(node.test.func, ast.Attribute)
                  and isinstance(node.test.func.value, ast.Name)
                  and node.test.func.value.id == 'debugging_output'
                  and node.test.func.attr == 'startswith'
                  and node.test.args and isinstance(node.test.args[0], ast.Constant)
                  and node.test.args[0].value == 'action:')
    module = ast.parse('def dispatch(self, action):\n line="//action:"+action\n debugging_output="action:"+action\n')
    module.body[0].body.extend(branch.body)
    namespace = dict(vars(core))
    exec(compile(ast.fix_missing_locations(module), '<actual core action dispatcher>', 'exec'), namespace)
    return namespace['dispatch']


class RealLcdResume(unittest.TestCase):
    def test_host_resume_completion_reaches_marker_without_waiting_for_session_poll(self):
        p,c,_,_=self.setup_core()
        c.setPause(False)  # Host initiated: firmware still reheats/primes/unparks.
        self.assertEqual(c.STATE_RESUMING,c._state)
        self.assertTrue(c._send_queue.empty())
        self.assertGreater(c._job_queue.qsize(),0)
        actual_action_dispatch()(c,'resumed')
        # No SESSION response or unrelated command ACK should be needed to
        # reach the core's marker after physically completed firmware resume.
        self.assertEqual(1,c._send_queue.qsize())
        marker=c._send_queue.get_nowait()
        self.assertIsInstance(marker,SendQueueMarker)
        self.assertEqual(c.STATE_RESUMING,c._state)
        marker.run()
        self.assertEqual(c.STATE_PRINTING,c._state)

    def setup_core(self, real_command=False, tail_macros=False):
        c = MachineCom.__new__(MachineCom)
        c.close = Mock()
        c._state = c.STATE_PAUSED
        c._monitoring_active = c._send_queue_active = True
        c._currentFile = types.SimpleNamespace(done=False)
        c._jobLock = threading.RLock()
        c._pause_mutex = threading.RLock()
        c._action_users_mutex = threading.RLock()
        c._action_users = {}
        c._job_on_hold = CountedEvent()
        c._job_queue = queue.Queue()
        c._send_queue = queue.Queue()
        c._send_from_command_queue = Mock(return_value=False)
        c._changeState = lambda state: setattr(c, '_state', state)
        c._pauseWaitStartTime = None
        c._pauseWaitTimeLost = 0
        c._pos_autoreporting = False
        c._emergency_commands = []
        c._logger = logging.getLogger(__name__)
        c._log = c._dual_log = Mock()
        c._timeout_intervals = {'positionLogWait': 10}
        c._pause_position_timer = Mock()
        c._record_pause_data = True
        events = []
        p = RmeCompatibilityPlugin()
        p._logger = logging.getLogger(__name__)
        p._rme_comm = c
        p._state.update(supported=True, connected=True)
        p._state['session'].update(active=True, printer_state='PAUSED', pause_phase='paused')
        state_names = {c.STATE_PAUSED:'PAUSED', c.STATE_RESUMING:'RESUMING', c.STATE_PRINTING:'PRINTING', c.STATE_PAUSING:'PAUSING'}
        p._printer = types.SimpleNamespace(get_state_id=lambda:state_names.get(c._state), is_operational=lambda:True, commands=Mock())
        p._defer = lambda callback,*args:callback(*args)
        p._schedule_publish = Mock()
        p._send_priority_service = Mock()
        def resumed(suppress_script=False, **kwargs):
            events.append(Events.PRINT_RESUMED)
            if not suppress_script:
                for command in ('G1 E6', 'G1 X10', 'M117 old resume macro'):
                    c.sendCommand(command, part_of_job=True, tags={'script:beforePrintResumed', 'trigger:resume'})
                if real_command:
                    c.sendCommand('M400', part_of_job=True)
                if tail_macros:
                    for command in ('M117 old tail one', 'M117 old tail two'):
                        c.sendCommand(command, part_of_job=True, tags={'script:beforePrintResumed'})
        c._callback = types.SimpleNamespace(on_comm_print_job_resumed=resumed,
            on_comm_print_job_paused=lambda **kw:events.append(Events.PRINT_PAUSED))
        def enqueue(command,cmd_type=None,on_sent=None,tags=None):
            if isinstance(command,QueueMarker):
                c._send_queue.put(command)
                return True
            result = p.gcode_queuing_hook(c,'queuing',command,cmd_type,command.split()[0],tags=tags)
            if result == (None,):
                return False
            c._send_queue.put(command)
            return True
        c._sendCommand = enqueue
        hook_states = []
        def action_hook(*args, **kwargs):
            hook_states.append(c._state)
            p.action_command_hook(*args, **kwargs)
        c._printer_action_hooks = {'rme_compatibility':action_hook}
        return p,c,events,hook_states

    def test_actual_lcd_resume_cancels_old_timer_and_reaches_core_marker(self):
        dispatch = actual_action_dispatch()
        p,c,events,hook_states = self.setup_core()
        timer = c._pause_position_timer
        dispatch(c,'resume')
        dispatch(c,'resumed')
        self.assertEqual([c.STATE_RESUMING]*2,hook_states,'known-action hooks run AFTER core handling')
        self.assertEqual(c.STATE_RESUMING,c._state)
        timer.cancel.assert_called_once()
        self.assertIsNone(c._pause_position_timer)
        self.assertFalse(c._record_pause_data)
        c._pause_preparation_failed() # A racing old 10-second callback cannot emit PrintPaused.
        self.assertNotIn(Events.PRINT_PAUSED,events)
        p.on_event(Events.PRINT_PAUSED,{}) # Also reject an already-queued stale event.
        p.on_event(Events.PRINT_RESUMED,{})
        p._printer.commands.assert_not_called()
        record = dict(printer_state='PRINTING',pause_phase='none')
        p._state['session'].update(record)
        p._reconcile_printer_pause(record)
        self.assertEqual(c.STATE_RESUMING,c._state,'plugin must not force state or fake an ACK')
        marker = c._send_queue.get_nowait()
        self.assertIsInstance(marker,SendQueueMarker)
        marker.run()
        self.assertEqual(c.STATE_PRINTING,c._state)

    def test_resume_queue_preserves_foreign_hold_real_command_and_ack(self):
        dispatch = actual_action_dispatch()
        p,c,_,_ = self.setup_core(real_command=True)
        dispatch(c,'resume');dispatch(c,'resumed')
        record = dict(printer_state='PRINTING',pause_phase='none')
        p._state['session'].update(record)
        before = c._job_queue.qsize()
        c._job_on_hold.set()
        p._reconcile_printer_pause(record)
        self.assertEqual(before,c._job_queue.qsize())
        self.assertEqual(1,c._job_on_hold.counter)
        c._job_on_hold.clear()
        p._reconcile_printer_pause(record)
        self.assertEqual('M400',c._send_queue.get_nowait())
        self.assertEqual(c.STATE_RESUMING,c._state)
        self.assertEqual(1,c._job_queue.qsize(),'final marker must wait for the real command ACK')
        c._continue_sending() # ordinary core ACK advancement, after the real command finishes
        c._send_queue.get_nowait().run()
        self.assertEqual(c.STATE_PRINTING,c._state)

    def test_reheating_and_priming_do_not_advance_before_physical_completion(self):
        p,c,_,_=self.setup_core()
        c.setPause(False)
        before=c._job_queue.qsize()
        for _ in range(5):
            p.gcode_received_hook(c,'ok')
            p._reconcile_printer_pause(dict(printer_state='PAUSED',pause_phase='resuming'))
        self.assertEqual(before,c._job_queue.qsize())
        self.assertTrue(c._send_queue.empty())
        self.assertEqual(c.STATE_RESUMING,c._state)

    def test_periodic_display_backlog_cannot_starve_completed_resume(self):
        p,c,_,_=self.setup_core()
        c._command_queue=queue.Queue()
        c._send_from_command_queue=MachineCom._send_from_command_queue.__get__(c)
        for i in range(100):
            c._command_queue.put(('M117 ETA '+str(i),None,None,{'plugin:detailedprogress'}))
            c._command_queue.put(('M73 P40 R147',None,None,{'plugin:detailedprogress'}))
        c.setPause(False)
        self.assertTrue(c._command_queue.empty())
        self.assertTrue(c._send_queue.empty())
        actual_action_dispatch()(c,'resumed')
        marker=c._send_queue.get_nowait()
        self.assertIsInstance(marker,SendQueueMarker)
        marker.run()
        self.assertEqual(c.STATE_PRINTING,c._state)
        self.assertTrue(c._send_queue.empty())

    def test_long_suppressed_resume_script_advances_in_bounded_batches(self):
        p,c,_,_=self.setup_core()
        original=c._callback.on_comm_print_job_resumed
        def long_resume(**kwargs):
            original(**kwargs)
            for i in range(250):
                c.sendCommand('M117 old macro '+str(i),part_of_job=True,
                              tags={'script:beforePrintResumed'})
        c._callback.on_comm_print_job_resumed=long_resume
        c.setPause(False)
        actual_action_dispatch()(c,'resumed')
        marker=c._send_queue.get_nowait()
        self.assertIsInstance(marker,SendQueueMarker)
        marker.run()
        self.assertEqual(c.STATE_PRINTING,c._state)

    def test_real_command_ack_exposes_suppressed_tail_without_another_poll(self):
        p,c,_,_=self.setup_core(real_command=True,tail_macros=True)
        c._clear_to_send=CountedEvent()
        c.fakeOk=Mock()
        c.setPause(False)
        actual_action_dispatch()(c,'resumed')
        self.assertEqual('M400',c._send_queue.get_nowait())
        # Model core handling a genuine M400 ACK. Suppressing the first tail
        # must schedule advancement of the rest without another wire ACK.
        ack_count=c._clear_to_send.counter
        c._continue_sending()
        self.assertEqual(ack_count,c._clear_to_send.counter)
        c.fakeOk.assert_not_called()
        self.assertEqual(c.STATE_RESUMING,c._state)
        marker=c._send_queue.get_nowait()
        self.assertIsInstance(marker,SendQueueMarker)
        marker.run()
        self.assertEqual(c.STATE_PRINTING,c._state)

    def test_completion_and_ack_never_clear_a_foreign_job_hold(self):
        p,c,_,_=self.setup_core()
        c.setPause(False)
        before=c._job_queue.qsize()
        c._job_on_hold.set()
        actual_action_dispatch()(c,'resumed')
        p.gcode_received_hook(c,'ok')
        self.assertEqual(1,c._job_on_hold.counter)
        self.assertEqual(before,c._job_queue.qsize())
        self.assertTrue(c._send_queue.empty())
        c._job_on_hold.clear()
        c._continue_sending()  # Native hold-release path, no new SESSION poll.
        self.assertIsInstance(c._send_queue.get_nowait(),SendQueueMarker)

    def test_queued_completion_kick_expires_on_new_intent_job_or_connection(self):
        for changed in ('pause','cancel','file','generation','connection'):
            with self.subTest(changed=changed):
                p,c,_,_=self.setup_core()
                c.setPause(False)
                deferred=[]
                p._defer=lambda fn,*args:deferred.append((fn,args))
                actual_action_dispatch()(c,'resumed')
                self.assertEqual(1,len(deferred))
                callback,args=deferred.pop()
                if changed in ('pause','cancel'):
                    p._request_priority_control(changed)
                elif changed=='file':
                    c._currentFile=types.SimpleNamespace(done=False)
                elif changed=='generation':
                    p._connection_generation+=1
                else:
                    p._rme_comm=Mock()
                # Even a previous PRINTING snapshot cannot authorize this
                # worker after its physical-completion context was replaced.
                p._state['session'].update(printer_state='PRINTING',pause_phase='none')
                before=c._job_queue.qsize()
                callback(*args)
                self.assertEqual(before,c._job_queue.qsize())
                self.assertTrue(c._send_queue.empty())
                self.assertFalse(p._resume_advance_pending)

    def test_repeated_resume_kicks_coalesce_one_completion_worker(self):
        p,c,_,_=self.setup_core()
        c.setPause(False)
        deferred=[]
        p._defer=lambda fn,*args:deferred.append((fn,args))
        actual_action_dispatch()(c,'resumed')
        for _ in range(20):p._schedule_confirmed_resume_advance(c)
        self.assertEqual(1,len(deferred))
        callback,args=deferred.pop()
        callback(*args)
        self.assertFalse(p._resume_advance_pending)
        self.assertIsInstance(c._send_queue.get_nowait(),SendQueueMarker)

    def test_real_deferred_worker_reaches_marker_without_wire_traffic(self):
        p,c,_,_=self.setup_core(tail_macros=True)
        c.setPause(False)
        p._defer=RmeCompatibilityPlugin._defer.__get__(p)
        actual_action_dispatch()(c,'resumed')
        marker=c._send_queue.get(timeout=2)
        self.assertIsInstance(marker,SendQueueMarker)
        self.assertEqual(c.STATE_RESUMING,c._state)
        marker.run()
        self.assertEqual(c.STATE_PRINTING,c._state)
