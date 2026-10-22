import ast
import os
import time
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from octoprint_rme_compatibility import object_safety as safety


class ObjectSafetyTests(unittest.TestCase):
    def index(self, text):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(text.encode('utf8'))
            stream.flush()
            return safety.read_index(stream.name)

    def test_final_extrusion_across_all_layers_and_exact_bytes(self):
        first = 'G90\r\nM83\r\n@Object båat\r\nG1 X2 E1\r\n@Objectstop båat\r\n'
        last = '@Object båat\r\nG1 X3 E1\r\n'
        rows = self.index(first + '@Object short\nG1 X4 E2\n@Objectstop short\n' + last + '@Objectstop båat\r\nT1\nG1 X9 E1\n')
        boat, short = rows['båat'], rows['short']
        self.assertIsNone(boat['reason'])
        self.assertGreater(boat['last_print_end'], short['last_print_end'])
        self.assertFalse(safety.status(boat, len(first.encode()), True)['completed'])
        self.assertFalse(safety.status(short, short['last_print_end'], True)['cancellable'])
        self.assertTrue(safety.status(boat, short['last_print_end'], True)['cancellable'])
        self.assertFalse(safety.status(boat, boat['last_print_end'], True)['cancellable'])

    def test_shared_tool_and_prime_blocks_are_outside_spans(self):
        rows = self.index('G90\nM83\n@Object A\nG1 X1 E1\n@Objectstop A\n'
                          'T1\nM109 S220\n;TYPE:Prime tower\nG1 X90 E2\n'
                          '@Object A\nG1 X2 E1\n@Objectstop A\n')
        self.assertIsNone(rows['A']['reason'])
        for block in ('T1\n', ';TYPE:Prime tower\n', '; CP TOOLCHANGE START\n', 'G91\nG1 X2 E1\n'):
            with self.subTest(block=block):
                bad = self.index('M83\n@Object A\nG1 X1 E1\n' + block + '@Objectstop A\n')
                self.assertFalse(safety.status(bad['A'], 0, True)['cancellable'])

    def test_unlabeled_unclosed_mismatched_and_idle_fail_closed(self):
        self.assertEqual({}, self.index('M83\nG1 X1 E1\nT2\n'))
        for tail in ('', '@Objectstop B\n'):
            row = self.index('M83\n@Object A\nG1 X1 E1\n' + tail)['A']
            self.assertFalse(safety.status(row, 0, True)['cancellable'])
        row = self.index('M83\n@Object A\nG1 X1 E1\n@Objectstop A\n')['A']
        for position, active in ((None, True), (0, False)):
            self.assertFalse(safety.status(row, position, active)['cancellable'])
        self.assertFalse(safety.status(None, 0, True)['cancellable'])

    def test_changed_file_invalidates_index(self):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(b'M83\n@Object A\nG1 X1 E1\n@Objectstop A\n'); stream.flush()
            self.assertIn('A', safety.read_index(stream.name))
            stream.write(b'@Object B\nG1 X2 E1\n@Objectstop B\n'); stream.flush()
            self.assertIn('B', safety.read_index(stream.name))

    def test_adapter_guards_cached_helpers_and_preserves_nonmotion(self):
        class Upstream:
            def __init__(self):
                self.allowed = []
                self.reptag = 'Object'
                self.object_list = [dict(id=0, object='A', cancelled=False)]
                self.skipping = True
                self.startskip = True
                self.endskip = False
            def _get_entry_byid(self, value):
                return self.object_list[0]
            def cancel_object(self, value):
                self._get_entry_byid(value)['cancelled'] = True
            def _skip_allow(self, cmd):
                return (None,)
        impl = Upstream()
        cached_helper = impl.cancel_object
        error = [None]
        self.assertTrue(safety.install(impl, lambda: True, lambda name: error[0]))
        safety.prepare_queue(impl)
        self.assertTrue(impl.allowed)
        for cmd in ('T1', 'M104 S220', 'M109 S220', 'G92 E0', 'M83', 'G90', 'M400'):
            self.assertEqual(cmd, impl._skip_allow(cmd))
        for cmd in ('G1 X2 E1', 'G2 X2 I1 E1', 'N3 G0 X2*42'):
            self.assertEqual((None,), impl._skip_allow(cmd))
        error[0] = 'Object finished'
        with self.assertRaises(Exception) as raised:
            cached_helper(0)
        self.assertIn('Object finished', str(raised.exception))
        self.assertFalse(impl.object_list[0]['cancelled'])
        safety.stop_span(impl)
        self.assertFalse(impl.skipping)
        self.assertFalse(impl.startskip)
        self.assertTrue(impl.endskip)

    def test_real_benchy_when_available(self):
        path = Path(__file__).resolve().parents[2] / 'Prusa-Firmware-Buddy' / '3dbenchy-dual-color-bambu_0.2mm_PLA_Prusa CORE One HF_1h41m.gcode'
        if not path.exists():
            self.skipTest('User-provided Benchy is not distributed with the plugin')
        rows = safety.read_index(path)
        self.assertEqual(1, len(rows))
        row = next(iter(rows.values()))
        self.assertIsNone(row['reason'])
        self.assertTrue(safety.status(row, 0, True)['cancellable'])
        self.assertFalse(safety.status(row, row['last_print_end'], True)['cancellable'])

    def test_real_benchy_through_upstream_filter_when_available(self):
        source = Path('/tmp/rme-cancelobject-upstream.py')
        path = Path(__file__).resolve().parents[2] / 'Prusa-Firmware-Buddy' / '3dbenchy-dual-color-bambu_0.2mm_PLA_Prusa CORE One HF_1h41m.gcode'
        if not source.exists() or not path.exists():
            self.skipTest('Read-only upstream source and user Benchy not available')
        tree = ast.parse(source.read_text())
        methods = [method for cls in tree.body if isinstance(cls, ast.ClassDef)
                   for method in cls.body if isinstance(method, ast.FunctionDef)
                   and method.name in ('check_queue', 'check_atcommand', '_skip_allow')]
        self.assertEqual(3, len(methods))
        namespace = {'time': time}
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), 'exec'), namespace)
        upstream = type('ActualUpstreamFilter', (), {method.name: namespace[method.name] for method in methods})()
        row = dict(id=0, object=next(iter(safety.read_index(path))), cancelled=True)
        upstream._get_entry_byid = lambda value: row
        upstream.cancel_object = Mock()
        upstream.object_list = [row]
        upstream._get_entry = lambda name: row if name == row['object'] else None
        upstream._printer = SimpleNamespace(is_printing=lambda: True)
        upstream._settings = SimpleNamespace(get=lambda key: False)
        upstream._console_logger = Mock()
        upstream.parser = SimpleNamespace(is_extrusion_move=lambda cmd: None, parse_move_args=lambda cmd: None)
        upstream._updatedisplay = Mock()
        upstream.reptag = 'Object'
        upstream.allowed = []; upstream.allowedregex = []
        upstream.beforegcode = []; upstream.aftergcode = []
        upstream.skipping = upstream.startskip = upstream.endskip = upstream.trackE = False
        upstream.active_object = 'None'; upstream.lastE = 0; upstream.objects_known = True
        self.assertTrue(safety.install(upstream, lambda: True, lambda name: None))
        safety.prepare_queue(upstream)
        skipped = shared = tools = 0
        for line in path.read_text().splitlines():
            stop = safety.STOP.fullmatch(line)
            start = safety.START.fullmatch(line)
            if stop:
                safety.stop_span(upstream)
                upstream.check_atcommand(None, 'queuing', 'Objectstop', stop.group(1))
                continue
            if start:
                upstream.check_atcommand(None, 'queuing', 'Object', start.group(1))
                continue
            code = line.split(';', 1)[0].strip()
            if not code: continue
            skipping = upstream.skipping
            result = upstream.check_queue(None, 'queuing', code, None, None, set())
            if safety.motion_command(code):
                if skipping:
                    self.assertEqual((None,), result)
                    skipped += 1
                else:
                    self.assertIn(code, result if isinstance(result, list) else [result])
                    shared += 1
            if code.startswith('T') and code[1:2].isdigit():
                self.assertIn(code, result if isinstance(result, list) else [result])
                tools += 1
        self.assertGreater(skipped, 1000)
        self.assertGreater(shared, 100)
        self.assertGreater(tools, 2)


