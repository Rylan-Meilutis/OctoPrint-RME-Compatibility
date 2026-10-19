import logging
import os
import tempfile
import unittest

from test_toolmap_gate import RmeCompatibilityPlugin


class FirmwareBulkDeleteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plugin = RmeCompatibilityPlugin()
        self.plugin._firmware_directory = self.directory.name
        self.plugin._require_print_idle = lambda _: None
        self.plugin._logger = logging.getLogger(__name__)
        for name in ("one.bbf", "two.BBF", "keep.txt", "new.bbf"):
            with open(os.path.join(self.directory.name, name), "wb") as file:
                file.write(b"fixture")

    def test_only_confirmed_bbf_snapshot_is_removed(self):
        self.plugin._delete_firmware_copies(["one.bbf", "two.BBF", "one.bbf"])
        self.assertEqual(sorted(os.listdir(self.directory.name)), ["keep.txt", "new.bbf"])

    def test_validate_all_before_deleting(self):
        for invalid in ("../outside.bbf", "keep.txt", "missing.bbf"):
            with self.assertRaises(ValueError):
                self.plugin._delete_firmware_copies(["one.bbf", invalid])
            self.assertTrue(os.path.isfile(os.path.join(self.directory.name, "one.bbf")))

    def test_bad_requests_rejected(self):
        for invalid in ([], "one.bbf", None, [12]):
            with self.assertRaises(ValueError):
                self.plugin._delete_firmware_copies(invalid)

    def test_symlink_and_directory_rejected(self):
        os.symlink("one.bbf", os.path.join(self.directory.name, "link.bbf"))
        os.mkdir(os.path.join(self.directory.name, "folder.bbf"))
        for name in ("link.bbf", "folder.bbf"):
            with self.assertRaises(ValueError):
                self.plugin._delete_firmware_copies([name])

    def test_release_operation_and_print_block_deletion(self):
        self.plugin._release_lock.acquire()
        with self.assertRaisesRegex(Exception, "release operation"):
            self.plugin._delete_firmware_copies(["one.bbf"])
        self.plugin._release_lock.release()
        def printing(_):
            raise ValueError("Printing")
        self.plugin._require_print_idle = printing
        with self.assertRaisesRegex(ValueError, "Printing"):
            self.plugin._delete_firmware_copies(["one.bbf"])
        self.assertTrue(os.path.isfile(os.path.join(self.directory.name, "one.bbf")))

    def test_queued_transfer_and_reboot_block_deletion(self):
        for status in ("queued", "uploading", "verifying", "flash_queued", "restarting"):
            self.plugin._state["firmware"]["status"] = status
            with self.assertRaisesRegex(Exception, "transfer"):
                self.plugin._delete_firmware_copies(["one.bbf"])
        self.assertTrue(os.path.isfile(os.path.join(self.directory.name, "one.bbf")))
