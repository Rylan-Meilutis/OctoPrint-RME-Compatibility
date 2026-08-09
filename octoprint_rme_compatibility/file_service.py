"""Serialized client for the RME firmware's sandboxed ``/usb`` service."""

import base64
import hashlib
import os
import threading
import time
from urllib.parse import quote


FILE_CHUNK_SIZE = 48


class FileServiceError(RuntimeError):
    """A safe, operator-facing USB filesystem failure."""


def normalize_remote_path(path):
    """Validate a UI path and encode it for one ``@RME FILE`` command."""
    text = str(path or "/").replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise FileServiceError("Parent path segments are not allowed")
    if any(any(ord(character) < 32 for character in part) for part in parts):
        raise FileServiceError("Control characters are not allowed in USB paths")
    normalized = "/".join(parts)
    if len(normalized.encode("utf-8")) > 220:
        raise FileServiceError("USB path is too long")
    return quote(normalized, safe="/-_.~") if normalized else "/"


class RmeFileService(object):
    """Perform one acknowledged filesystem transaction at a time.

    OctoPrint remains the sole owner of the serial descriptor. ``send_command``
    queues commands through its normal writer, while ``handle_response`` is fed
    parsed records by the receive hook.
    """

    def __init__(self, send_command, logger=None, response_timeout=20):
        self.send_command = send_command
        self.logger = logger
        self.response_timeout = response_timeout
        self._operation_lock = threading.Lock()
        self._condition = threading.Condition()
        self._expected = set()
        self._records = []
        self._error = None
        self._active = False

    @property
    def busy(self):
        if self._operation_lock.locked():
            return True
        with self._condition:
            return self._active

    def reset(self, reason="Printer disconnected"):
        """Interrupt a waiter when the serial connection disappears."""
        with self._condition:
            if self._active:
                self._error = reason
            self._condition.notify_all()

    def handle_response(self, record):
        """Wake the active HTTP/API worker for relevant parsed file records."""
        if not record or not str(record.get("record", "")).startswith("file_"):
            return
        with self._condition:
            if not self._active:
                return
            if record["record"] == "file_error":
                self._error = record.get("code") or record.get("message")
            else:
                self._records.append(dict(record))
            self._condition.notify_all()

    def _exchange(self, command, expected, timeout=None):
        """Send a command and return all records through its terminal reply."""
        expected = set(expected if isinstance(expected, (tuple, list, set)) else [expected])
        with self._condition:
            self._expected = expected
            self._records = []
            self._error = None
            self._active = True
        try:
            self.send_command(command)
            deadline = time.monotonic() + (timeout or self.response_timeout)
            with self._condition:
                while not any(item.get("record") in expected for item in self._records):
                    if self._error:
                        raise FileServiceError("Printer USB operation failed: %s" % self._error)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FileServiceError("Timed out waiting for printer USB response")
                    self._condition.wait(min(remaining, 0.5))
                return list(self._records)
        finally:
            with self._condition:
                self._active = False
                self._expected.clear()
                self._condition.notify_all()

    @staticmethod
    def _terminal(records, record_name):
        return next(item for item in reversed(records) if item.get("record") == record_name)

    def capabilities(self):
        with self._operation_lock:
            records = self._exchange("@RME FILE CAPS", "file_caps")
        return self._terminal(records, "file_caps")

    def list_directory(self, path="/"):
        encoded = normalize_remote_path(path)
        with self._operation_lock:
            records = self._exchange(
                "@RME FILE LIST path=%s" % encoded, "file_list_end", timeout=30
            )
        return [item for item in records if item.get("record") == "file_entry"]

    def stat(self, path):
        encoded = normalize_remote_path(path)
        with self._operation_lock:
            records = self._exchange("@RME FILE STAT path=%s" % encoded, "file_stat")
        return self._terminal(records, "file_stat")

    def iter_file(self, path):
        """Yield decoded 48-byte blocks while holding the filesystem lease."""
        encoded = normalize_remote_path(path)
        with self._operation_lock:
            offset = 0
            while True:
                records = self._exchange(
                    "@RME FILE READ path=%s offset=%d length=%d"
                    % (encoded, offset, FILE_CHUNK_SIZE),
                    "file_data",
                )
                record = self._terminal(records, "file_data")
                if int(record.get("offset", -1)) != offset:
                    raise FileServiceError("Printer returned an unexpected file offset")
                try:
                    block = base64.b64decode(str(record.get("data", "")), validate=True)
                except Exception as exc:
                    raise FileServiceError("Printer returned invalid Base64 file data") from exc
                if len(block) != int(record.get("length", -1)):
                    raise FileServiceError("Printer returned an invalid file-data length")
                if block:
                    yield block
                    offset += len(block)
                if record.get("eof"):
                    return
                if not block:
                    raise FileServiceError("Printer file download made no progress")

    def write_file(self, local_path, remote_path, progress=None):
        """Upload, hash-check, and atomically publish one local file on USB."""
        encoded = normalize_remote_path(remote_path)
        size, digest = self._hash_file(local_path)
        with self._operation_lock:
            try:
                self._exchange(
                    "@RME FILE WRITE_BEGIN path=%s size=%d sha256=%s"
                    % (encoded, size, digest),
                    "file_write_ready",
                )
                offset = 0
                with open(local_path, "rb") as source:
                    while True:
                        block = source.read(FILE_CHUNK_SIZE)
                        if not block:
                            break
                        data = base64.b64encode(block).decode("ascii")
                        records = self._exchange(
                            "@RME FILE WRITE_CHUNK path=%s offset=%d data=%s"
                            % (encoded, offset, data),
                            "file_write_offset",
                        )
                        acknowledged = int(self._terminal(records, "file_write_offset")["offset"])
                        expected = offset + len(block)
                        if acknowledged != expected:
                            raise FileServiceError("Printer returned an invalid upload offset")
                        offset = acknowledged
                        if progress:
                            progress(offset, size)
                if offset != size:
                    raise FileServiceError("Printer did not acknowledge the complete upload")
                self._exchange(
                    "@RME FILE WRITE_END path=%s" % encoded,
                    "file_write_complete",
                    timeout=60,
                )
            except Exception:
                try:
                    self.send_command("@RME FILE ABORT")
                except Exception:
                    pass
                raise

    def mutate(self, action, path, destination=None):
        """Run one acknowledged mkdir/rename/delete/print/flash operation."""
        action = str(action).upper()
        replies = {
            "MKDIR": "file_directory_created", "RENAME": "file_renamed",
            "DELETE": "file_deleted", "PRINT": "file_print_queued",
            "FLASH": "file_flash_queued",
        }
        if action not in replies:
            raise FileServiceError("Unsupported printer USB operation")
        command = "@RME FILE %s path=%s" % (action, normalize_remote_path(path))
        if action == "RENAME":
            command += " dest=%s" % normalize_remote_path(destination)
        with self._operation_lock:
            self._exchange(command, replies[action])

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as source:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
        return size, digest.hexdigest()
