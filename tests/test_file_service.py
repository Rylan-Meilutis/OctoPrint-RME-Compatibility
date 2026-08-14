import base64
import os
import struct
import tempfile
import threading
import unittest
import zlib
from unittest import mock

from octoprint_rme_compatibility.file_service import (
    FileServiceError,
    RmeFileService,
    normalize_remote_path,
)
from octoprint_rme_compatibility.protocol import parse_line


class FileServiceTests(unittest.TestCase):
    def test_current_binary_timeout_capability_enables_fast_transport(self):
        service = None
        commands = []
        raw_frames = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 binary=1 "
                    "binary_chunk=1024 binary_window=8 binary_timeout_ms=10000"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        def send_binary(frame):
            raw_frames.append(frame)
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            if length:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ACK offset=%d" % (offset + length)
                ))
            else:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=fast.bin"
                ))

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "d" * 32,
            end_binary=lambda: None,
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"current bounded binary transport")
            source_path = source.name
        try:
            service.write_file(source_path, "fast.bin")
        finally:
            os.unlink(source_path)

        self.assertTrue(raw_frames)
        self.assertTrue(any("WRITE_BINARY_BEGIN" in item for item in commands))
        self.assertFalse(any("WRITE_BULK_BEGIN" in item for item in commands))

    def test_authoritative_firmware_query_and_unstage_are_serialized(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command == "@RME FIRMWARE QUERY":
                service.handle_response(parse_line(
                    "RME_FIRMWARE candidate=1 armed=0 state=ready "
                    "path=FWUPD.RME size=1234 sha256=" + "b" * 64
                ))
            elif command == "@RME FIRMWARE UNSTAGE":
                service.handle_response(parse_line(
                    "RME_FIRMWARE_UNSTAGED candidate=0 armed=0"
                ))

        service = RmeFileService(send, response_timeout=1)
        status = service.firmware_status()
        self.assertEqual(1234, status["size"])
        self.assertEqual("ready", status["state"])
        result = service.unstage_firmware()
        self.assertEqual(0, result["candidate"])
        self.assertEqual([
            "@RME FIRMWARE QUERY", "@RME FIRMWARE UNSTAGE",
        ], commands)

    def test_current_binary_capability_uses_fast_transport_without_old_resync_flag(self):
        service = None
        commands = []
        raw_frames = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 bulk_chunk=384 "
                    "bulk_window=4 binary=1 binary_chunk=1024 binary_window=8 "
                    "binary_control=1 resumable_abort=1 durable_resume=1"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        def send_binary(frame):
            raw_frames.append(frame)
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            if length:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ACK offset=%d" % (offset + length)
                ))
            else:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=FWUPD.BBF"
                ))

        service = RmeFileService(
            send, response_timeout=1,
            send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "f" * 32,
            end_binary=lambda: None,
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(bytes(range(256)) * 4)
            source_path = source.name
        try:
            service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)

        self.assertTrue(raw_frames)
        self.assertTrue(any("WRITE_BINARY_BEGIN" in item for item in commands))
        self.assertFalse(any("WRITE_BULK_BEGIN" in item for item in commands))

    def test_transfer_latch_waits_without_arming_raw_writer(self):
        service = None
        commands = []
        armed = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 binary=1 binary_resync=1 "
                    "binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                attempts = sum("WRITE_BINARY_BEGIN" in item for item in commands)
                if attempts < 3:
                    service.handle_response(parse_line(
                        "echo:RME_ERROR workflow=file code=transfer_busy"
                    ))
                else:
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                        "header=10 endian=little crc=crc32"
                    ))

        def begin_binary():
            armed.append(len(commands))
            return "@RME FILE RAW_SESSION token=" + "a" * 32

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            if length:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ACK offset=%d" % (offset + length)
                ))
            else:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=test.bin"
                ))

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=begin_binary, end_binary=lambda: None,
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"latched transfer")
            source_path = source.name
        try:
            with mock.patch(
                "octoprint_rme_compatibility.file_service.TRANSFER_LATCH_RETRY_SECONDS",
                0.001,
            ):
                service.write_file(source_path, "test.bin")
        finally:
            os.unlink(source_path)

        self.assertEqual(3, sum("WRITE_BINARY_BEGIN" in item for item in commands))
        self.assertEqual(1, len(armed))
        self.assertGreaterEqual(armed[0], 4)

    def test_repeated_binary_nack_retries_negotiated_raw_chunk(self):
        service = None
        # Current firmware snapshots and bounds its parser, so retransmission
        # keeps the negotiated 1024-byte frame size.
        source_data = bytes(range(256)) * 512
        committed = bytearray()
        window = []
        nacks = [0]
        payload_sizes = []

        def send(command):
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 binary=1 binary_resync=1 "
                    "binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            payload = frame[10:]
            if not payload:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=FWUPD.BBF"
                ))
                return
            payload_sizes.append(length)
            window.append((offset, payload))
            if len(window) == 8 or offset + length == len(source_data):
                if nacks[0] < 2:
                    nacks[0] += 1
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_NACK offset=%d" % len(committed)
                    ))
                else:
                    for frame_offset, frame_payload in window:
                        self.assertEqual(len(committed), frame_offset)
                        committed.extend(frame_payload)
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_ACK offset=%d" % len(committed)
                    ))
                window[:] = []

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "0" * 32,
            end_binary=lambda: None,
        )
        service._wait_for_binary_quiet = lambda: None
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(source_data)
            source_path = source.name
        try:
            service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)

        self.assertEqual(source_data, bytes(committed))
        self.assertTrue(payload_sizes)
        self.assertEqual({1024}, set(payload_sizes))

    def test_reasoned_crc_nack_restarts_without_throttling(self):
        service = None
        source_data = bytes(range(256)) * 96
        committed = bytearray()
        window = []
        rejected = [False]
        payload_sizes = []

        def send(command):
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 binary=1 binary_resync=1 "
                    "binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            payload = frame[10:]
            if not payload:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=FWUPD.BBF"
                ))
                return
            payload_sizes.append(length)
            window.append((offset, payload))
            if len(window) == 8 or offset + length == len(source_data):
                if not rejected[0]:
                    rejected[0] = True
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_NACK offset=0 reason=crc_mismatch"
                    ))
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_NACK offset=0 reason=offset_mismatch"
                    ))
                else:
                    for frame_offset, frame_payload in window:
                        self.assertEqual(len(committed), frame_offset)
                        committed.extend(frame_payload)
                    service.handle_response(parse_line(
                        "RME_FILE_BINARY_ACK offset=%d" % len(committed)
                    ))
                window[:] = []

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "9" * 32,
            end_binary=lambda: None,
        )
        service._wait_for_binary_quiet = lambda: None
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(source_data)
            source_path = source.name
        try:
            service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)

        self.assertEqual(source_data, bytes(committed))
        self.assertEqual(1024, payload_sizes[0])
        self.assertEqual({1024}, set(payload_sizes))

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
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 binary=1 binary_resync=1 "
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
                    # Firmware also rejects the later frames that were already
                    # in the failed eight-frame window. They must be drained,
                    # not counted as independent retry failures.
                    threading.Timer(
                        0.02,
                        lambda: service.handle_response(parse_line(
                            "RME_FILE_BINARY_NACK offset=0"
                        )),
                    ).start()
                    threading.Timer(
                        0.04,
                        lambda: service.handle_response(parse_line(
                            "RME_FILE_BINARY_NACK offset=0"
                        )),
                    ).start()
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

    def test_binary_upload_ignores_delayed_ack_from_preceding_window(self):
        service = None
        source_data = bytes(range(256)) * 64
        received = bytearray()
        window = []
        window_number = [0]

        def send(command):
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 binary=1 "
                    "binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            payload = frame[10:]
            if not payload:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_COMPLETE path=delayed.bin"
                ))
                return
            self.assertEqual(len(received) + sum(len(item[1]) for item in window), offset)
            window.append((offset, payload))
            if len(window) != 8:
                return
            for _, item in window:
                received.extend(item)
            window[:] = []
            window_number[0] += 1
            committed = len(received)
            if window_number[0] == 2:
                # Simulate the previous window's cumulative ACK arriving
                # while the second window exchange is active.
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ACK offset=8192"
                ))
                threading.Timer(
                    0.02,
                    lambda: service.handle_response(parse_line(
                        "RME_FILE_BINARY_ACK offset=%d" % committed
                    )),
                ).start()
            else:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ACK offset=%d" % committed
                ))

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "8" * 32,
            end_binary=lambda: None,
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(source_data)
            source_path = source.name
        try:
            service.write_file(source_path, "delayed.bin")
        finally:
            os.unlink(source_path)

        self.assertEqual(source_data, bytes(received))

    def test_binary_transport_failure_aborts_and_falls_back_to_bulk(self):
        service = None
        commands = []
        ended = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 bulk_chunk=320 "
                    "bulk_window=4 binary=1 binary_resync=1 binary_chunk=1024 binary_window=8"
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

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            if offset == 0xFFFFFFFF and length == 0:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ABORTED offset=0 resumable=1"
                ))
                return
            raise IOError("raw failed")

        service = RmeFileService(
            send, response_timeout=1,
            send_binary=send_binary,
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
        self.assertEqual(1, service._capabilities["binary"])
        bulk_index = next(
            index for index, command in enumerate(commands)
            if "WRITE_BULK_BEGIN" in command
        )
        self.assertNotIn("@RME FILE ABORT", commands)
        self.assertTrue(any("WRITE_BULK_BEGIN" in command for command in commands))
        self.assertTrue(any("WRITE_BULK_END" in command for command in commands))

    def test_bulk_decode_failure_resumes_with_legacy_text_transport(self):
        service = None
        commands = []
        received = bytearray()
        bulk_failed = [False]

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 bulk_chunk=384 "
                    "bulk_window=4 binary=1 binary_chunk=1024 binary_window=8 "
                    "durable_resume=1"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_READY offset=0 chunk=384 window=4 resumed=1"
                ))
            elif "WRITE_BULK_CHUNK" in command and not bulk_failed[0]:
                bulk_failed[0] = True
                service.handle_response(parse_line(
                    "echo:RME_ERROR workflow=file code=decode_failed "
                    "offset=0 resumable=1"
                ))
            elif "WRITE_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_WRITE_READY offset=0 chunk=48 resumed=1"
                ))
            elif "WRITE_CHUNK" in command and "WRITE_BULK_CHUNK" not in command:
                offset = int(command.split("offset=", 1)[1].split(" ", 1)[0])
                payload = base64.b64decode(command.split("data=", 1)[1])
                self.assertEqual(len(received), offset)
                received.extend(payload)
                service.handle_response(parse_line(
                    "RME_FILE_WRITE_OFFSET offset=%d" % len(received)
                ))
            elif "WRITE_END" in command and "WRITE_BULK_END" not in command:
                service.handle_response(parse_line(
                    "RME_FILE_WRITE_COMPLETE path=FWUPD.BBF"
                ))

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            if offset == 0xFFFFFFFF and length == 0:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_ABORTED offset=0 resumable=1"
                ))
                return
            raise IOError("simulated raw writer failure")

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "f" * 32,
            end_binary=lambda: None,
        )
        content = bytes(range(137))
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(content)
            source_path = source.name
        try:
            service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)

        self.assertTrue(bulk_failed[0])
        self.assertEqual(content, bytes(received))
        self.assertTrue(any("WRITE_BEGIN" in item for item in commands))
        self.assertTrue(any("WRITE_END" in item for item in commands))
        self.assertFalse(any("WRITE_BULK_END" in item for item in commands))

    def test_legacy_fallback_resumes_at_ready_offset(self):
        service = None
        commands = []
        source_data = bytes(range(100))

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=0 binary=0 durable_resume=1"
                ))
            elif "WRITE_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_WRITE_READY offset=48 chunk=48 resumed=1"
                ))
            elif "WRITE_CHUNK" in command:
                payload = base64.b64decode(command.split("data=", 1)[1])
                offset = int(command.split("offset=", 1)[1].split(" ", 1)[0])
                service.handle_response(parse_line(
                    "RME_FILE_WRITE_OFFSET offset=%d" % (offset + len(payload))
                ))
            elif "WRITE_END" in command:
                service.handle_response(parse_line(
                    "RME_FILE_WRITE_COMPLETE path=resume.bin"
                ))

        service = RmeFileService(send, response_timeout=1)
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(source_data)
            source_path = source.name
        try:
            service.write_file(source_path, "resume.bin")
        finally:
            os.unlink(source_path)

        chunks = [item for item in commands if "WRITE_CHUNK" in item]
        self.assertEqual(2, len(chunks))
        self.assertIn("offset=48", chunks[0])

    def test_unconfirmed_binary_abort_never_attempts_ascii_fallback(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 binary=1 binary_resync=1 "
                    "binary_chunk=1024 binary_window=8"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32"
                ))

        service = RmeFileService(
            send, response_timeout=1,
            send_binary=lambda frame: (_ for _ in ()).throw(IOError("raw failed")),
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "3" * 32,
            end_binary=lambda: None,
        )
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"signed-firmware")
            source_path = source.name
        try:
            with self.assertRaisesRegex(FileServiceError, "power-cycle or reconnect"):
                service.write_file(source_path, "FWUPD.BBF")
        finally:
            os.unlink(source_path)

        self.assertFalse(any("WRITE_BULK_BEGIN" in command for command in commands))
        self.assertNotIn("@RME FILE ABORT", commands)
        self.assertTrue(service._binary_mode_uncertain)

    def test_binary_inactivity_suspension_confirms_line_mode_recovery(self):
        ended = []
        service = None

        def send_binary(frame):
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            self.assertEqual(0xFFFFFFFF, offset)
            self.assertEqual(0, length)
            service.handle_response(parse_line(
                "RME_FILE_BINARY_SUSPENDED offset=8192 resumable=1 "
                "reason=inactivity_timeout"
            ))

        service = RmeFileService(
            lambda command: None,
            response_timeout=1,
            send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "a" * 32,
            end_binary=lambda: ended.append(True),
        )
        service._capabilities = {"binary_timeout_ms": 1}
        service._binary_active = True
        service._binary_mode_uncertain = True

        service._abort_binary_transport()

        self.assertEqual([True], ended)
        self.assertFalse(service.binary_mode_uncertain)

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
        self.assertEqual(384, max(
            len(base64.b64decode(command.split("data=", 1)[1]))
            for command in chunks
        ))
        self.assertLess(max(map(len, chunks)), 600)
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

    def test_manifest_is_saved_before_begin_and_cleared_after_completion(self):
        service = None
        events = []

        def send(command):
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 bulk_chunk=384 bulk_window=4"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                self.assertEqual("manifest:bulk", events[-1])
                service.handle_response(parse_line(
                    "RME_FILE_BULK_READY offset=0 chunk=384 window=4 resumed=0"
                ))
            elif "WRITE_BULK_CHUNK" in command:
                payload = base64.b64decode(command.split("data=", 1)[1])
                offset = int(command.split("offset=", 1)[1].split(" ", 1)[0])
                service.handle_response(parse_line(
                    "RME_FILE_BULK_ACK offset=%d" % (offset + len(payload))
                ))
            elif "WRITE_BULK_END" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_COMPLETE path=jobs/restartable.bgcode"
                ))

        service = RmeFileService(send, response_timeout=1)
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"restartable")
            source_path = source.name
        try:
            service.write_file(
                source_path, "jobs/restartable.bgcode",
                manifest_update=lambda transport, size, digest: events.append(
                    "manifest:" + transport
                ),
                manifest_complete=lambda: events.append("complete"),
            )
        finally:
            os.unlink(source_path)
        self.assertEqual(["manifest:bulk", "complete"], events)

    def test_line_failure_preserves_partial_without_implicit_abort(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 bulk_chunk=384 bulk_window=4"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_READY offset=0 chunk=384 window=4 resumed=0"
                ))
            elif "WRITE_BULK_CHUNK" in command:
                service.handle_response(parse_line(
                    "echo:RME_ERROR workflow=file code=disk_write_failed offset=0 resumable=1"
                ))

        service = RmeFileService(send, response_timeout=1)
        with tempfile.NamedTemporaryFile(delete=False) as source:
            source.write(b"keep this prefix")
            source_path = source.name
        try:
            with self.assertRaisesRegex(FileServiceError, "disk_write_failed"):
                service.write_file(source_path, "jobs/keep.bgcode")
        finally:
            os.unlink(source_path)
        self.assertNotIn("@RME FILE ABORT", commands)

    def test_discard_recovers_with_bulk_begin_then_confirms_line_abort(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 durable_resume=1"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BULK_READY offset=4096 chunk=384 window=4 resumed=1"
                ))
            elif command == "@RME FILE ABORT":
                service.handle_response(parse_line("RME_FILE_ABORTED"))

        service = RmeFileService(send, response_timeout=1)
        service.discard_partial("jobs/keep.bgcode", 8192, "a" * 64)
        self.assertTrue(any("WRITE_BULK_BEGIN" in item for item in commands))
        self.assertFalse(any("WRITE_BINARY_BEGIN" in item for item in commands))
        self.assertEqual("@RME FILE ABORT", commands[-1])

    def test_discard_aborts_an_already_active_line_receiver(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 durable_resume=1"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "echo:RME_ERROR workflow=file code=upload_state"
                ))
            elif command == "@RME FILE ABORT":
                service.handle_response(parse_line("RME_FILE_ABORTED"))

        service = RmeFileService(send, response_timeout=1)
        service.discard_partial("jobs/active.bgcode", 8192, "b" * 64)

        self.assertTrue(any("WRITE_BULK_BEGIN" in item for item in commands))
        self.assertEqual("@RME FILE ABORT", commands[-1])
        self.assertFalse(service.transport_mode_uncertain)

    def test_discard_locks_transport_when_active_receiver_abort_is_unconfirmed(self):
        service = None

        def send(command):
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb write=1 bulk=1 durable_resume=1"
                ))
            elif "WRITE_BULK_BEGIN" in command:
                service.handle_response(parse_line(
                    "echo:RME_ERROR workflow=file code=upload_state"
                ))

        service = RmeFileService(send, response_timeout=0.02)
        with self.assertRaisesRegex(
                FileServiceError, "teardown was not confirmed"):
            service.discard_partial("jobs/active.bgcode", 8192, "c" * 64)

        self.assertTrue(service.transport_mode_uncertain)

    def test_reconnect_probe_recovers_offset_then_suspends_raw_transport(self):
        service = None
        commands = []
        raw = []
        ended = []

        def send(command):
            commands.append(command)
            if command == "@RME FILE CAPS":
                service.handle_response(parse_line(
                    "RME_FILE_CAPS root=/usb binary=1 durable_resume=1 "
                    "binary_timeout_ms=10000"
                ))
            elif "WRITE_BINARY_BEGIN" in command:
                service.handle_response(parse_line(
                    "RME_FILE_BINARY_READY offset=4096 chunk=1024 window=8 "
                    "header=10 endian=little crc=crc32 resumed=1"
                ))

        def send_binary(frame):
            raw.append(frame)
            offset, length, _ = struct.unpack("<IHI", frame[:10])
            self.assertEqual(0xFFFFFFFF, offset)
            self.assertEqual(0, length)
            service.handle_response(parse_line(
                "RME_FILE_BINARY_ABORTED offset=4096 resumable=1"
            ))

        service = RmeFileService(
            send, response_timeout=1, send_binary=send_binary,
            begin_binary=lambda: "@RME FILE RAW_SESSION token=" + "b" * 32,
            end_binary=lambda: ended.append(True),
        )
        ready = service.probe_partial("jobs/resume.bgcode", 8192, "c" * 64)
        self.assertEqual(4096, ready["offset"])
        self.assertEqual(1, ready["resumed"])
        self.assertEqual(1, len(raw))
        self.assertEqual([True], ended)
        self.assertFalse(service.binary_mode_uncertain)

    def test_lost_manifest_cleanup_derives_only_current_private_sidecars(self):
        service = None
        commands = []

        def send(command):
            commands.append(command)
            if command.endswith("jobs/resume.bgcode.rme-part") and " STAT " in command:
                service.handle_response(parse_line(
                    "RME_FILE_STAT path=jobs/resume.bgcode.rme-part "
                    "type=file size=4096 mtime=1"
                ))
            elif command.endswith("jobs/resume.bgcode.rme-part") and " DELETE " in command:
                service.handle_response(parse_line("RME_FILE_DELETED"))
            elif command.endswith("jobs/resume.bgcode.rme-meta") and " STAT " in command:
                service.handle_response(parse_line(
                    "echo:RME_ERROR workflow=file code=not_found"
                ))

        service = RmeFileService(send, response_timeout=1)
        result = service.cleanup_orphan("jobs/resume.bgcode")

        self.assertEqual([
            {"path": "jobs/resume.bgcode.rme-part", "deleted": True},
            {"path": "jobs/resume.bgcode.rme-meta", "deleted": False},
        ], result)
        self.assertFalse(any(" LIST " in command for command in commands))
        self.assertFalse(any(".rme-old" in command for command in commands))
