import logging
import types
import unittest

from test_toolmap_gate import RmeCompatibilityPlugin


class RetryPrintTests(unittest.TestCase):
    def setUp(self):
        self.plugin = RmeCompatibilityPlugin()
        self.plugin._state.update(connected=True, supported=True)
        self.file = dict(origin="local", path="part.gcode", size=100, date=123)
        self.starts = []
        self.notices = []
        self.commands = []
        self.tasks = []
        self.plugin._printer = types.SimpleNamespace(
            get_current_data=lambda: {"job": {"file": self.file}},
            is_printing=lambda: False, is_paused=lambda: False,
            get_state_id=lambda: "OPERATIONAL", is_operational=lambda: True,
            start_print=lambda: self.starts.append(True),
        )
        self.plugin._logger = logging.getLogger(__name__)
        self.plugin._retry_notice = lambda message, error: self.notices.append((message, error))
        self.plugin._send_command = self.commands.append
        self.plugin._defer = lambda callback, *args: self.tasks.append((callback, args))
        self.plugin._retry_file = self.plugin._selected_retry_file()
        self.plugin._retry_ready = True

    def request(self):
        self.plugin.action_command_hook(None, "// action:rme_retry", "rme_retry")

    def drain(self):
        while self.tasks:
            callback, args = self.tasks.pop(0)
            callback(*args)

    def test_confirmed_request_restarts_once_without_resume(self):
        self.request()
        self.request()
        self.assertEqual(len(self.tasks), 1)
        self.drain()
        self.assertEqual(self.starts, [True])
        self.request()
        self.drain()
        self.assertEqual(self.starts, [True])

    def test_changed_file_or_replaced_file_never_restarts(self):
        for key, value in (("path", "other.gcode"), ("date", 124), ("size", 101), ("origin", "sdcard")):
            with self.subTest(key=key):
                self.setUp()
                self.request()
                self.file[key] = value
                self.drain()
                self.assertEqual(self.starts, [])
                self.assertTrue(self.notices[-1][1])

    def test_reconnect_invalidates_pending_request(self):
        self.request()
        self.plugin._connection_generation += 1
        self.drain()
        self.assertEqual(self.starts, [])

    def test_busy_locked_and_transfer_guards(self):
        for reason in ("PRINTING", "PAUSED", "CANCELLING", "STARTING", "transfer", "locked", "disconnected", "not_ready"):
            with self.subTest(reason=reason):
                self.setUp()
                if reason == "transfer":
                    self.plugin._file_service = types.SimpleNamespace(busy=True)
                elif reason == "locked":
                    self.plugin._state["lock"]["locked"] = 1
                elif reason == "disconnected":
                    self.plugin._state["connected"] = False
                elif reason == "not_ready":
                    self.plugin._retry_ready = False
                else:
                    self.plugin._printer.get_state_id = lambda: reason
                self.request()
                self.drain()
                self.assertEqual(self.starts, [])
                self.assertTrue(self.notices[-1][1])

    def test_other_actions_and_unknown_printer_do_not_restart(self):
        self.plugin.action_command_hook(None, "", "resume")
        self.plugin._state["supported"] = False
        self.request()
        self.assertEqual(self.tasks, [])

    def test_sd_file_not_eligible(self):
        self.file["origin"] = "sdcard"
        self.assertIsNone(self.plugin._selected_retry_file())

    def test_lifecycle_records_completed_and_canceled_jobs_only(self):
        for event in ("PrintDone", "PrintCancelled", "PrintFailed"):
            with self.subTest(event=event):
                self.setUp()
                self.plugin._handle_print_started = lambda payload: None
                self.plugin._release_toolmap_hold = lambda: None
                self.plugin.on_event("PrintStarted", dict(self.file))
                self.assertFalse(self.plugin._retry_ready)
                self.plugin.on_event(event, dict(self.file))
                self.assertTrue(self.plugin._retry_ready)
                self.assertEqual(self.plugin._retry_file, ("local", "part.gcode", 100, 123))

    def test_unrelated_completion_cannot_make_a_retry_eligible(self):
        self.plugin._release_toolmap_hold = lambda: None
        self.plugin._retry_job_path = ("local", "other.gcode")
        self.plugin.on_event("PrintDone", {})
        self.assertFalse(self.plugin._retry_ready)
