import logging
import threading
import types
import unittest
from unittest.mock import Mock

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.completion_snapshot import CompletionSnapshot


class CompletionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.bridge = CompletionSnapshot()
        self.image = Mock(return_value=b"earlier frame")
        self.owner = types.SimpleNamespace(image=self.image)
        values = {"camera_snapshot_url": "http://camera/snapshot", "webcam_flipH": True,
                  "webcam_flipV": False, "webcam_rotate90": True}
        self.settings = types.SimpleNamespace(get=lambda keys: values.get(keys[0]))
        self.manager = types.SimpleNamespace(plugins={"octopod": types.SimpleNamespace(
            enabled=True, implementation=types.SimpleNamespace(
                _job_notifications=self.owner, _settings=self.settings))})
        self.printer = types.SimpleNamespace(get_state_id=lambda: "FINISHING")
        self.logger = logging.getLogger("snapshot-test")

    def capture(self, **kwargs):
        return self.bridge.capture(self.manager, self.printer, self.logger, **kwargs)

    def test_completion_reuses_earlier_frame_without_another_camera_fetch(self):
        self.assertTrue(self.capture())
        self.image.assert_called_once_with(False, "http://camera/snapshot", True, False, True)
        self.image.return_value = b"bed lowered"
        self.assertEqual(self.owner.image(True, "url", False, False, False), b"earlier frame")
        self.assertEqual(self.image.call_count, 1)

    def test_other_print_states_use_live_image_and_reset_restores_owner(self):
        self.capture()
        self.image.return_value = b"live"
        self.printer.get_state_id = lambda: "PRINTING"
        self.assertEqual(self.owner.image(False, "url", False, False, False), b"live")
        self.bridge.reset()
        self.assertIs(self.owner.image, self.image)
        self.assertIsNone(self.bridge._frame)

    def test_timeout_late_frame_is_discarded_and_requests_do_not_accumulate(self):
        release = threading.Event()
        self.image.side_effect = lambda *args: (release.wait(1), b"too late")[1]
        try:
            self.assertFalse(self.capture(timeout=0.001))
            self.assertFalse(self.capture(timeout=0.001))
            self.assertIs(self.owner.image, self.image)
            self.assertEqual(self.image.call_count, 1)
        finally:
            release.set()
            self.bridge._worker.join(1)
        self.assertIsNone(self.bridge._frame)

    def test_cancel_during_capture_discards_frame(self):
        def fetch(*args):
            self.bridge.reset()
            return b"canceled"
        self.image.side_effect = fetch
        self.assertFalse(self.capture())
        self.assertIs(self.owner.image, self.image)

    def test_missing_disabled_and_failed_camera_fall_back(self):
        self.manager.plugins["octopod"].enabled = False
        self.assertFalse(self.capture())
        self.manager.plugins["octopod"].enabled = True
        self.image.side_effect = RuntimeError("camera offline")
        self.assertFalse(self.capture())
        self.assertIs(self.owner.image, self.image)

    def test_expired_frame_is_not_reused(self):
        self.capture()
        self.bridge._expires = 0
        self.image.return_value = b"live"
        self.assertEqual(self.owner.image(False, "url", False, False, False), b"live")

    def test_host_marker_is_synchronous_and_never_forwarded_to_firmware(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["supported"] = True
        plugin._printer = types.SimpleNamespace(is_printing=lambda: True)
        plugin._plugin_manager = self.manager
        plugin._logger = self.logger
        calls = []
        plugin._completion_snapshot.capture = lambda *a: calls.append("captured")
        comm = types.SimpleNamespace(_do_send=lambda *a: self.fail("host marker forwarded"))
        plugin.atcommand_sending_hook(comm, "sending", "RME", "SNAPSHOT", tags={"source:file"})
        calls.append("next Z move")
        self.assertEqual(calls, ["captured", "next Z move"])
        plugin.atcommand_sending_hook(comm, "sending", "RME", "SNAPSHOT", tags={"source:terminal"})
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
