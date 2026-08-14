"""End-to-end FILE transports against an independent firmware state model."""

import base64
import hashlib
from pathlib import Path
import queue
import re
import struct
import subprocess
import tempfile
import threading
import unittest
import zlib

from octoprint_rme_compatibility.file_service import RmeFileService
from octoprint_rme_compatibility.protocol import parse_line


class FirmwareFileServicePeer(object):
    """Independent current-firmware FILE state model on a byte-stream link.

    This models firmware-owned state (durable prefix, raw/line ownership,
    preserved ACK count, recovery mode, and final SHA) rather than echoing the
    host's requested offsets. Both directions are fragmented and faults are
    injected into serialized bytes before the firmware model sees them.
    """

    chunk_size = 1024
    window_size = 8

    bulk_chunk_size = 384
    bulk_window_size = 4
    legacy_chunk_size = 48

    def __init__(
        self, suspend_binary_once=False, suspend_binary_count=0,
        corrupt_frame_position=None,
        fail_raw_writer_once=False, corrupt_bulk_line_once=False,
        reject_bulk_window_once=False, reject_bulk_window_count=0,
        suspend_bulk_count=0, suspend_legacy_count=0,
        suspend_bulk_after_ack_count=0,
        bulk_supported=True, confirm_abort=True,
        fragment_widths=(1, 2, 5, 13, 31),
    ):
        self.service = None
        self.commands = []
        self.received = bytearray()
        self.expected_size = 0
        self.expected_sha = ""
        self.final_path = ""
        self.published_path = ""
        self.flash_queued = False
        self._host_raw = False
        self._firmware_raw = False
        self._unacknowledged = 0
        self._binary_suspensions_remaining = max(
            int(suspend_binary_count), 1 if suspend_binary_once else 0
        )
        self._corrupt_frame_position = corrupt_frame_position
        self._data_frame_index = 0
        self._fault_injected = False
        self._binary_nacked = False
        self._recovering = False
        self._line_transport = None
        self._line_unacknowledged = 0
        self._fail_raw_writer_once = bool(fail_raw_writer_once)
        self._raw_writer_failed = False
        self._corrupt_bulk_line_once = bool(corrupt_bulk_line_once)
        self._bulk_line_corrupted = False
        self._reject_bulk_windows_remaining = max(
            int(reject_bulk_window_count), 1 if reject_bulk_window_once else 0
        )
        self._bulk_window_rejected = False
        self._bulk_suspensions_remaining = max(0, int(suspend_bulk_count))
        self._legacy_suspensions_remaining = max(0, int(suspend_legacy_count))
        self._bulk_post_ack_suspensions_remaining = max(
            0, int(suspend_bulk_after_ack_count)
        )
        self._bulk_supported = bool(bulk_supported)
        self._confirm_abort = bool(confirm_abort)
        self._fragment_widths = tuple(fragment_widths)
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

    def _fragments(self, data):
        widths = self._fragment_widths
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
        wire_command = command
        if (
            self._corrupt_bulk_line_once
            and not self._bulk_line_corrupted
            and command.startswith("@RME FILE WRITE_BULK_CHUNK ")
        ):
            prefix, encoded = command.split("data=", 1)
            wire_command = prefix + "data=!" + encoded[1:]
            self._bulk_line_corrupted = True
        for fragment in self._fragments((wire_command + "\n").encode("ascii")):
            self._host_to_firmware.put(fragment)

    def begin_binary(self):
        return "@RME FILE RAW_SESSION token=" + "a" * 32

    def send_binary(self, frame):
        if not self._host_raw:
            raise AssertionError("raw frame sent before writer reservation")
        wire_frame = bytes(frame)
        _, length, _ = struct.unpack("<IHI", wire_frame[:10])
        if length:
            if self._fail_raw_writer_once and not self._raw_writer_failed:
                self._raw_writer_failed = True
                raise IOError("simulated USB raw-writer failure")
            if (
                not self._fault_injected
                and self._corrupt_frame_position == self._data_frame_index
            ):
                damaged = bytearray(wire_frame)
                damaged[-1] ^= 0x01
                wire_frame = bytes(damaged)
                self._fault_injected = True
            self._data_frame_index += 1
        for fragment in self._fragments(wire_frame):
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
                "RME_FILE_CAPS root=/usb chunk=48 bulk=%d bulk_chunk=384 "
                "bulk_window=4 binary=1 binary_chunk=1024 binary_window=8 "
                "binary_control=1 binary_control_offset=4294967294 "
                "resumable_abort=1 durable_resume=1 shared_transfer_latch=1 "
                "binary_timeout_ms=10000 upload_timeout_ms=10000 write=1"
                % int(self._bulk_supported),
                "ok",
            )
            return
        if line == "@RME FILE ABORT":
            self.received.clear()
            self._line_transport = None
            self._line_unacknowledged = 0
            self._firmware_raw = False
            if self._confirm_abort:
                self._reply("RME_FILE_ABORTED", "ok")
            return
        if line == "@RME FILE FLASH path=FWUPD.RME":
            if self.published_path != "FWUPD.RME":
                self._reply("echo:RME_ERROR workflow=file code=not_found")
            else:
                self.flash_queued = True
                self._reply("RME_FILE_FLASH_QUEUED", "ok")
            return
        begin = re.match(
            r"^@RME FILE WRITE_(BULK_)?BEGIN path=(\S+) size=(\d+) "
            r"sha256=([0-9a-f]{64})$",
            line,
        )
        if begin:
            bulk = bool(begin.group(1))
            path = begin.group(2)
            size = int(begin.group(3))
            digest = begin.group(4)
            resumed = self._prepare_upload(path, size, digest)
            self._line_transport = "bulk" if bulk else "legacy"
            self._line_unacknowledged = 0
            if bulk:
                self._reply(
                    "RME_FILE_BULK_READY offset=%d chunk=384 window=4 resumed=%d"
                    % (len(self.received), resumed),
                    "ok",
                )
            else:
                self._reply(
                    "RME_FILE_WRITE_READY offset=%d chunk=48 resumed=%d"
                    % (len(self.received), resumed),
                    "ok",
                )
            return
        bulk_chunk = re.match(
            r"^@RME FILE WRITE_BULK_CHUNK offset=(\d+) data=(\S+)$", line
        )
        if bulk_chunk:
            self._handle_line_chunk(bulk_chunk, bulk=True)
            return
        legacy_chunk = re.match(
            r"^@RME FILE WRITE_CHUNK path=\S+ offset=(\d+) data=(\S+)$", line
        )
        if legacy_chunk:
            self._handle_line_chunk(legacy_chunk, bulk=False)
            return
        end = re.match(r"^@RME FILE WRITE_(BULK_)?END path=(\S+)$", line)
        if end:
            bulk = bool(end.group(1))
            if self._line_transport != ("bulk" if bulk else "legacy"):
                self._reply("echo:RME_ERROR workflow=file code=upload_state")
                return
            self._publish()
            self._line_transport = None
            self._reply("RME_FILE_WRITE_COMPLETE path=" + end.group(2), "ok")
            return
        match = re.match(
            r"^@RME FILE WRITE_BINARY_BEGIN path=(\S+) size=(\d+) "
            r"sha256=([0-9a-f]{64})$",
            line,
        )
        if not match:
            raise AssertionError("unexpected firmware command: %s" % line)
        self._prepare_upload(match.group(1), int(match.group(2)), match.group(3))
        self._firmware_raw = True
        self._unacknowledged = 0
        self._recovering = False
        offset = len(self.received)
        self._reply(
            "RME_FILE_BINARY_READY offset=%d chunk=1024 window=8 "
            "header=10 endian=little crc=crc32 resumed=%d"
            % (offset, 1 if offset else 0),
            "ok",
        )

    def _prepare_upload(self, path, size, digest):
        resumed = int(
            self.final_path == path
            and self.expected_size == size
            and self.expected_sha == digest
            and len(self.received) <= size
        )
        if not resumed:
            self.received.clear()
        self.final_path = path
        self.expected_size = size
        self.expected_sha = digest
        return resumed

    def _handle_line_chunk(self, match, bulk):
        expected_transport = "bulk" if bulk else "legacy"
        offset = int(match.group(1))
        encoded = match.group(2)
        if self._line_transport != expected_transport or offset != len(self.received):
            self._reply("echo:RME_ERROR workflow=file code=upload_state")
            return
        if bulk and self._reject_bulk_windows_remaining:
            self._reject_bulk_windows_remaining -= 1
            self._bulk_window_rejected = True
            self._reply("echo:RME_ERROR workflow=file code=upload_state")
            return
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception:
            self._line_transport = None
            self._reply(
                "echo:RME_ERROR workflow=file code=decode_failed offset=%d resumable=1"
                % len(self.received)
            )
            return
        limit = self.bulk_chunk_size if bulk else self.legacy_chunk_size
        if len(payload) > limit:
            self._line_transport = None
            self._reply(
                "echo:RME_ERROR workflow=file code=chunk_too_large offset=%d resumable=1"
                % len(self.received)
            )
            return
        self.received.extend(payload)
        if bulk:
            self._line_unacknowledged += 1
            if (
                self._line_unacknowledged >= self.bulk_window_size
                or len(self.received) == self.expected_size
            ):
                self._line_unacknowledged = 0
                if self._bulk_suspensions_remaining:
                    self._bulk_suspensions_remaining -= 1
                    self._line_transport = None
                    self._reply(
                        "RME_FILE_SUSPENDED offset=%d resumable=1 "
                        "reason=inactivity_timeout" % len(self.received)
                    )
                    return
                if self._bulk_post_ack_suspensions_remaining:
                    self._bulk_post_ack_suspensions_remaining -= 1
                    self._line_transport = None
                    self._reply(
                        "RME_FILE_BULK_ACK offset=%d" % len(self.received),
                        "RME_FILE_SUSPENDED offset=%d resumable=1 "
                        "reason=inactivity_timeout" % len(self.received),
                    )
                    return
                self._reply("RME_FILE_BULK_ACK offset=%d" % len(self.received))
        else:
            if self._legacy_suspensions_remaining:
                self._legacy_suspensions_remaining -= 1
                self._line_transport = None
                self._reply(
                    "RME_FILE_SUSPENDED offset=%d resumable=1 "
                    "reason=inactivity_timeout" % len(self.received)
                )
                return
            self._reply("RME_FILE_WRITE_OFFSET offset=%d" % len(self.received))

    def _publish(self):
        if len(self.received) != self.expected_size:
            raise AssertionError("completed upload size mismatch")
        if hashlib.sha256(self.received).hexdigest() != self.expected_sha:
            raise AssertionError("completed upload SHA-256 mismatch")
        self.published_path = (
            "FWUPD.RME" if self.final_path == "FWUPD.BBF" else self.final_path
        )

    def _handle_frame(self, frame):
        offset, length, checksum = struct.unpack("<IHI", frame[:10])
        payload = frame[10:]
        if length != len(payload):
            raise AssertionError("raw frame length mismatch")
        if not payload:
            if offset == 0xFFFFFFFF:
                self._firmware_raw = False
                self._reply(
                    "RME_FILE_BINARY_ABORTED offset=%d resumable=1"
                    % len(self.received)
                )
                return
            if offset != self.expected_size or len(self.received) != self.expected_size:
                raise AssertionError("invalid completion offset")
            self._firmware_raw = False
            self._publish()
            self._reply("RME_FILE_BINARY_COMPLETE path=" + self.final_path, "ok")
            return
        if offset != len(self.received):
            # Current firmware emits one diagnostic and silently slides over
            # the remaining stale frames from an already-pipelined window.
            self._recovering = True
            return
        if length > self.chunk_size:
            raise AssertionError("host exceeded negotiated binary chunk")
        if checksum != zlib.crc32(payload) & 0xFFFFFFFF:
            if not self._recovering:
                self._binary_nacked = True
                self._reply(
                    "RME_FILE_BINARY_NACK offset=%d reason=crc_mismatch recovering=1"
                    % len(self.received)
                )
            self._recovering = True
            return
        self._recovering = False
        self.received.extend(payload)
        self._unacknowledged += 1
        acknowledge = (
            self._unacknowledged >= self.window_size
            or len(self.received) == self.expected_size
        )
        if acknowledge:
            self._unacknowledged = 0
            if self._binary_suspensions_remaining:
                self._binary_suspensions_remaining -= 1
                self._firmware_raw = False
                self._reply(
                    "RME_FILE_BINARY_SUSPENDED offset=%d resumable=1 "
                    "reason=inactivity_timeout" % len(self.received)
                )
                return
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
        link = FirmwareFileServicePeer()
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

    def test_binary_inactivity_resumes_verified_prefix_without_bulk_fallback(self):
        link = FirmwareFileServicePeer(suspend_binary_once=True)
        service = RmeFileService(
            link.send_command,
            response_timeout=2,
            send_binary=link.send_binary,
            begin_binary=link.begin_binary,
            end_binary=link.end_binary,
        )
        link.service = service
        content = bytes(range(256)) * 160 + b"resumed-firmware-tail"

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                service.write_file(source.name, "FWUPD.BBF")
            finally:
                link.close()

        begins = [item for item in link.commands if "WRITE_BINARY_BEGIN" in item]
        self.assertEqual(2, len(begins))
        self.assertFalse(any("WRITE_BULK_BEGIN" in item for item in link.commands))
        self.assertEqual(content, bytes(link.received))
        self.assertEqual(hashlib.sha256(content).hexdigest(), link.expected_sha)

    def test_binary_fallback_resumes_current_bulk_suspensions(self):
        link = FirmwareFileServicePeer(
            suspend_binary_count=3, suspend_bulk_count=2
        )
        service = RmeFileService(
            link.send_command,
            response_timeout=2,
            send_binary=link.send_binary,
            begin_binary=link.begin_binary,
            end_binary=link.end_binary,
        )
        link.service = service
        content = bytes(range(251)) * 200 + b"three-suspension-tail"

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                service.write_file(source.name, "FWUPD.BBF")
            finally:
                link.close()

        binary_begins = [
            item for item in link.commands if "WRITE_BINARY_BEGIN" in item
        ]
        self.assertEqual(3, len(binary_begins))
        bulk_begins = [item for item in link.commands if "WRITE_BULK_BEGIN" in item]
        self.assertEqual(3, len(bulk_begins))
        self.assertEqual(content, bytes(link.received))
        self.assertEqual("FWUPD.RME", link.published_path)

    def test_current_legacy_suspension_resumes_verified_prefix(self):
        link = FirmwareFileServicePeer(
            bulk_supported=False, suspend_legacy_count=2
        )
        service = RmeFileService(link.send_command, response_timeout=2)
        link.service = service
        content = bytes(range(251)) * 12 + b"legacy-resume-tail"

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                service.write_file(source.name, "jobs/resumed.bgcode")
            finally:
                link.close()

        begins = [
            item for item in link.commands
            if "WRITE_BEGIN" in item and "WRITE_BULK_BEGIN" not in item
        ]
        self.assertEqual(3, len(begins))
        self.assertEqual(content, bytes(link.received))
        self.assertEqual("jobs/resumed.bgcode", link.published_path)

    def test_bulk_suspension_racing_final_ack_is_not_lost(self):
        link = FirmwareFileServicePeer(suspend_bulk_after_ack_count=1)
        service = RmeFileService(link.send_command, response_timeout=2)
        link.service = service
        content = bytes(range(251)) * 5

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                service.write_file(source.name, "jobs/ack-race.bgcode")
            finally:
                link.close()

        begins = [item for item in link.commands if "WRITE_BULK_BEGIN" in item]
        self.assertEqual(2, len(begins))
        self.assertEqual(content, bytes(link.received))
        self.assertEqual("jobs/ack-race.bgcode", link.published_path)

    def test_persistent_bulk_parser_loss_is_aborted_before_commands_resume(self):
        link = FirmwareFileServicePeer(
            suspend_binary_count=3, reject_bulk_window_count=20
        )
        service = RmeFileService(
            link.send_command,
            response_timeout=2,
            send_binary=link.send_binary,
            begin_binary=link.begin_binary,
            end_binary=link.end_binary,
        )
        link.service = service
        content = bytes(range(251)) * 100

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                with self.assertRaisesRegex(
                    Exception, "partial upload was safely discarded"
                ):
                    service.write_file(source.name, "FWUPD.BBF")
            finally:
                link.close()

        self.assertEqual("@RME FILE ABORT", link.commands[-1])
        self.assertEqual(b"", bytes(link.received))
        self.assertFalse(service.binary_mode_uncertain)

    def test_unconfirmed_bulk_abort_locks_all_following_transport(self):
        link = FirmwareFileServicePeer(
            suspend_binary_count=3,
            reject_bulk_window_count=20,
            confirm_abort=False,
        )
        service = RmeFileService(
            link.send_command,
            response_timeout=0.2,
            send_binary=link.send_binary,
            begin_binary=link.begin_binary,
            end_binary=link.end_binary,
        )
        link.service = service

        with tempfile.NamedTemporaryFile() as source:
            source.write(bytes(range(251)) * 100)
            source.flush()
            try:
                with self.assertRaisesRegex(
                    Exception, "teardown was not confirmed"
                ):
                    service.write_file(source.name, "FWUPD.BBF")
            finally:
                link.close()

        self.assertEqual("@RME FILE ABORT", link.commands[-1])
        self.assertTrue(service.transport_mode_uncertain)

    def test_crc_fault_at_every_window_position_preserves_ack_cadence(self):
        content = bytes(range(256)) * 96 + b"nack-recovery-tail"
        for fault_position in range(FirmwareFileServicePeer.window_size):
            with self.subTest(fault_position=fault_position):
                link = FirmwareFileServicePeer(
                    corrupt_frame_position=fault_position
                )
                service = RmeFileService(
                    link.send_command,
                    response_timeout=2,
                    send_binary=link.send_binary,
                    begin_binary=link.begin_binary,
                    end_binary=link.end_binary,
                )
                link.service = service
                with tempfile.NamedTemporaryFile() as source:
                    source.write(content)
                    source.flush()
                    try:
                        service.write_file(source.name, "FWUPD.BBF")
                    finally:
                        link.close()

                self.assertTrue(link._fault_injected)
                self.assertTrue(link._binary_nacked)
                self.assertEqual(content, bytes(link.received))
                self.assertFalse(any(
                    "WRITE_BULK_BEGIN" in item for item in link.commands
                ))

    def test_file_and_firmware_boundaries_and_firmware_flash(self):
        sizes = (1, 1023, 1024, 1025, 8191, 8192, 8193)
        for destination in ("jobs/model.bgcode", "FWUPD.BBF"):
            for size in sizes:
                with self.subTest(destination=destination, size=size):
                    link = FirmwareFileServicePeer()
                    service = RmeFileService(
                        link.send_command,
                        response_timeout=2,
                        send_binary=link.send_binary,
                        begin_binary=link.begin_binary,
                        end_binary=link.end_binary,
                    )
                    link.service = service
                    content = bytes(index % 251 for index in range(size))
                    with tempfile.NamedTemporaryFile() as source:
                        source.write(content)
                        source.flush()
                        try:
                            service.write_file(source.name, destination)
                            if destination == "FWUPD.BBF":
                                service.mutate("FLASH", "FWUPD.RME")
                        finally:
                            link.close()

                    self.assertEqual(content, bytes(link.received))
                    self.assertEqual(
                        "FWUPD.RME" if destination == "FWUPD.BBF" else destination,
                        link.published_path,
                    )
                    self.assertEqual(
                        destination == "FWUPD.BBF", link.flash_queued
                    )

    def test_raw_failure_and_bulk_decode_failure_resume_with_legacy_bytes(self):
        link = FirmwareFileServicePeer(
            fail_raw_writer_once=True, corrupt_bulk_line_once=True
        )
        service = RmeFileService(
            link.send_command,
            response_timeout=2,
            send_binary=link.send_binary,
            begin_binary=link.begin_binary,
            end_binary=link.end_binary,
        )
        link.service = service
        content = bytes(range(251)) * 17 + b"fallback-firmware-tail"

        with tempfile.NamedTemporaryFile() as source:
            source.write(content)
            source.flush()
            try:
                service.write_file(source.name, "FWUPD.BBF")
            finally:
                link.close()

        self.assertTrue(link._raw_writer_failed)
        self.assertTrue(link._bulk_line_corrupted)
        self.assertEqual(content, bytes(link.received))
        self.assertEqual("FWUPD.RME", link.published_path)
        self.assertTrue(any("WRITE_BINARY_BEGIN" in item for item in link.commands))
        self.assertTrue(any("WRITE_BULK_BEGIN" in item for item in link.commands))
        self.assertTrue(any("WRITE_BEGIN" in item for item in link.commands))
        self.assertTrue(any(
            "WRITE_END" in item and "WRITE_BULK_END" not in item
            for item in link.commands
        ))
        self.assertFalse(any("WRITE_BULK_END" in item for item in link.commands))

    def test_serial_fragmentation_patterns_do_not_change_protocol_results(self):
        content = bytes(range(251)) * 37
        patterns = ((1,), (2, 3, 5, 7), (63, 64, 127))
        for widths in patterns:
            with self.subTest(fragment_widths=widths):
                link = FirmwareFileServicePeer(fragment_widths=widths)
                service = RmeFileService(
                    link.send_command,
                    response_timeout=2,
                    send_binary=link.send_binary,
                    begin_binary=link.begin_binary,
                    end_binary=link.end_binary,
                )
                link.service = service
                with tempfile.NamedTemporaryFile() as source:
                    source.write(content)
                    source.flush()
                    try:
                        service.write_file(source.name, "jobs/fragmented.bgcode")
                    finally:
                        link.close()
                self.assertEqual(content, bytes(link.received))
                self.assertEqual("jobs/fragmented.bgcode", link.published_path)


