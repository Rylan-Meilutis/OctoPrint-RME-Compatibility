"""Run separately in the real OctoPrint environment, without serial hardware."""
import logging
import queue
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
from octoprint.util import CountedEvent
from octoprint.util.comm import MachineCom, PrintingGcodeFileInformation, QueueMarker
from octoprint.filemanager.storage import LocalFileStorage
from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


class RealControlStartup(unittest.TestCase):
    def test_real_start_with_suppressed_scripts_and_delayed_events_holds_before_file(self):
        for opted_out in (False, True):
            with self.subTest(opted_out=opted_out), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'part.gcode'
                path.write_text(('@pause\n; skip_validation\n; skip-rme-toolmapping\n' if opted_out else
                                 '; filament_type = ASA;ASA\nM75\nG28 XY\nM870 C\n'))
                p = RmeCompatibilityPlugin()
                p._logger = logging.getLogger(__name__)
                p._settings = Mock()
                p._settings.get.return_value = None
                p._settings.get_boolean.return_value = True
                p._settings.get_int.return_value = 0
                p._state.update(supported=True, connected=True, machine={'logical_tools': 2})
                p._state['session']['tool_review'] = 1
                p._file_manager = types.SimpleNamespace(
                    path_in_storage=lambda origin, name: Path(name).name,
                    path_on_disk=lambda origin, name: str(path))
                p._persist_and_publish = Mock()
                deferred = []
                p._defer = lambda callback, *args: deferred.append((callback, args))
                p._prepare_mesh_area = Mock()
                p._prepare_chamber_job = lambda: setattr(p, '_chamber_job_command', '@RME CHAMBER JOB allow_cooling=0')
                p._prepare_completion_move = Mock()
                c = MachineCom.__new__(MachineCom)
                c.close = Mock()  # No transport/thread lifecycle in this harness.
                c._emergency_commands = set()
                c._trigger_error = Mock(side_effect=AssertionError('Unexpected connector error'))
                c._state = c.STATE_OPERATIONAL
                c._currentFile = PrintingGcodeFileInformation(str(path))
                c._jobLock = threading.RLock()
                c._job_on_hold = CountedEvent()
                c._job_queue = queue.Queue()
                c._send_queue = queue.Queue()
                c._monitoring_active = True
                c._send_queue_active = True
                c._heating = False
                c._logger = logging.getLogger(__name__)
                c._changeState = lambda state: setattr(c, '_state', state)
                c.isSdFileSelected = lambda: False
                c.isSdPrinting = lambda: False
                c._send_from_command_queue = lambda: False
                c._callback = types.SimpleNamespace(on_comm_print_job_started=lambda **kw: None,
                                                     on_comm_progress=lambda: None)
                # Use real startPrint, resetLineNumbers, sendCommand, counted
                # hold and queue selection. Only the physical serial enqueue
                # is replaced; the same hook receives the real start tags.
                def enqueue(command, cmd_type=None, on_sent=None, tags=None):
                    if not isinstance(command, QueueMarker):
                        result = p.gcode_queuing_hook(c, 'queuing', command, cmd_type,
                                                     str(command).split()[0], tags=tags)
                        self.assertIsNone(result)
                    c._send_queue.put(command)
                    return True
                c._sendCommand = enqueue
                p._rme_comm = c
                p._printer = types.SimpleNamespace(
                    set_job_on_hold=c.set_job_on_hold,
                    is_operational=c.isOperational,
                    commands=lambda commands, **kw: [c.sendCommand(cmd) for cmd in commands],
                    get_current_data=lambda: {'job': {'file': {'origin': 'local', 'path': path.name}}})
                try:
                    c.startPrint()
                    self.assertTrue(c.job_on_hold)
                    self.assertEqual(c._currentFile.getFilepos(), 0)
                    # The sending hook returns before the worker runs: it must
                    # not wait on a preparation worker holding another lock.
                    callback, args = deferred.pop(0)
                    callback(*args)
                    sent = list(c._send_queue.queue)
                    if opted_out:
                        self.assertFalse(c.job_on_hold)
                        self.assertEqual(sent, ['M110 N0'])
                    else:
                        self.assertTrue(c.job_on_hold)
                        self.assertTrue(any(str(cmd).startswith('@RME REVIEW BEGIN') for cmd in sent))
                        self.assertTrue(any(str(cmd).startswith('@RME REVIEW SHOW') for cmd in sent))
                        self.assertIn('M110 N0', sent)
                        self.assertNotIn('M75', sent)
                        self.assertNotIn('G28 XY', sent)
                        self.assertNotIn('M870 C', sent)
                        self.assertEqual(c._currentFile.getFilepos(), 0)
                        # Draining the control commands still cannot read the
                        # file or its state-changing start marker while held.
                        while not c._send_queue.empty():
                            c._send_queue.get_nowait()
                        self.assertFalse(c._continue_sending())
                        self.assertEqual(c._currentFile.getFilepos(), 0)
                        prompt = p._state['prompt']
                        p.on_event('PrintStarted', {'origin': 'local', 'path': path.name})
                        self.assertIs(prompt, p._state['prompt'])
                        self.assertEqual(c._job_on_hold.counter, 1)
                finally:
                    c._currentFile.close()

    def test_real_connector_start_token_changes_for_repeated_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.gcode'
            path.write_text('G28\nT1\n')
            selected = PrintingGcodeFileInformation(str(path))
            comm = types.SimpleNamespace(_currentFile=selected)
            plugin = RmeCompatibilityPlugin()
            self.assertIsNone(plugin._print_start_token(comm))
            selected.start()
            first = plugin._print_start_token(comm)
            self.assertIsNotNone(first)
            self.assertEqual(first, plugin._print_start_token(comm))
            selected.close()
            selected.start()
            self.assertNotEqual(first, plugin._print_start_token(comm))
            selected.close()

    def test_real_selected_file_overrides_stale_ui_control_and_print_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileStorage(directory)
            control = Path(storage.path_on_disk('control.gcode'))
            real = Path(storage.path_on_disk('real.gcode'))
            control.write_text('@pause\n; skip_validation\n; skip-rme-toolmapping\n')
            real.write_text('G28\nT1\nG1 X100 E1\n')
            plugin = RmeCompatibilityPlugin()
            plugin._logger = logging.getLogger(__name__)
            plugin._file_manager = types.SimpleNamespace(
                path_in_storage=lambda origin, path: storage.path_in_storage(path),
                path_on_disk=lambda origin, path: storage.path_on_disk(path))
            for selected, stale, executable in ((control, real, False), (real, control, True)):
                with self.subTest(selected=selected.name):
                    comm = types.SimpleNamespace(_currentFile=PrintingGcodeFileInformation(str(selected)))
                    plugin._printer = types.SimpleNamespace(get_current_data=lambda: {
                        'job': {'file': {'origin': 'local', 'path': stale.name}}})
                    plugin._preflight_file_info = plugin._startup_file_info(comm)
                    self.assertEqual(selected.name, plugin._preflight_file_info['path'])
                    self.assertEqual(executable, plugin._selected_job_has_executable_gcode())


if __name__ == '__main__':
    unittest.main()
