"""Control-file opt-out and job-transition races without a physical printer."""
import logging
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
from test_toolmap_gate import RmeCompatibilityPlugin, _Settings, _Printer


class ContinuousPrintStartup(unittest.TestCase):
    def plugin(self, root):
        p = RmeCompatibilityPlugin()
        p._printer = _Printer()
        p._settings = _Settings()
        p._logger = logging.getLogger(__name__)
        p._plugin_manager = types.SimpleNamespace(plugins={})
        p._persist_and_publish = Mock()
        p._schedule_publish = Mock()
        p._defer = lambda *args: None
        p._state.update(supported=True, connected=True, machine={"logical_tools": 3})
        p._printer.get_current_data = lambda: {"job": {"file": {
            "origin": "local", "path": "real.gcode"}}}
        p._file_manager = types.SimpleNamespace(
            path_in_storage=lambda origin, path: Path(path).name,
            path_on_disk=lambda origin, path: str(root / path))
        p._rme_comm = types.SimpleNamespace()
        return p

    def select(self, p, root, name):
        p._rme_comm._currentFile = types.SimpleNamespace(
            getFilename=lambda: str(root / name), getFileLocation=lambda: "local")

    def test_exact_user_marker_uses_connector_file_not_stale_ui_snapshot(self):
        for prelude in ("", "M77\n", "\ufeff"):
            with self.subTest(prelude=prelude), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "control.gcode").write_text(prelude + "@pause\n; skip_validation\n; skip-rme-toolmapping\n")
                (root / "real.gcode").write_text("G28\nT1\nG1 X100 E1\n")
                p = self.plugin(root)
                self.select(p, root, "control.gcode")
                p.gcode_script_hook(p._rme_comm, "gcode", "beforePrintStarted")
                p.on_event("PrintStarted", {"origin": "local", "path": "control.gcode"})
                self.assertFalse(p._toolmap_preflight_decision)
                self.assertEqual([], p._printer.holds)
                self.assertIsNone(p._state["prompt"])
                # A real print immediately after the helper still gets reviewed.
                self.select(p, root, "real.gcode")
                p.gcode_script_hook(p._rme_comm, "gcode", "beforePrintStarted")
                prompt = p._state["prompt"]
                self.assertEqual([True], p._printer.holds)
                # Delayed helper lifecycle events cannot clear/reopen this review.
                for event in ("PrintStarted", "PrintPaused", "PrintResumed", "PrintCancelling",
                              "PrintDone", "PrintFailed", "PrintCancelled"):
                    p.on_event(event, {"origin": "local", "path": "control.gcode"})
                    self.assertIs(prompt, p._state["prompt"])
                    self.assertTrue(p._toolmap_hold_active)
                p.on_event("PrintStarted", {"origin": "local", "path": "real.gcode"})
                self.assertIs(prompt, p._state["prompt"])
                self.assertEqual([True], p._printer.holds)
                p._release_toolmap_hold()
                p.on_event("PrintStarted", {"origin": "local", "path": "real.gcode"})
                self.assertIsNone(p._state["prompt"])
                self.assertEqual([True, False], p._printer.holds)

    def test_stale_opted_out_ui_file_cannot_skip_a_real_print(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control.gcode").write_text("@pause\n; skip-rme-toolmapping\n")
            (root / "real.gcode").write_text("G28\nT1\n")
            p = self.plugin(root)
            p._printer.get_current_data = lambda: {"job": {"file": {
                "origin": "local", "path": "control.gcode"}}}
            self.select(p, root, "real.gcode")
            p.gcode_script_hook(p._rme_comm, "gcode", "beforePrintStarted")
            self.assertTrue(p._toolmap_preflight_decision)
            self.assertEqual([True], p._printer.holds)

    def test_simultaneous_start_callbacks_acquire_only_one_review_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.gcode").write_text("G28\nT1\n")
            p = self.plugin(root)
            entered = threading.Barrier(3)
            original = p._printer.set_job_on_hold
            def slow_hold(*args, **kwargs):
                time.sleep(0.05)
                return original(*args, **kwargs)
            p._printer.set_job_on_hold = slow_hold
            errors = []
            def start():
                try:
                    entered.wait(timeout=2)
                    p._prepare_toolmap_prompt()
                except BaseException as error:
                    errors.append(error)
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            entered.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([True], p._printer.holds)

    def test_unresolvable_connector_file_cannot_reuse_stale_opt_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "control.gcode").write_text("; skip-rme-toolmapping\n")
            (root / "real.gcode").write_text("G28\nT1\n")
            p = self.plugin(root)
            p._printer.get_current_data = lambda: {"job": {"file": {
                "origin": "local", "path": "control.gcode"}}}
            p._file_manager.path_in_storage = Mock(side_effect=RuntimeError("unavailable storage"))
            self.select(p, root, "real.gcode")
            p.gcode_script_hook(p._rme_comm, "gcode", "beforePrintStarted")
            self.assertTrue(p._toolmap_preflight_decision)
            self.assertEqual([True], p._printer.holds)


if __name__ == "__main__":
    unittest.main()