class CheckedOutFirmwareContractTests(unittest.TestCase):
    """Prevent the independent peer from silently drifting from Buddy."""

    def test_peer_constants_and_nack_state_match_current_firmware_checkout(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "Prusa-Firmware-Buddy/src/marlin_stubs/rme_file_service.cpp"
        )
        if not source.exists():
            self.skipTest("adjacent current Prusa-Firmware-Buddy checkout unavailable")
        firmware = source.read_text(encoding="utf-8")
        firmware_root = source.parents[2]
        transfer_contract = (
            firmware_root / "src/common/rme_file_transfer.hpp"
        ).read_text(encoding="utf-8")
        tinyusb = (
            firmware_root / "include/tinyusb/tusb_config.h"
        ).read_text(encoding="utf-8")
        connect_renderer = (
            firmware_root / "src/connect/render.cpp"
        ).read_text(encoding="utf-8")
        host_service = (
            Path(__file__).resolve().parents[1]
            / "octoprint_rme_compatibility/file_service.py"
        ).read_text(encoding="utf-8")

        expected_constants = {
            "transfer_chunk_size": FirmwareFileServicePeer.legacy_chunk_size,
            "binary_chunk_size": FirmwareFileServicePeer.chunk_size,
            "binary_window_size": FirmwareFileServicePeer.window_size,
        }
        for name, value in expected_constants.items():
            with self.subTest(constant=name):
                self.assertRegex(
                    firmware,
                    r"constexpr\s+[^;=]+\s+%s\s*=\s*%d\s*;"
                    % (re.escape(name), value),
                )
        self.assertRegex(
            transfer_contract,
            r"bulk_payload_size\s*=\s*%d\s*;"
            % FirmwareFileServicePeer.bulk_chunk_size,
        )
        self.assertRegex(
            transfer_contract,
            r"bulk_window_size\s*=\s*%d\s*;"
            % FirmwareFileServicePeer.bulk_window_size,
        )
        self.assertIn(
            "bulk_chunk_size = rme_file_transfer::bulk_payload_size", firmware,
        )
        self.assertIn(
            "bulk_window_size = rme_file_transfer::bulk_window_size", firmware,
        )
        self.assertRegex(
            firmware,
            r"upload_inactivity_timeout_ms\s*=\s*10'000\s*;",
        )
        self.assertIn("upload_timeout_ms=", firmware)
        self.assertIn("RME_FILE_SUSPENDED offset=", firmware)
        self.assertIn("upload.last_activity_ms = ticks_ms();", firmware)
        self.assertRegex(tinyusb, r"CFG_TUD_CDC_RX_BUFSIZE\s+2048")
        self.assertIn("bulk_receive_backlog", firmware)
        self.assertIn("const char *lfn = dirent_lfn", connect_renderer)
        self.assertIn("filename_is_rme_private(lfn)", connect_renderer)
        self.assertNotIn("BINARY_FRAME_SEND_DELAY", host_service)
        self.assertNotIn("BULK_COMMAND_SEND_DELAY", host_service)
        self.assertIn(
            "const uint8_t unacknowledged = binary_receiver.unacknowledged;",
            firmware,
        )
        self.assertIn(
            "binary_receiver.unacknowledged = unacknowledged;",
            firmware,
        )
        self.assertIn(
            "++binary_receiver.unacknowledged >= binary_window_size",
            firmware,
        )
        self.assertIn(
            "binary_receiver.unacknowledged = acknowledge ? 0 : unacknowledged;",
            firmware,
        )

    def test_current_indx_extrusion_fault_workflow_contract(self):
        firmware_root = (
            Path(__file__).resolve().parents[2] / "Prusa-Firmware-Buddy"
        )
        marlin_server_path = firmware_root / "src/common/marlin_server.cpp"
        if not marlin_server_path.exists():
            self.skipTest("adjacent current Prusa-Firmware-Buddy checkout unavailable")

        marlin_server = marlin_server_path.read_text(encoding="utf-8")
        m591 = (firmware_root / "src/marlin_stubs/M591.cpp").read_text(
            encoding="utf-8"
        )
        protocol_doc = (
            firmware_root / "doc/rme_serial_remote_protocol.md"
        ).read_text(encoding="utf-8")
        plugin_protocol = (
            Path(__file__).resolve().parents[1]
            / "octoprint_rme_compatibility/protocol.py"
        ).read_text(encoding="utf-8")

        contracts = (
            ("filament_runout", "runout", "M1601 R1"),
            ("filament_movement", "not_moving", "M1601 R2"),
            ("extrusion_flow_limit", "flow_limit", "M1601 R3"),
        )
        for workflow, code, command in contracts:
            with self.subTest(workflow=workflow):
                self.assertIn(
                    'notify_error("%s", "%s"' % (workflow, code),
                    marlin_server,
                )
                self.assertIn(command, marlin_server)
                self.assertIn("workflow=%s" % workflow, protocol_doc)
                self.assertIn('"%s"' % workflow, plugin_protocol)

        self.assertIn("Loadcell filament runout detection ", m591)
        self.assertIn("Loadcell filament movement detection ", m591)
        self.assertIn("M591 S", protocol_doc)
        self.assertIn("M591 U", protocol_doc)
        self.assertIn("Retain the cause until recovery closes", protocol_doc)

    def test_657_release_exposes_the_same_host_workflow_and_transfer_contract(self):
        firmware_root = (
            Path(__file__).resolve().parents[2] / "Prusa-Firmware-Buddy"
        )
        if not (firmware_root / ".git").exists():
            self.skipTest("adjacent Prusa-Firmware-Buddy git checkout unavailable")

        def release_file(path):
            try:
                return subprocess.check_output(
                    ["git", "-C", str(firmware_root), "show", "v6.5.7-RME:%s" % path],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("v6.5.7-RME release ref unavailable")

        marlin_server = release_file("src/common/marlin_server.cpp")
        file_service = release_file("src/marlin_stubs/rme_file_service.cpp")
        transfer_contract = release_file("src/common/rme_file_transfer.hpp")
        protocol_doc = release_file("doc/rme_serial_remote_protocol.md")

        for workflow, code, command in (
            ("filament_runout", "runout", "M1601 R1"),
            ("filament_movement", "not_moving", "M1601 R2"),
            ("extrusion_flow_limit", "flow_limit", "M1601 R3"),
        ):
            with self.subTest(workflow=workflow):
                self.assertIn(
                    'notify_error("%s", "%s"' % (workflow, code),
                    marlin_server,
                )
                self.assertIn(command, marlin_server)
                self.assertIn("workflow=%s" % workflow, protocol_doc)

        self.assertRegex(file_service, r"binary_chunk_size\s*=\s*1024\s*;")
        self.assertRegex(file_service, r"binary_window_size\s*=\s*8\s*;")
        self.assertRegex(
            transfer_contract,
            r"bulk_payload_size\s*=\s*%d\s*;"
            % FirmwareFileServicePeer.bulk_chunk_size,
        )
        self.assertRegex(
            transfer_contract,
            r"bulk_window_size\s*=\s*%d\s*;"
            % FirmwareFileServicePeer.bulk_window_size,
        )
        self.assertIn("RME_FILE_SUSPENDED offset=", file_service)
        self.assertIn("upload_inactivity_timeout_ms = 10'000", file_service)


if __name__ == "__main__":
    unittest.main()