class ObjectContextTests(unittest.TestCase):
    def test_cancel_api_rejects_stale_completed_and_ambiguous_selection(self):
        from test_toolmap_gate import RmeCompatibilityPlugin
        from octoprint.access.permissions import Permissions
        Permissions.CONTROL = Permissions.PRINT = SimpleNamespace(can=lambda: True)
        plugin = RmeCompatibilityPlugin()
        impl = SimpleNamespace(object_list=[dict(id=1, object='A')], cancel_object=Mock())
        plugin._cancel_object_implementation = lambda: impl
        plugin._object_context = lambda: dict(file='a.gcode', job_id='current', active=True)
        plugin._object_cancel_error = lambda name, expected=None: None
        plugin._public_state = lambda: {}
        data = dict(id=1, file='a.gcode', job_id='current')
        self.assertEqual(409, plugin.on_api_command('cancel_object', dict(data, job_id='old'))[1])
        impl.cancel_object.assert_not_called()
        plugin._object_cancel_error = lambda name, expected=None: 'Object finished'
        self.assertEqual(409, plugin.on_api_command('cancel_object', data)[1])
        impl.cancel_object.assert_not_called()
        plugin._object_cancel_error = lambda name, expected=None: None
        self.assertEqual(409, plugin.on_api_command('cancel_object', dict(data, id=2))[1])
        plugin.on_api_command('cancel_object', data)
        impl.cancel_object.assert_called_once_with(1)

    def test_job_identity_and_final_position_are_rechecked(self):
        from test_toolmap_gate import RmeCompatibilityPlugin
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(b'M83\n@Object A\nG1 X1 E1\n@Objectstop A\n'); stream.flush()
            current = SimpleNamespace(getFilename=lambda: stream.name, getStartTime=lambda: 1,
                                      getFilepos=lambda: 0)
            plugin = RmeCompatibilityPlugin()
            plugin._printer = SimpleNamespace(
                get_current_data=lambda: {'job': {'file': {'origin': 'local', 'path': 'A.gcode'}}},
                _comm=SimpleNamespace(_currentFile=current), is_printing=lambda: True, is_paused=lambda: False)
            plugin._file_manager = SimpleNamespace(path_on_disk=lambda *args: stream.name)
            plugin._logger = Mock()
            first = plugin._object_context()['job_id']
            self.assertIsNone(plugin._object_cancel_error('A'))
            current.getFilepos = lambda: os.path.getsize(stream.name)
            self.assertIn('finished', plugin._object_cancel_error('A'))
            current.getStartTime = lambda: 2
            self.assertNotEqual(first, plugin._object_context()['job_id'])
            self.assertIn('changed', plugin._object_cancel_error('A', first))
            current.getFilepos = lambda: None
            self.assertIn('unavailable', plugin._object_cancel_error('A'))


