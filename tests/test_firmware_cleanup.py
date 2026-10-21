import copy
import hashlib
import logging
import os
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from bbf_fixture import bbf
from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.file_service import FileServiceError, RmeFileService
from octoprint_rme_compatibility.firmware_cleanup import FlashReceiptStore, cleanup_matches
from octoprint_rme_compatibility.firmware_image import validate
from octoprint_rme_compatibility.protocol import parse_line
from octoprint_rme_compatibility.uploader import UploadError


class FirmwareCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FlashReceiptStore(os.path.join(self.directory.name, "receipt.json"))
        self.uuid = "cede2a2f-41a2-4748-9b12-c55c62f367ff"
        self.receipt = dict(schema=1, printer_uuid=self.uuid, model="COREONEINDX",
                            size=3917320, sha256="a" * 64, application_size=1397620,
                            application_sha256="b" * 64, previous_application_sha256="c" * 64)
        self.candidate = dict(candidate=1, armed=0, state="ready", path="FWUPD.RME",
                              size=self.receipt["size"], sha256=self.receipt["sha256"])
        self.running = dict(algorithm="app-sha256-v1", model="COREONEINDX",
                            size=self.receipt["application_size"], sha256="b" * 64)

    def plugin(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger(__name__)
        plugin._persist_and_publish = Mock()
        plugin._print_job_active = lambda: False
        plugin._flash_receipt_store = self.store
        plugin._firmware_printer_uuid = self.uuid
        plugin._state.update(connected=True, supported=True)
        plugin._state["storage"]["caps"] = dict(firmware_guarded_unstage=1, firmware_running=1)
        plugin._file_service = types.SimpleNamespace(
            firmware_status=Mock(side_effect=[self.candidate, dict(candidate=0, armed=0, state="idle")]),
            firmware_running=Mock(return_value=self.running),
            unstage_firmware=Mock(),
        )
        return plugin

    def test_missing_both_usb_markers_recovers_after_plugin_restart(self):
        self.store.save(self.receipt)
        plugin = self.plugin()
        restored = FlashReceiptStore(self.store.path)
        self.assertEqual(self.receipt, restored.load())
        plugin._flash_receipt_store = restored
        # Cached telemetry is deliberately stale: recovery needs a fresh read.
        plugin._state["running_firmware"] = dict(self.running, sha256="c" * 64)
        self.assertFalse(plugin._reconcile_firmware_stage())
        plugin._file_service.firmware_running.assert_called_once_with()
        plugin._file_service.unstage_firmware.assert_called_once_with(expected=self.receipt)
        self.assertEqual("idle", plugin._state["firmware"]["status"])
        self.assertFalse(os.path.exists(self.store.path))

    def test_merely_staged_matching_image_is_preserved_without_receipt(self):
        plugin = self.plugin()
        self.assertTrue(plugin._reconcile_firmware_stage())
        plugin._file_service.unstage_firmware.assert_not_called()
        plugin._file_service.firmware_running.assert_not_called()

    def test_identity_and_handoff_negative_matrix(self):
        mutations = [
            ("receipt", "schema", 2), ("receipt", "sha256", "invalid"),
            ("receipt", "previous_application_sha256", "b" * 64),
            ("receipt", "model", None), ("receipt", "size", 0),
            ("receipt", "application_size", True),
            ("receipt", "printer_uuid", "different-printer"),
            ("candidate", "sha256", "d" * 64), ("candidate", "size", 1),
            ("candidate", "armed", 1), ("candidate", "state", "validating"),
            ("candidate", "path", "OTHER.RME"),
            ("running", "sha256", "c" * 64), ("running", "size", 1),
            ("running", "model", "COREONE"), ("running", "algorithm", "unknown"),
        ]
        self.assertTrue(cleanup_matches(self.receipt, self.candidate, self.running, self.uuid))
        for target, key, value in mutations:
            with self.subTest(target=target, key=key):
                items = {"receipt": copy.deepcopy(self.receipt), "candidate": dict(self.candidate),
                         "running": dict(self.running)}
                items[target][key] = value
                self.assertFalse(cleanup_matches(items["receipt"], items["candidate"], items["running"], self.uuid))
        self.assertFalse(cleanup_matches(self.receipt, self.candidate, self.running, None))
        self.assertFalse(cleanup_matches(None, self.candidate, self.running, self.uuid))

    def test_changed_candidate_during_cleanup_is_not_hidden_and_retries(self):
        self.store.save(self.receipt)
        plugin = self.plugin()
        plugin._file_service.unstage_firmware.side_effect = FileServiceError("candidate_changed")
        with self.assertRaises(FileServiceError):
            plugin._reconcile_firmware_stage()
        self.assertEqual(self.receipt, self.store.get())
        plugin._file_service.firmware_status.assert_called_once_with()

    def test_print_and_unsupported_firmware_never_cleanup(self):
        self.store.save(self.receipt)
        plugin = self.plugin()
        plugin._print_job_active = lambda: True
        self.assertFalse(plugin._reconcile_firmware_stage())
        plugin._file_service.firmware_status.assert_not_called()
        plugin._print_job_active = lambda: False
        plugin._state["storage"]["caps"] = {}
        self.assertTrue(plugin._reconcile_firmware_stage())
        plugin._file_service.unstage_firmware.assert_not_called()

    def test_persistence_failure_prevents_flash_and_reconnect_arm(self):
        plugin = self.plugin()
        plugin._state["running_firmware"] = self.running
        plugin._validate_staged_firmware = Mock(return_value=self.receipt)
        plugin._arm_firmware_reconnect = Mock()
        plugin._file_service.mutate = Mock()
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(UploadError):
                plugin._flash_firmware()
        plugin._arm_firmware_reconnect.assert_not_called()
        plugin._file_service.mutate.assert_not_called()

    def test_receipt_is_durable_before_flash_is_sent(self):
        plugin = self.plugin()
        plugin._state["running_firmware"] = dict(self.running, sha256="c" * 64)
        plugin._validate_staged_firmware = Mock(return_value={
            key: self.receipt[key] for key in ("size", "sha256", "application_size", "application_sha256")})
        plugin._arm_firmware_reconnect = Mock()
        def mutate(action, path):
            self.assertEqual(("FLASH", "FWUPD.RME"), (action, path))
            self.assertEqual(self.receipt, FlashReceiptStore(self.store.path).load())
        plugin._file_service.mutate = mutate
        plugin._flash_firmware()

    def test_bbf_application_identity_uses_only_application_bytes(self):
        path = os.path.join(self.directory.name, "image.bbf")
        with open(path, "wb") as image:
            image.write(bbf(payload=b"application") + b"resources")
        identity = validate(path, "coreone")
        self.assertEqual(len(b"application"), identity["application_size"])
        self.assertEqual(hashlib.sha256(b"application").hexdigest(), identity["application_sha256"])

    def test_guarded_unstage_transport_and_invalid_guard(self):
        commands = []
        service = None
        def send(command):
            commands.append(command)
            service.handle_response(parse_line("RME_FIRMWARE_UNSTAGED candidate=0 armed=0"))
        service = RmeFileService(send, response_timeout=1)
        service.unstage_firmware(expected=self.receipt)
        self.assertEqual(["@RME FIRMWARE UNSTAGE size=3917320 sha256=" + "a" * 64], commands)
        for expected in ({}, dict(size=1, sha256="bad"), dict(size=True, sha256="a" * 64)):
            with self.assertRaises(FileServiceError):
                service.unstage_firmware(expected=expected)
        self.assertEqual(1, len(commands))

    def test_corrupt_receipt_never_becomes_cleanup_authorization(self):
        with open(self.store.path, "w") as handle:
            handle.write('{"schema":1')
        self.assertIsNone(self.store.load())
        plugin = self.plugin()
        self.assertTrue(plugin._reconcile_firmware_stage())
        plugin._file_service.unstage_firmware.assert_not_called()

    def test_disconnect_during_identity_query_never_deletes(self):
        self.store.save(self.receipt)
        plugin = self.plugin()
        def running():
            plugin._connection_generation += 1
            return self.running
        plugin._file_service.firmware_running.side_effect = running
        self.assertTrue(plugin._reconcile_firmware_stage())
        plugin._file_service.unstage_firmware.assert_not_called()

    def test_failed_confirmation_preserves_receipt_for_retry(self):
        self.store.save(self.receipt)
        plugin = self.plugin()
        plugin._file_service.firmware_status.side_effect = [self.candidate, self.candidate]
        with self.assertRaises(FileServiceError):
            plugin._reconcile_firmware_stage()
        self.assertEqual(self.receipt, self.store.get())

    def test_already_cleaned_printer_clears_receipt_without_mutation(self):
        self.store.save(self.receipt)
        plugin = self.plugin()
        plugin._file_service.firmware_status.side_effect = [dict(candidate=0, armed=0, state="idle")]
        self.assertFalse(plugin._reconcile_firmware_stage())
        self.assertIsNone(self.store.get())
        plugin._file_service.unstage_firmware.assert_not_called()

    def test_fresh_running_identity_uses_serial_exchange(self):
        commands = []
        service = None
        def send(command):
            commands.append(command)
            service.handle_response(parse_line(
                "RME_FIRMWARE_RUNNING algorithm=app-sha256-v1 model=COREONEINDX "
                "version=6.9.2-RME size=1397620 sha256=" + "b" * 64))
        service = RmeFileService(send, response_timeout=1)
        self.assertEqual("b" * 64, service.firmware_running()["sha256"])
        self.assertEqual(["@RME FIRMWARE RUNNING"], commands)

    def test_uuid_is_captured_from_actual_m115_reply(self):
        plugin = self.plugin()
        plugin._observe_pressure_advance_output = lambda line: None
        plugin._uploader = None
        plugin._file_service = None
        line = "FIRMWARE_NAME:Prusa-Firmware-Buddy 6.9.2-RME MACHINE_TYPE:Prusa-COREONEINDX UUID:" + self.uuid
        self.assertEqual(line, plugin.gcode_received_hook(None, line))
        self.assertEqual(self.uuid, plugin._firmware_printer_uuid)
