"""End-to-end FILE transport tests over a fragmented simulated serial link."""

import hashlib
import queue
import re
import struct
import tempfile
import threading
import unittest
import zlib

from octoprint_rme_compatibility.file_service import RmeFileService
from octoprint_rme_compatibility.protocol import parse_line


class CurrentFirmwareSerialLink(object):
    """Minimal current-firmware CDC peer with independent RX/TX workers.

    Both directions are deliberately fragmented so this exercises the byte
    stream boundary, line reconstruction, raw frame reconstruction, negotiated
    window ACKs, and final hash verification instead of directly invoking
    ``handle_response`` from the sender callback.
    """

    chunk_size = 1024
    window_size = 8

    def __init__(self):
        self.service = None
        self.commands = []
        self.received = bytearray()
        self.expected_size = 0
        self.expected_sha = ""
        self.final_path = ""
        self._host_raw = False
        self._firmware_raw = False
        self._unacknowledged = 0
        self._host_to_firmware = queue.Queue()
        self._firmware_to_host = queue.Queue()
        self._errors = []
        self._firmware_thread = threading.Thread(
            target=self._firmware_worker, name="simulated-rme-firmware", daemon=True
        )
        self._host_reader_thread = threading.Thread(
            target=self._host_reader, name="simulated-octoprint-reader", daemon=True
        )
        self._firmware_thread.start()
        self._host_reader_thread.start()

    @staticmethod
    def _fragments(data):
        widths = (1, 2, 5, 13, 31)
        offset = 0
        index = 0
        while offset < len(data):
            width = widths[index % len(widths)]
            yield data[offset:offset + width]
            offset += width
            index += 1

    def send_command(self, command):
        if command.startswith("@RME FILE RAW_SESSION token="):
            self._host_raw = True
            return
        if self._host_raw:
            raise AssertionError("line command sent while host owns raw transport")
        self.commands.append(command)
        for fragment in self._fragments((command + "\n").encode("ascii")):
            self._host_to_firmware.put(fragment)

    def begin_binary(self):
        return "@RME FILE RAW_SESSION token=" + "a" * 32

    def send_binary(self, frame):
        if not self._host_raw:
            raise AssertionError("raw frame sent before writer reservation")
        for fragment in self._fragments(bytes(frame)):
            self._host_to_firmware.put(fragment)

    def end_binary(self):
        self._host_raw = False

    def _reply(self, *lines):
        payload = "".join(line + "\n" for line in lines).encode("ascii")
        for fragment in self._fragments(payload):
            self._firmware_to_host.put(fragment)

    def _handle_line(self, line):
        if line == "@RME FILE CAPS":
            self._reply(
                "RME_FILE_CAPS root=/usb chunk=48 bulk=1 bulk_chunk=384 "
                "bulk_window=4 binary=1 binary_chunk=1024 binary_window=8 "
                "binary_control=1 binary_control_offset=4294967294 "
                "resumable_abort=1 durable_resume=1 shared_transfer_latch=1 "
                "write=1",
                "ok",
            )
            return
        match = re.match(
            r"^@RME FILE WRITE_BINARY_BEGIN path=(\S+) size=(\d+) "
            r"sha256=([0-9a-f]{64})$",
            line,
        )
        if not match:
            raise AssertionError("unexpected firmware command: %s" % line)
        self.final_path = match.group(1)
        self.expected_size = int(match.group(2))
        self.expected_sha = match.group(3)
        self._firmware_raw = True
        self._reply(
            "RME_FILE_BINARY_READY offset=0 chunk=1024 window=8 "
            "header=10 endian=little crc=crc32 resumed=0",
            "ok",
        )

    def _handle_frame(self, frame):
        offset, length, checksum = struct.unpack("<IHI", frame[:10])
        payload = frame[10:]
        if length != len(payload):
            raise AssertionError("raw frame length mismatch")
        if checksum != zlib.crc32(payload) & 0xFFFFFFFF:
            raise AssertionError("raw frame CRC mismatch")
        if not payload:
            if offset != self.expected_size or len(self.received) != self.expected_size:
                raise AssertionError("invalid completion offset")
            if hashlib.sha256(self.received).hexdigest() != self.expected_sha:
                raise AssertionError("completed upload SHA-256 mismatch")
            self._firmware_raw = False
            self._reply("RME_FILE_BINARY_COMPLETE path=" + self.final_path, "ok")
            return
        if offset != len(self.received):
            raise AssertionError("non-contiguous upload offset")
        if length > self.chunk_size:
            raise AssertionError("host exceeded negotiated binary chunk")
        self.received.extend(payload)
        self._unacknowledged += 1
        acknowledge = (
            self._unacknowledged >= self.window_size
            or len(self.received) == self.expected_size
        )
        if acknowledge:
            self._unacknowledged = 0
            # At the second window, inject a delayed duplicate ACK for the
            # preceding window before the current cumulative ACK.
            if len(self.received) == self.chunk_size * self.window_size * 2:
                self._reply(
                    "RME_FILE_BINARY_ACK offset=%d"
                    % (self.chunk_size * self.window_size)
                )
            self._reply("RME_FILE_BINARY_ACK offset=%d" % len(self.received))

    def _firmware_worker(self):
        line_buffer = bytearray()
        raw_buffer = bytearray()
        try:
            while True:
                fragment = self._host_to_firmware.get()
                if fragment is None:
                    return
                if self._firmware_raw:
                    raw_buffer.extend(fragment)
                    while len(raw_buffer) >= 10:
                        length = struct.unpack("<H", raw_buffer[4:6])[0]
                        frame_size = 10 + length
                        if len(raw_buffer) < frame_size:
                            break
                        frame = bytes(raw_buffer[:frame_size])
                        del raw_buffer[:frame_size]
                        self._handle_frame(frame)
                else:
                    line_buffer.extend(fragment)
                    while b"\n" in line_buffer:
                        raw_line, _, remainder = line_buffer.partition(b"\n")
                        line_buffer[:] = remainder
                        self._handle_line(raw_line.rstrip(b"\r").decode("ascii"))
        except Exception as exc:
            self._errors.append(exc)

    def _host_reader(self):
        line_buffer = bytearray()
        try:
            while True:
                fragment = self._firmware_to_host.get()
                if fragment is None:
                    return
                line_buffer.extend(fragment)
                while b"\n" in line_buffer:
                    raw_line, _, remainder = line_buffer.partition(b"\n")
                    line_buffer[:] = remainder
                    record = parse_line(raw_line.rstrip(b"\r").decode("ascii"))
                    if record is not None:
                        self.service.handle_response(record)
        except Exception as exc:
            self._errors.append(exc)

    def close(self):
        self._host_to_firmware.put(None)
        self._firmware_to_host.put(None)
        self._firmware_thread.join(2)
        self._host_reader_thread.join(2)
        if self._errors:
            raise self._errors[0]


class SerialLinkIntegrationTests(unittest.TestCase):
    def test_current_binary_firmware_upload_over_fragmented_serial_link(self):
        link = CurrentFirmwareSerialLink()
        service = RmeFileService(
            link.send_command,
            response_timeout=2,
            send_binary=link.send_binary,
            begin_binary=link.begin_binary,
            end_binary=link.end_binary,
        )
        link.service = service
        progress = []
        content = bytes(range(256)) * 1024 + b"signed-firmware-tail"

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                service.write_file(
                    source.name,
                    "FWUPD.BBF",
                    progress=lambda offset, size: progress.append((offset, size)),
                )
            finally:
                link.close()

        self.assertEqual(content, bytes(link.received))
        self.assertEqual(hashlib.sha256(content).hexdigest(), link.expected_sha)
        self.assertEqual((len(content), len(content)), progress[-1])
        self.assertTrue(any("WRITE_BINARY_BEGIN" in item for item in link.commands))
        self.assertFalse(any("WRITE_BULK_BEGIN" in item for item in link.commands))


if __name__ == "__main__":
    unittest.main()
