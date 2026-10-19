import hashlib
import os
import tempfile
import unittest
from unittest.mock import Mock
from bbf_fixture import bbf
from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.firmware_image import validate
from octoprint_rme_compatibility.uploader import UploadError


class FirmwareImageTests(unittest.TestCase):
    def test_embedded_identity_and_support_not_filename(self):
        with tempfile.NamedTemporaryFile(suffix="_renamed.bbf") as image:
            cases = [
                ((6, 9, 2), (7, 10, 0), "coreone_indx", True),
                ((6, 10, 3), (7, 10, 0), "coreone_indx", False),
                ((6, 10, 3), (3, 1, 0), "xl", True),
                ((6, 10, 3), (3, 1, 0), "coreone_indx", False),
                ((6, 8, 1), (7, 1, 0), "coreone", True),
                ((6, 10, 3), (7, 1, 0), "coreone", False),
                ((6, 5, 7), (1, 3, 5), "mk3.5", True),
                ((6, 99, 0), (7, 10, 0), "coreone_indx", False),
            ]
            for version, model, expected, valid in cases:
                image.seek(0)
                image.truncate()
                image.write(bbf(version, model))
                image.flush()
                if valid:
                    self.assertEqual(validate(image.name, expected)["variant"], expected)
                else:
                    with self.assertRaises(UploadError):
                        validate(image.name, expected)

    def test_corrupt_or_unknown_machine_fails_closed(self):
        with tempfile.NamedTemporaryFile() as image:
            image.write(bbf()[:-1] + b"X")
            image.flush()
            for expected in ("coreone", None):
                with self.assertRaises(UploadError):
                    validate(image.name, expected)

    def test_flash_revalidates_file_checksum_and_connected_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "anything.bbf")
            with open(path, "wb") as image:
                image.write(bbf())
            plugin = RmeCompatibilityPlugin()
            plugin._firmware_directory = directory
            plugin._settings = Mock()
            plugin._settings.get.return_value = ""
            plugin._state["running_firmware"]["model"] = "COREONE"
            plugin._state["firmware"].update(status="ready", filename="anything.bbf",
                sha256=hashlib.sha256(bbf()).hexdigest())
            plugin._validate_staged_firmware()
            plugin._state["running_firmware"]["model"] = "COREONEINDX"
            with self.assertRaises(UploadError):
                plugin._validate_staged_firmware()
            plugin._state["running_firmware"]["model"] = "COREONE"
            plugin._state["firmware"]["sha256"] = "a" * 64
            with self.assertRaises(UploadError):
                plugin._validate_staged_firmware()