class DownloadCorrelationTests(unittest.TestCase):
    def test_request_correlation_survives_completion(self):
        from test_toolmap_gate import RmeCompatibilityPlugin
        plugin = RmeCompatibilityPlugin()
        plugin._require_storage = Mock()
        plugin._persist_and_publish = Mock()
        plugin._publish = Mock()
        plugin._file_service = SimpleNamespace(download_file=lambda *args, **kwargs: {'size': 123})
        plugin._download_directory = '/tmp'
        immediate = lambda target, **kwargs: SimpleNamespace(start=target)
        with patch('octoprint_rme_compatibility.plugin.threading.Thread', side_effect=immediate):
            queued = plugin._start_storage_download('part.gcode', 'device', 'browser-123')
        ready = plugin._state['storage']['download']
        self.assertEqual(queued['id'], ready['id'])
        self.assertEqual('ready', ready['status'])
        self.assertEqual('browser-123', ready['request_id'])
        self.assertEqual('browser-123', plugin._state['storage']['downloads'][0]['request_id'])

    def test_invalid_correlation_does_not_start_transfer(self):
        from test_toolmap_gate import RmeCompatibilityPlugin
        plugin = RmeCompatibilityPlugin()
        plugin._require_storage = Mock()
        with patch('octoprint_rme_compatibility.plugin.threading.Thread') as worker:
            for request_id in ('', '../browser', 'x' * 81, {}, 1):
                with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                    plugin._start_storage_download('part.gcode', 'device', request_id)
            worker.assert_not_called()
