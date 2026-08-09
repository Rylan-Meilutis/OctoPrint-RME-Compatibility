"""Acknowledged M998 firmware transport.

The uploader never opens a serial descriptor. ``send_command`` must enqueue on
OctoPrint's normal printer transport.
"""

import hashlib
import os
import threading
import time

from .protocol import DEFAULT_CHUNK_SIZE, chunk_command


class UploadError(RuntimeError):
    """Expected transfer failure safe to display to an OctoPrint operator."""
    pass


class FirmwareUploader(object):
    """Run the acknowledged M998 transaction on a dedicated worker thread."""
    def __init__(self, send_command, state_changed, logger=None, response_timeout=20):
        self.send_command = send_command
        self.state_changed = state_changed
        self.logger = logger
        self.response_timeout = response_timeout
        self._condition = threading.Condition()
        self._expected_record = None
        self._expected_value = None
        self._record_received = False
        self._ok_received = False
        self._error = None
        self._cancel = threading.Event()
        self._thread = None
        self._busy = False

    @property
    def busy(self):
        with self._condition:
            return self._busy

    def start(self, path, metadata):
        """Start one verified upload, rejecting overlapping transactions."""
        with self._condition:
            if self._busy:
                raise UploadError("A firmware transfer is already active")
            self._busy = True
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(path, dict(metadata)),
            name="rme-firmware-upload",
            daemon=True,
        )
        self._thread.start()

    def cancel(self):
        """Cooperatively interrupt an active transfer at its next wait boundary."""
        self._cancel.set()
        with self._condition:
            self._condition.notify_all()

    def handle_response(self, raw_line, record):
        """Called from the serial receive hook; it only mutates memory."""
        with self._condition:
            if not self._busy or self._expected_record is None:
                return
            if record and record.get("record") == "upload_error":
                self._error = record.get("message", "Firmware rejected the transfer")
            elif record and record.get("record") == self._expected_record:
                if self._expected_value is None or record.get(self._expected_value[0]) == self._expected_value[1]:
                    self._record_received = True
            elif raw_line.strip().lower().startswith("ok") and self._record_received:
                self._ok_received = True
            self._condition.notify_all()

    def _exchange(self, command, expected_record, expected_value=None, timeout=None):
        """Require both the structured acknowledgement and its trailing ``ok``."""
        with self._condition:
            self._expected_record = expected_record
            self._expected_value = expected_value
            self._record_received = False
            self._ok_received = False
            self._error = None
        self.send_command(command)
        deadline = time.monotonic() + (timeout or self.response_timeout)
        with self._condition:
            while not (self._record_received and self._ok_received):
                if self._error:
                    raise UploadError(self._error)
                if self._cancel.is_set():
                    raise UploadError("Transfer cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UploadError("Timed out waiting for %s" % expected_record)
                self._condition.wait(min(remaining, 0.5))
            self._expected_record = None

    def _run(self, path, metadata):
        size = int(metadata["size"])
        digest = metadata["sha256"]
        chunk_size = DEFAULT_CHUNK_SIZE
        try:
            actual_size, actual_digest = hash_file(path)
            if actual_size != size or actual_digest != digest:
                raise UploadError("Firmware file changed after it was selected")
            self.state_changed(
                status="starting", filename=metadata["name"], size=size, sha256=digest,
                offset=0, progress=0, error=None, staged_path=None,
            )
            self._exchange(
                "M998 P0 S%d H%s" % (size, digest),
                "upload_ready",
            )
            with self._condition:
                # Firmware currently advertises 48; retain the protocol ceiling.
                chunk_size = min(DEFAULT_CHUNK_SIZE, chunk_size)

            offset = 0
            with open(path, "rb") as firmware:
                while offset < size:
                    if self._cancel.is_set():
                        raise UploadError("Transfer cancelled")
                    payload = firmware.read(chunk_size)
                    next_offset = offset + len(payload)
                    self._exchange(
                        chunk_command(offset, payload),
                        "upload_offset",
                        ("offset", next_offset),
                    )
                    offset = next_offset
                    self.state_changed(
                        status="uploading",
                        offset=offset,
                        progress=round(offset * 100.0 / size, 2),
                    )

            self.state_changed(status="verifying", offset=size, progress=100)
            self._exchange("M998 P2", "upload_complete", timeout=60)
            self.state_changed(
                status="staged", offset=size, progress=100, staged_path="/usb/FWUPD.BBF"
            )
        except Exception as exc:
            try:
                self.send_command("M998 P3")
            except Exception:
                pass
            if self.logger:
                self.logger.exception("RME firmware transfer failed")
            self.state_changed(status="error", error=str(exc))
        finally:
            with self._condition:
                self._busy = False
                self._expected_record = None
                self._condition.notify_all()


def hash_file(path):
    """Return byte count and SHA-256 while reading large firmware incrementally."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def firmware_metadata(path):
    size, digest = hash_file(path)
    stat = os.stat(path)
    return {
        "name": os.path.basename(path),
        "size": size,
        "sha256": digest,
        "modified": int(stat.st_mtime),
    }
