"""Serialized client for the RME firmware's sandboxed ``/usb`` service."""

import base64
import hashlib
import os
import struct
import threading
import time
import zlib
from urllib.parse import quote


FILE_CHUNK_SIZE = 48
BULK_CHUNK_SIZE = 384
BULK_WINDOW_SIZE = 4
TRANSFER_LATCH_RETRY_SECONDS = 1.0


class FileServiceError(RuntimeError):
    """A safe, operator-facing USB filesystem failure."""

    def __init__(self, message, record=None):
        super().__init__(message)
        self.record = dict(record) if record else None


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

    def __init__(
        self, send_command, logger=None, response_timeout=20, send_binary=None,
        begin_binary=None, end_binary=None,
    ):
        self.send_command = send_command
        self.send_binary = send_binary
        self.begin_binary = begin_binary
        self.end_binary = end_binary
        self.logger = logger
        self.response_timeout = response_timeout
        self._operation_lock = threading.Lock()
        self._condition = threading.Condition()
        self._expected = set()
        self._records = []
        self._error = None
        self._active = False
        self._cancel = threading.Event()
        self._capabilities = None
        self._binary_active = False
        self._binary_mode_uncertain = False
        self._last_binary_response = 0.0

    @property
    def busy(self):
        if self._operation_lock.locked():
            return True
        with self._condition:
            return self._active

    @property
    def binary_mode_uncertain(self):
        """Whether firmware may still own the raw receiver."""
        return self._binary_mode_uncertain

    def reset(self, reason="Printer disconnected"):
        """Interrupt a waiter when the serial connection disappears."""
        self._capabilities = None
        if self.end_binary:
            self.end_binary()
        self._binary_active = False
        self._binary_mode_uncertain = False
        with self._condition:
            if self._active:
                self._error = reason
            self._condition.notify_all()

    def cancel(self):
        """Suspend an active upload/download at its next response boundary.

        Current firmware retains upload provenance across interruption.  The
        explicit discard workflow performs a matching BEGIN and confirmed
        line-mode ABORT; cancellation must not send an untracked ABORT here.
        """
        self._cancel.set()
        with self._condition:
            if self._active:
                self._error = "cancelled"
            self._condition.notify_all()

    def handle_response(self, record):
        """Wake the active HTTP/API worker for relevant parsed file records."""
        if not record or not (
            str(record.get("record", "")).startswith("file_")
            or record.get("record") in (
                "firmware_status", "firmware_unstaged", "firmware_error",
            )
        ):
            return
        with self._condition:
            if record["record"] in ("file_binary_ack", "file_binary_nack"):
                # A failed pipelined window can produce one NACK for the bad
                # frame and more for frames that were already in flight. Track
                # them even between exchanges so recovery can wait for quiet.
                self._last_binary_response = time.monotonic()
            if not self._active:
                self._condition.notify_all()
                return
            if record["record"] in (
                "file_error", "firmware_error", "file_binary_suspended",
            ):
                self._error = dict(record)
            else:
                self._records.append(dict(record))
            self._condition.notify_all()

    def _wait_for_binary_quiet(self, quiet=0.5, timeout=3.0):
        """Drain responses from a failed in-flight raw window before retrying."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                now = time.monotonic()
                quiet_remaining = quiet - (now - self._last_binary_response)
                if quiet_remaining <= 0:
                    return
                remaining = deadline - now
                if remaining <= 0:
                    return
                self._condition.wait(min(quiet_remaining, remaining))

    def _exchange(
        self, command, expected, timeout=None, terminal=None, respect_cancel=True,
        send_delay=0,
    ):
        """Send a command and return all records through its terminal reply."""
        expected = set(expected if isinstance(expected, (tuple, list, set)) else [expected])
        with self._condition:
            self._expected = expected
            self._records = []
            self._error = None
            self._active = True
        try:
            commands = command if isinstance(command, (tuple, list)) else [command]
            for index, item in enumerate(commands):
                if isinstance(item, bytes):
                    if not self.send_binary:
                        raise FileServiceError("Raw printer transport is unavailable")
                    self.send_binary(item)
                else:
                    self.send_command(item)
                if send_delay and index + 1 < len(commands):
                    time.sleep(send_delay)
            deadline = time.monotonic() + (timeout or self.response_timeout)
            with self._condition:
                while not any(
                    item.get("record") in expected and (terminal is None or terminal(item))
                    for item in self._records
                ):
                    if self._error:
                        error = self._error
                        if isinstance(error, dict):
                            code = (
                                error.get("code") or error.get("reason")
                                or error.get("message") or error.get("record")
                            )
                            raise FileServiceError(
                                "Printer USB operation failed: %s" % code,
                                record=error,
                            )
                        raise FileServiceError("Printer USB operation failed: %s" % error)
                    if respect_cancel and self._cancel.is_set():
                        raise FileServiceError("Printer USB operation cancelled")
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

    def _exchange_when_available(self, command, expected, **kwargs):
        """Wait behind a firmware-owned print or Connect/Link transfer.

        Buddy's shared transfer monitor reports ``transfer_busy`` while
        another producer owns USB storage.  Treat that response, and a remote
        print that OctoPrint did not start, as a held latch rather than a
        failed upload.  The operation lock remains held, so another local RME
        operation cannot overtake this waiter.
        """
        while True:
            try:
                return self._exchange(command, expected, **kwargs)
            except FileServiceError as exc:
                message = str(exc)
                if not (
                    message.endswith("transfer_busy")
                    or message.endswith("printer_busy")
                ):
                    raise
                if self._cancel.is_set():
                    raise FileServiceError("Printer USB operation cancelled")
                if self.logger:
                    self.logger.info(
                        "RME operation paused behind printer activity: %s", message
                    )
                self._cancel.wait(TRANSFER_LATCH_RETRY_SECONDS)

    @staticmethod
    def _terminal(records, record_name):
        return next(item for item in reversed(records) if item.get("record") == record_name)

    def capabilities(self):
        with self._operation_lock:
            self._cancel.clear()
            records = self._exchange("@RME FILE CAPS", "file_caps")
        result = self._terminal(records, "file_caps")
        self._capabilities = dict(result)
        return result

    def list_directory(self, path="/"):
        encoded = normalize_remote_path(path)
        with self._operation_lock:
            self._cancel.clear()
            records = self._exchange(
                "@RME FILE LIST path=%s" % encoded, "file_list_end", timeout=30
            )
        return [item for item in records if item.get("record") == "file_entry"]

    def stat(self, path):
        encoded = normalize_remote_path(path)
        with self._operation_lock:
            self._cancel.clear()
            records = self._exchange("@RME FILE STAT path=%s" % encoded, "file_stat")
        return self._terminal(records, "file_stat")

    def iter_file(self, path):
        """Yield decoded 48-byte blocks while holding the filesystem lease."""
        encoded = normalize_remote_path(path)
        with self._operation_lock:
            self._cancel.clear()
            offset = 0
            while True:
                records = self._exchange_when_available(
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

    def download_file(self, remote_path, local_path, progress=None):
        """Download one remote file atomically into persistent Pi storage.

        The earlier HTTP generator coupled the serial transaction to Flask's
        request context, which OctoPrint's WSGI executor may consume on another
        thread. Staging to a sibling ``.part`` file keeps serial I/O independent
        of HTTP and prevents a failed transfer from exposing partial content.
        """
        metadata = self.stat(remote_path)
        if metadata.get("type") != "file":
            raise FileServiceError("Only files can be downloaded")
        size = max(0, int(metadata.get("size", 0)))
        temporary = local_path + ".part"
        offset = 0
        try:
            with open(temporary, "wb") as destination:
                for block in self.iter_file(remote_path):
                    destination.write(block)
                    offset += len(block)
                    if offset > size:
                        raise FileServiceError(
                            "Printer returned more file data than advertised"
                        )
                    if progress:
                        progress(offset, size)
            if offset != size:
                raise FileServiceError(
                    "Printer download ended at %d of %d bytes" % (offset, size)
                )
            os.replace(temporary, local_path)
            if progress:
                progress(size, size)
            return metadata
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write_file(
        self, local_path, remote_path, progress=None, finalizing=None,
        starting=None, cancel_check=None, manifest_update=None,
        manifest_complete=None,
    ):
        """Upload, hash-check, and atomically publish one local file on USB."""
        encoded = normalize_remote_path(remote_path)
        size, digest = self._hash_file(local_path)
        with self._operation_lock:
            if cancel_check and cancel_check():
                raise FileServiceError("Printer USB operation cancelled before sending")
            self._cancel.clear()
            try:
                if starting:
                    starting()
                if self._capabilities is None:
                    records = self._exchange("@RME FILE CAPS", "file_caps")
                    self._capabilities = dict(self._terminal(records, "file_caps"))
                use_binary = bool(
                    self.send_binary and self.begin_binary and self.end_binary
                    and int(self._capabilities.get("binary", 0))
                )
                use_bulk = False
                if use_binary:
                    if manifest_update:
                        manifest_update("binary", size, digest)
                    binary_resume_attempts = 0
                    binary_failure = None
                    while True:
                        try:
                            offset = self._write_binary(
                                local_path, encoded, size, digest, progress
                            )
                            break
                        except Exception as exc:
                            if self._cancel.is_set():
                                raise
                            suspended = (
                                isinstance(exc, FileServiceError)
                                and exc.record
                                and exc.record.get("record") == "file_binary_suspended"
                                and self._error_code(exc) == "inactivity_timeout"
                            )
                            if not suspended or binary_resume_attempts >= 2:
                                binary_failure = exc
                                break
                            # The SUSPENDED record proves that firmware closed
                            # raw RX, retained the committed prefix, and
                            # restored its line parser. Reopen the same binary
                            # transfer before changing transports: this is both
                            # the fastest recovery and avoids feeding a large
                            # Base64 window through a just-recovered line link.
                            self._release_confirmed_line_mode()
                            binary_resume_attempts += 1
                            if self.logger:
                                self.logger.warning(
                                    "RME binary upload became inactive; resuming "
                                    "the verified prefix in binary mode (attempt %d/2)",
                                    binary_resume_attempts,
                                )
                            continue
                    if binary_failure is not None:
                        exc = binary_failure
                        # Current firmware leaves raw mode before reporting a
                        # structured FILE error and retains the verified prefix.
                        # Otherwise suspend with the raw abort frame and wait for
                        # its confirmation. Do not follow either case with the
                        # line-mode ABORT command: that command deliberately
                        # discards durable resume state in the current protocol.
                        if isinstance(exc, FileServiceError) and exc.record:
                            self._release_confirmed_line_mode()
                        else:
                            self._abort_binary_transport()
                        if self.logger:
                            self.logger.warning(
                                "RME binary upload failed; retrying with text transport: %s",
                                exc,
                            )
                        use_binary = False
                        if bool(int(self._capabilities.get("bulk", 0))):
                            use_bulk = True
                            if manifest_update:
                                manifest_update("bulk", size, digest)
                            try:
                                offset = self._write_bulk(
                                    local_path, encoded, size, digest, progress
                                )
                            except FileServiceError as bulk_exc:
                                if self._error_code(bulk_exc) not in (
                                    "decode_failed", "chunk_too_large",
                                ):
                                    raise
                                # These errors suspend the upload and restore
                                # line mode. Resume its verified prefix with the
                                # one-ACK transport instead of failing the whole
                                # firmware operation after a damaged bulk line.
                                if self.logger:
                                    self.logger.warning(
                                        "RME bulk upload framing failed; resuming "
                                        "with verified text chunks: %s", bulk_exc,
                                    )
                                if manifest_update:
                                    manifest_update("legacy", size, digest)
                                use_bulk = False
                                offset = self._write_legacy(
                                    local_path, encoded, size, digest, progress
                                )
                        else:
                            if manifest_update:
                                manifest_update("legacy", size, digest)
                            offset = self._write_legacy(
                                local_path, encoded, size, digest, progress
                            )
                elif bool(int(self._capabilities.get("bulk", 0))):
                    use_bulk = True
                    if manifest_update:
                        manifest_update("bulk", size, digest)
                    offset = self._write_bulk(local_path, encoded, size, digest, progress)
                else:
                    if manifest_update:
                        manifest_update("legacy", size, digest)
                    offset = self._write_legacy(local_path, encoded, size, digest, progress)
                if offset != size:
                    raise FileServiceError("Printer did not acknowledge the complete upload")
                if finalizing:
                    finalizing()
                if use_binary:
                    # A zero-length frame verifies SHA-256, atomically installs
                    # the file, and restores firmware's line parser.
                    self._exchange(
                        self._binary_frame(size, b""), "file_binary_complete",
                        timeout=60,
                    )
                    # The completion record confirms that firmware has restored
                    # line mode; normal OctoPrint traffic may now resume.
                    self.end_binary()
                    self._binary_active = False
                    self._binary_mode_uncertain = False
                elif use_bulk:
                    self._exchange(
                        "@RME FILE WRITE_BULK_END path=%s" % encoded,
                        ("file_bulk_complete", "file_write_complete"), timeout=60,
                    )
                else:
                    self._exchange(
                        "@RME FILE WRITE_END path=%s" % encoded,
                        "file_write_complete", timeout=60,
                    )
                if manifest_complete:
                    manifest_complete()
            except Exception:
                try:
                    if self._binary_active and self.send_binary:
                        self._abort_binary_transport()
                except Exception:
                    pass
                # Line-mode failures intentionally retain the firmware's
                # durable partial.  A matching BEGIN may resume it after a
                # reconnect; only the explicit discard workflow sends ABORT.
                raise

    def discard_partial(self, remote_path, size, digest):
        """Recover an identified partial and atomically discard its sidecars."""
        encoded = normalize_remote_path(remote_path)
        size = int(size)
        digest = str(digest).lower()
        if size < 0 or len(digest) != 64:
            raise FileServiceError("The saved transfer manifest is invalid")
        with self._operation_lock:
            self._cancel.clear()
            if self._capabilities is None:
                records = self._exchange("@RME FILE CAPS", "file_caps")
                self._capabilities = dict(self._terminal(records, "file_caps"))
            if bool(int(self._capabilities.get("bulk", 0))):
                command = (
                    "@RME FILE WRITE_BULK_BEGIN path=%s size=%d sha256=%s"
                    % (encoded, size, digest)
                )
                expected = "file_bulk_ready"
            else:
                command = (
                    "@RME FILE WRITE_BEGIN path=%s size=%d sha256=%s"
                    % (encoded, size, digest)
                )
                expected = "file_write_ready"
            self._exchange_when_available(command, expected)
            self._exchange(
                "@RME FILE ABORT", "file_aborted", respect_cancel=False,
            )

    def probe_partial(self, remote_path, size, digest):
        """Recover the committed offset, then suspend without discarding it.

        The current firmware always advertises the bounded binary transport.
        Its raw abort is the only operation that releases the shared latch
        while retaining the recovered partial for an operator decision.
        """
        encoded = normalize_remote_path(remote_path)
        size = int(size)
        digest = str(digest).lower()
        if size < 0 or len(digest) != 64:
            raise FileServiceError("The saved transfer manifest is invalid")
        with self._operation_lock:
            self._cancel.clear()
            if self._capabilities is None:
                records = self._exchange("@RME FILE CAPS", "file_caps")
                self._capabilities = dict(self._terminal(records, "file_caps"))
            if not (
                self.send_binary and self.begin_binary and self.end_binary
                and int(self._capabilities.get("binary", 0))
                and int(self._capabilities.get("durable_resume", 0))
            ):
                raise FileServiceError(
                    "Current durable binary recovery is unavailable"
                )
            records = self._exchange_when_available(
                "@RME FILE WRITE_BINARY_BEGIN path=%s size=%d sha256=%s"
                % (encoded, size, digest), "file_binary_ready",
            )
            ready = self._terminal(records, "file_binary_ready")
            offset = int(ready.get("offset", -1))
            if not 0 <= offset <= size:
                raise FileServiceError("Printer returned an invalid resume offset")
            try:
                marker = self.begin_binary()
                self.send_command(marker)
                self._binary_active = True
                self._binary_mode_uncertain = True
                self._abort_binary_transport()
            except Exception:
                if self._binary_active:
                    raise
                self.end_binary()
                raise
            return ready

    def cleanup_orphan(self, remote_path):
        """Delete only mechanically derived partial/meta names supplied by a user."""
        normalized = str(remote_path or "").replace("\\", "/").strip("/")
        if not normalized:
            raise FileServiceError("Enter the original destination path")
        if normalized.lower().endswith((".rme-part", ".rme-meta", ".rme-old")):
            raise FileServiceError("Enter the original final path, not a private sidecar")
        # Validate once before deriving the two firmware-private siblings.
        normalize_remote_path(normalized)
        results = []
        for suffix in (".rme-part", ".rme-meta"):
            candidate = normalized + suffix
            try:
                self.stat(candidate)
            except FileServiceError as exc:
                if not str(exc).endswith("not_found"):
                    raise
                results.append({"path": candidate, "deleted": False})
                continue
            try:
                self.mutate("DELETE", candidate)
                deleted = True
            except FileServiceError as exc:
                if not str(exc).endswith("not_found"):
                    raise
                deleted = False
            results.append({"path": candidate, "deleted": deleted})
        return results

    def _abort_binary_transport(self):
        """Return firmware and OctoPrint to line mode after a raw failure."""
        failure = None
        confirmed = False
        try:
            if self._binary_active and self.send_binary:
                self._exchange(
                    self._binary_frame(0xFFFFFFFF, b""),
                    "file_binary_aborted",
                    timeout=max(
                        12,
                        int(self._capabilities.get("binary_timeout_ms", 10000))
                        / 1000.0 + 2,
                    ),
                    respect_cancel=False,
                )
                confirmed = True
        except Exception as exc:
            # Current Buddy restores the line parser and preserves the durable
            # prefix when its binary inactivity timer expires. That SUSPENDED
            # record is just as authoritative as an explicit ABORTED reply and
            # must not leave OctoPrint permanently recovery-locked.
            if (
                isinstance(exc, FileServiceError)
                and exc.record
                and exc.record.get("record") == "file_binary_suspended"
            ):
                confirmed = True
            else:
                failure = exc
                if self.logger:
                    self.logger.warning("Could not transmit binary abort frame: %s", exc)
        finally:
            try:
                if self.end_binary:
                    self.end_binary()
            except Exception as exc:
                failure = failure or exc
                if self.logger:
                    self.logger.warning("Could not release binary writer: %s", exc)
            self._binary_active = False
            self._binary_mode_uncertain = not (confirmed and failure is None)
        if failure is not None:
            raise FileServiceError(
                "Binary abort was not confirmed; power-cycle or reconnect the "
                "printer before retrying"
            ) from failure

    def _release_confirmed_line_mode(self):
        """Release the raw writer after firmware itself restored line mode."""
        failure = None
        try:
            if self.end_binary:
                self.end_binary()
        except Exception as exc:
            failure = exc
        self._binary_active = False
        self._binary_mode_uncertain = failure is not None
        if failure is not None:
            raise FileServiceError(
                "Binary writer could not be released after the printer "
                "restored line mode; reconnect before retrying"
            ) from failure

    @staticmethod
    def _error_code(error):
        """Return the firmware diagnostic carried by a service exception."""
        record = error.record if isinstance(error, FileServiceError) else None
        record = record or {}
        return str(
            record.get("code") or record.get("reason")
            or record.get("message") or record.get("record") or ""
        )

    @staticmethod
    def _binary_frame(offset, payload):
        """Build one firmware raw frame with a CRC32-protected payload."""
        payload = bytes(payload)
        return struct.pack(
            "<IHI", int(offset), len(payload), zlib.crc32(payload) & 0xFFFFFFFF
        ) + payload

    def _write_binary(self, local_path, encoded, size, digest, progress):
        """Use negotiated raw frames with cumulative ACK recovery."""
        # Do not reserve/block OctoPrint's writer until firmware has granted
        # the shared transfer latch. A Connect/Link owner may keep BEGIN in a
        # retry loop for minutes; arming RAW_SESSION before READY would wedge
        # all ordinary line traffic for that entire wait.
        try:
            records = self._exchange_when_available(
                "@RME FILE WRITE_BINARY_BEGIN path=%s size=%d sha256=%s"
                % (encoded, size, digest),
                "file_binary_ready",
            )
            marker = self.begin_binary()
            self.send_command(marker)
        except Exception:
            self.end_binary()
            raise
        ready = self._terminal(records, "file_binary_ready")
        self._binary_active = True
        self._binary_mode_uncertain = True
        if int(ready.get("header", 10)) != 10:
            raise FileServiceError("Printer returned an unsupported binary header")
        if str(ready.get("endian", "little")) != "little":
            raise FileServiceError("Printer returned an unsupported binary byte order")
        if str(ready.get("crc", "crc32")) != "crc32":
            raise FileServiceError("Printer returned an unsupported binary checksum")
        chunk_size = min(
            65535,
            max(1, int(ready.get("chunk", self._capabilities.get("binary_chunk", 1024)))),
        )
        window_size = min(
            64,
            max(1, int(ready.get("window", self._capabilities.get("binary_window", 8)))),
        )
        offset = int(ready.get("offset", 0))
        if not 0 <= offset <= size:
            raise FileServiceError("Printer returned an invalid resume offset")
        if progress:
            progress(offset, size)
        if self.logger:
            self.logger.info(
                "Using RME binary upload: chunk=%d window=%d", chunk_size, window_size
            )
        retries = 0
        unacknowledged_frames = 0
        with open(local_path, "rb") as source:
            while offset < size:
                source.seek(offset)
                frames = []
                frame_ends = []
                expected_offset = offset
                # Firmware preserves its accepted-frame count across a NACK.
                # Complete only the remainder of that ACK window after a
                # retransmission; sending a fresh full window would make the
                # printer ACK early and then leave both sides waiting.
                frames_until_ack = max(1, window_size - unacknowledged_frames)
                for _ in range(frames_until_ack):
                    payload = source.read(chunk_size)
                    if not payload:
                        break
                    frames.append(self._binary_frame(expected_offset, payload))
                    expected_offset += len(payload)
                    frame_ends.append(expected_offset)
                records = self._exchange(
                    frames, ("file_binary_ack", "file_binary_nack"),
                    # A cumulative ACK from the preceding raw window may be
                    # delivered after this window has already been queued.
                    # It is safe progress information, but it cannot complete
                    # the current exchange. Wait for its target or any NACK.
                    terminal=lambda item, target=expected_offset: (
                        item.get("record") == "file_binary_nack"
                        or int(item.get("offset", -1)) >= target
                    ),
                )
                responses = [
                    item for item in records
                    if item.get("record") in ("file_binary_ack", "file_binary_nack")
                ]
                # A bad frame in a pipelined window is followed by stale
                # offset_mismatch NACKs for frames already in flight. Preserve
                # the first diagnostic reason instead of allowing the last
                # stale response to hide crc_mismatch/chunk_too_large.
                nacks = [item for item in responses if item["record"] == "file_binary_nack"]
                if nacks:
                    response = nacks[0]
                    acknowledged = min(int(item.get("offset", -1)) for item in nacks)
                else:
                    response = responses[-1]
                    acknowledged = int(response.get("offset", -1))
                if not offset <= acknowledged <= expected_offset:
                    raise FileServiceError("Printer returned an invalid binary upload offset")
                if nacks:
                    retries += 1
                    if retries > 6:
                        raise FileServiceError(
                            "Printer repeatedly rejected binary data at offset %d"
                            % acknowledged
                        )
                    committed_frames = sum(
                        1 for frame_end in frame_ends if frame_end <= acknowledged
                    )
                    unacknowledged_frames = min(
                        window_size - 1,
                        unacknowledged_frames + committed_frames,
                    )
                    # Frames after the failed one were already handed to the
                    # USB driver and can produce stale NACKs for this same
                    # offset. Let those drain before sending the remainder of
                    # the firmware's still-open ACK window.
                    offset = acknowledged
                    self._wait_for_binary_quiet()
                    continue
                if acknowledged != expected_offset:
                    raise FileServiceError("Printer returned an invalid binary upload ACK")
                retries = 0
                unacknowledged_frames = 0
                offset = acknowledged
                if progress:
                    progress(offset, size)
        return offset

    def _write_legacy(self, local_path, encoded, size, digest, progress):
        """Use the original one-ACK-per-48-byte transfer for older firmware."""
        records = self._exchange_when_available(
            "@RME FILE WRITE_BEGIN path=%s size=%d sha256=%s" % (encoded, size, digest),
            "file_write_ready",
        )
        ready = self._terminal(records, "file_write_ready")
        offset = int(ready.get("offset", 0))
        if not 0 <= offset <= size:
            raise FileServiceError("Printer returned an invalid resume offset")
        if progress:
            progress(offset, size)
        with open(local_path, "rb") as source:
            source.seek(offset)
            while True:
                if self._cancel.is_set():
                    raise FileServiceError("Printer USB operation cancelled")
                block = source.read(FILE_CHUNK_SIZE)
                if not block:
                    return offset
                data = base64.b64encode(block).decode("ascii")
                records = self._exchange(
                    "@RME FILE WRITE_CHUNK path=%s offset=%d data=%s"
                    % (encoded, offset, data), "file_write_offset",
                )
                acknowledged = int(self._terminal(records, "file_write_offset")["offset"])
                if acknowledged != offset + len(block):
                    raise FileServiceError("Printer returned an invalid upload offset")
                offset = acknowledged
                if progress:
                    progress(offset, size)

    def _write_bulk(self, local_path, encoded, size, digest, progress):
        """Pipeline negotiated Base64 chunks and pace them with cumulative ACKs.

        OctoPrint's supported plugin interface is line-oriented, so raw binary
        framing cannot safely take ownership of its receive parser. Bulk mode
        retains the single OctoPrint serial owner while reducing ACK round trips
        by roughly 32x compared with the legacy transport.
        """
        records = self._exchange_when_available(
            "@RME FILE WRITE_BULK_BEGIN path=%s size=%d sha256=%s"
            % (encoded, size, digest), "file_bulk_ready",
        )
        ready = self._terminal(records, "file_bulk_ready")
        # Honor the negotiated pacing values instead of assuming this
        # firmware release's defaults. Defensive ceilings bound memory and
        # command length if a malformed capability response is received.
        chunk_size = min(
            65535,
            max(1, int(ready.get("chunk", self._capabilities.get("bulk_chunk", BULK_CHUNK_SIZE)))),
        )
        window_size = min(
            64,
            max(1, int(ready.get("window", self._capabilities.get("bulk_window", BULK_WINDOW_SIZE)))),
        )
        offset = int(ready.get("offset", 0))
        if not 0 <= offset <= size:
            raise FileServiceError("Printer returned an invalid resume offset")
        if progress:
            progress(offset, size)
        with open(local_path, "rb") as source:
            source.seek(offset)
            while offset < size:
                if self._cancel.is_set():
                    raise FileServiceError("Printer USB operation cancelled")
                commands = []
                expected_offset = offset
                for _ in range(window_size):
                    block = source.read(chunk_size)
                    if not block:
                        break
                    commands.append(
                        "@RME FILE WRITE_BULK_CHUNK offset=%d data=%s"
                        % (expected_offset, base64.b64encode(block).decode("ascii"))
                    )
                    expected_offset += len(block)
                records = self._exchange(
                    commands, "file_bulk_ack",
                    terminal=lambda item, target=expected_offset: int(item.get("offset", -1)) >= target,
                )
                acknowledged = int(self._terminal(records, "file_bulk_ack")["offset"])
                if acknowledged != expected_offset:
                    raise FileServiceError("Printer returned an invalid bulk upload offset")
                offset = acknowledged
                if progress:
                    progress(offset, size)
        return offset

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
            self._cancel.clear()
            self._exchange_when_available(command, replies[action])

    def firmware_status(self):
        """Return the firmware's authoritative protected-candidate state."""
        with self._operation_lock:
            self._cancel.clear()
            records = self._exchange_when_available(
                "@RME FIRMWARE QUERY", "firmware_status"
            )
        return self._terminal(records, "firmware_status")

    def unstage_firmware(self):
        """Idempotently remove the unarmed protected firmware candidate."""
        with self._operation_lock:
            self._cancel.clear()
            records = self._exchange_when_available(
                "@RME FIRMWARE UNSTAGE", "firmware_unstaged"
            )
        return self._terminal(records, "firmware_unstaged")

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
