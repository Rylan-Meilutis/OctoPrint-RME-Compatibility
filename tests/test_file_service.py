import base64
import os
import struct
import tempfile
import unittest
import zlib

from octoprint_rme_compatibility.file_service import (
    FileServiceError,
    RmeFileService,
    normalize_remote_path,
)
from octoprint_rme_compatibility.protocol import parse_line


class FileServiceTests(unittest.TestCase):
    def test_binary_upload_uses_crc_frames_and_recovers_from_nack(self):
        service = None
        source_data = bytes(range(256)) * 36
        received = bytearray()
        ended = []
        window = []
        rejected_once = [False]

        def send(command):
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 binary=1 "
                    "binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        def send_binary(frame):
            offset, length, checksum = struct.unpack("<IHI", frame[:10])
            payload = frame[10:]
            self.assertEqual(length, len(payload))
            self.assertEqual(checksum, zlib.crc32(payload) & 0xFFFFFFFF)
            if not payload:
                self.assertEqual(len(source_data), offset)
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=FWUPD.BBF"
                ))
                return
            window.append((offset, payload))
            final = offset + len(payload) == len(source_data)
            if len(window) == 8 or final:
                if not rejected_once[0]:
                    rejected_once[0] = True
                    service.handle_response(parse_line("RME_FILE_BINARY_NACK offset=0"))
                else:
                    for frame_offset, frame_payload in window:
                        self.assertEqual(len(received), frame_offset)
                        received.extend(frame_payload)
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_ACK offset=%d" % len(received)
                    ))
                window[:] = []

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "1" * 32,
            end_binary=lambda: ended.append(True),
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(source_data)
            source_path = source.name
        try:
            service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)
        self.assertTrue(rejected_once[0])
        self.assertEqual(source_data, bytes(received))
        self.assertEqual([True], ended)

    def test_binary_transport_failure_aborts_and_falls_back_to_bulk(self):
        service = None
        commands = []
        ended = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 bulk_chunk=320 "
                    "bulk_window=4 binary=1 binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_READY offset=0 chunk=320 window=4"
                ))
            elif "WRITE_BULK_CHUNK" in command:
                payload = base64.b64decode(command.split("data=", 1)[1])
                offset = int(command.split("offset=", 1)[1].split(" ", 1)[0])
                service.handle_response(parse_line(
                    "RME_FILE_BULK_ACK offset=%d" % (offset + len(payload))
                ))
            elif "WRITE_BULK_END" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_COMPLETE path=FWUPD.BBF"
                ))

        service = RmeFileService(
            send, response_timeout=1,
            send_binary=lambda frame: (_ for _ in ()).throw(IOError("raw failed")),
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "2" * 32,
            end_binary=lambda: ended.append(True),
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"signed-firmware")
            source_path = source.name
        try:
            service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)
        self.assertTrue(ended)
        self.assertTrue(any("WRITE_BULK_BEGIN" in command for command in commands))
        self.assertTrue(any("WRITE_BULK_END" in command for command in commands))

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

    def test_download_stages_atomically_and_reports_progress(self):
        service = None

        def send(command):
            if "FILE STAT" in command:
                service.handle_response(parse_line(
                    "RME_FILE_STAT path=dump.bin type=file size=5 mtime=1"
                ))
            elif "offset=0" in command:
                service.handle_response(parse_line(
                    "RME_FILE_DATA path=dump.bin offset=0 length=5 eof=1 data=YWJjAP8="
                ))

        service = RmeFileService(send, response_timeout=1)
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "dump.bin")
            metadata = service.download_file(
                "dump.bin", destination,
                progress=lambda offset, size: progress.append((offset, size)),
            )
            with open(destination, "rb") as downloaded:
                self.assertEqual(b"abc\x00\xff", downloaded.read())
            self.assertFalse(os.path.exists(destination + ".part"))
        self.assertEqual(5, metadata["size"])
        self.assertEqual((5, 5), progress[-1])

    def test_verified_upload_advances_only_from_firmware_offsets(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line("RME_FILE_CAPS root=/usb chunk=48 bulk=0"))
            elif "WRITE_BEGIN" in command:
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
        self.assertTrue(any("path=jobs/test%20file.gcode" in command for command in commands))
        self.assertEqual(3, len([command for command in commands if "WRITE_CHUNK" in command]))

    def test_bulk_upload_pipelines_four_negotiated_chunks_per_ack(self):
        service = None
        commands = []
        pending = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb chunk=48 bulk=1 bulk_chunk=384 bulk_window=4 binary=1"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_READY offset=0 chunk=384 window=4"
                ))
            elif "WRITE_BULK_CHUNK" in command:
                encoded = command.split("data=", 1)[1]
                start = int(command.split("offset=", 1)[1].split(" ", 1)[0])
                pending.append(start + len(base64.b64decode(encoded)))
                if len(pending) == 4 or pending[-1] == 1600:
                    service.handle_response(parse_line(
                        "RME_FILE_BULK_ACK offset=%d" % pending[-1]
                    ))
                    pending[:] = []
            elif "WRITE_BULK_END" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_COMPLETE path=jobs/test.bgcode"
                ))

        service = RmeFileService(send, response_timeout=1)
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(bytes(range(256)) * 6 + bytes(range(64)))
            source_path = source.name
        try:
            service.write_file(source_path, "jobs/test.bgcode")
        finally:
            os.unlink(source_path)
        chunks = [command for command in commands if "WRITE_BULK_CHUNK" in command]
        self.assertEqual(5, len(chunks))
        self.assertLessEqual(max(
            len(base64.b64decode(command.split("data=", 1)[1]))
            for command in chunks
        ), 320)
        self.assertLess(max(map(len, chunks)), 512)
        self.assertTrue(any("WRITE_BULK_END" in command for command in commands))

    def test_paths_cannot_escape_usb_root(self):
        self.assertEqual("jobs/My%20print.gcode", normalize_remote_path("/jobs/My print.gcode"))
        with self.assertRaises(FileServiceError):
            normalize_remote_path("jobs/../secret")

    def test_queued_upload_can_cancel_before_first_command(self):
        commands = []
        service = RmeFileService(commands.append, response_timeout=1)
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"firmware")
            source_path = source.name
        try:
            with self.assertRaisesRegex(FileServiceError, "before sending"):
                service.write_file(
                    source_path, "FWUPD.BBF", cancel_check=lambda: True
                )
        finally:
            os.unlink(source_path)
        self.assertEqual([], commands)
