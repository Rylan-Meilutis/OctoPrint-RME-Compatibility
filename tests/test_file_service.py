import base64
import os
import tempfile
import unittest

from octoprint_rme_compatibility.file_service import (
    FileServiceError,
    RmeFileService,
    normalize_remote_path,
)
from octoprint_rme_compatibility.protocol import parse_line


class FileServiceTests(unittest.TestCase):
    def test_lists_and_downloads_space_containing_binary_file(self):
        service = None

        def send(command):
            if command == "@RME FILE LIST path=/":
                service.handle_response(parse_line(
                    "RME_FILE_ENTRY name=My print.bgcode type=file size=5"
                ))
                service.handle_response(parse_line("RME_FILE_LIST_END"))
            elif "offset=0" in command:
                service.handle_response(parse_line(
                    "RME_FILE_DATA path=My print.bgcode offset=0 length=3 eof=0 data=YWJj"
                ))
            elif "offset=3" in command:
                service.handle_response(parse_line(
                    "RME_FILE_DATA path=My print.bgcode offset=3 length=2 eof=1 data=AP8="
                ))

        service = RmeFileService(send, response_timeout=1)
        entries = service.list_directory("/")
        self.assertEqual(entries[0]["name"], "My print.bgcode")
        self.assertEqual(b"abc\x00\xff", b"".join(service.iter_file("My print.bgcode")))

    def test_verified_upload_advances_only_from_firmware_offsets(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if "WRITE_BEGIN" in command:
                service.handle_response(parse_line("RME_FILE_WRITE_READY offset=0 chunk=48"))
            elif "WRITE_CHUNK" in command:
                encoded = command.split("data=", 1)[1]
                length = len(base64.b64decode(encoded))
                offset = int(command.split("offset=", 1)[1].split(" ", 1)[0]) + length
                service.handle_response(parse_line("RME_FILE_WRITE_OFFSET offset=%d" % offset))
            elif "WRITE_END" in command:
                service.handle_response(parse_line("RME_FILE_WRITE_COMPLETE path=jobs/test file.gcode"))

        service = RmeFileService(send, response_timeout=1)
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(bytes(range(100)))
            source_path = source.name
        try:
            service.write_file(source_path, "jobs/test file.gcode")
        finally:
            os.unlink(source_path)
        self.assertIn("path=jobs/test%20file.gcode", commands[0])
        self.assertEqual(3, len([command for command in commands if "WRITE_CHUNK" in command]))

    def test_paths_cannot_escape_usb_root(self):
        self.assertEqual("jobs/My%20print.gcode", normalize_remote_path("/jobs/My print.gcode"))
        with self.assertRaises(FileServiceError):
            normalize_remote_path("jobs/../secret")
