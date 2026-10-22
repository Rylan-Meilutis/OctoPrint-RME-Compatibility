import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from octoprint_rme_compatibility.startup_analysis import BackgroundAnalysis
import test_tool_review as review


class StartupAnalysisTests(unittest.TestCase):
    def test_cold_or_running_scan_never_blocks_startup_and_never_mutates_job(self):
        cache = BackgroundAnalysis()
        started, release = threading.Event(), threading.Event()
        def slow(path):
            started.set()
            release.wait(2)
            return 'old result'
        with tempfile.NamedTemporaryFile() as stream:
            try:
                begin = time.monotonic()
                self.assertIsNone(cache.request(stream.name, 'end', slow))
                self.assertLess(time.monotonic() - begin, .2)
                self.assertTrue(started.wait(1))
                worker = cache.worker
                self.assertIsNone(cache.request(stream.name, 'end', slow))
                self.assertIs(cache.worker, worker)
                # Modification invalidates even a completed result.
                stream.write(b'changed'); stream.flush()
                release.set(); worker.join(2)
                self.assertFalse(cache.results)
                self.assertIsNone(cache.request(stream.name, 'end', lambda path: 'new'))
                worker = cache.worker
                if worker is not None: worker.join(2)
                self.assertEqual('new', cache.request(stream.name, 'end', slow))
            finally:
                release.set()

    def test_pending_work_and_cache_are_bounded(self):
        cache = BackgroundAnalysis()
        release = threading.Event()
        with tempfile.NamedTemporaryFile() as stream:
            cache.request(stream.name, 'blocked', lambda path: release.wait(2))
            worker = cache.worker
            try:
                for index in range(20):
                    cache.request(stream.name, str(index), lambda path: None)
                with cache.lock:
                    self.assertLessEqual(len(cache.pending), 4)
            finally:
                release.set(); worker.join(2)
            self.assertLessEqual(len(cache.results), 8)

    def test_approved_commit_enters_serial_mode_before_release_and_optional_save(self):
        p = review.ToolReview().plugin()
        p._preflight_gate_started = True
        review.ToolReview().submit(p)
        events, deferred = [], []
        p._printer.commands = lambda commands, **kwargs: events.extend(commands)
        p._printer.set_job_on_hold = lambda held, **kwargs: events.append(('hold', held))
        p._settings.save = lambda: events.append('save')
        p._defer = lambda callback, *args: deferred.append((callback, args))
        p._handle_tool_review(dict(id=123, state='complete', revision=19))
        self.assertEqual(['M75', ('hold', False)], events)
        self.assertFalse(p._toolmap_hold_active)
        self.assertEqual(1, len(deferred))
        deferred[0][0](*deferred[0][1])
        self.assertEqual('save', events[-1])

    def test_stale_or_uncommitted_review_never_enters_serial_mode(self):
        for record in (dict(id=122, state='complete', revision=19),
                       dict(id=123, state='complete', revision=18)):
            p = review.ToolReview().plugin()
            p._preflight_gate_started = True
            p._handle_tool_review(record)
            self.assertNotIn(['M75'], p._printer.command_batches)
            self.assertTrue(p._toolmap_hold_active)

    def test_blocked_analyses_use_slicer_fallbacks_without_waiting(self):
        p = review.ToolReview().plugin()
        p._state['machine'].update(mesh_area=1, x_max=248, y_max=205)
        p._plugin_manager.plugins['octopod'] = type('Octopod', (), {'enabled': True})()
        p._printer.get_current_data = lambda: {'job': {'file': {'origin': 'local', 'path': 'part.gcode'}}}
        release = threading.Event()
        with tempfile.NamedTemporaryFile() as stream:
            p._file_manager = type('Files', (), {'path_on_disk': lambda *args: stream.name})()
            with patch('octoprint_rme_compatibility.plugin.analyze_mesh_area', side_effect=lambda *args: release.wait(2)), patch('octoprint_rme_compatibility.plugin.analyze_end_move', return_value='late'):
                try:
                    start = time.monotonic()
                    p._prepare_mesh_area()
                    p._prepare_completion_move()
                    self.assertLess(time.monotonic() - start, .2)
                    self.assertIsNone(p._mesh_area_command)
                    self.assertIsNone(p._completion_move)
                    worker = p._startup_analysis.worker
                    release.set()
                    if worker is not None: worker.join(2)
                    self.assertIsNone(p._mesh_area_command)
                    self.assertIsNone(p._completion_move)
                finally:
                    release.set()

    def test_async_defaults_cannot_overwrite_newer_mapping(self):
        p = review.ToolReview().plugin()
        deferred = []
        p._defer = lambda callback, *args: deferred.append(callback)
        p._save_toolmap_defaults_later({0: 1}, True)
        p._save_toolmap_defaults_later({0: 2}, False)
        deferred[1](); deferred[0]()
        self.assertEqual({'0': 2}, p._settings.get(['default_toolmap']))
        self.assertFalse(p._settings.get_boolean(['default_toolmap_enabled']))
