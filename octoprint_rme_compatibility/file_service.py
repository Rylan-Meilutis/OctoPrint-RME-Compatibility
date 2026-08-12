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
# Firmware accepts a 384-byte decoded Base64 chunk, but OctoPrint can pipeline
# four long commands before their ``ok`` replies. Some CDC/Marlin paths have
# duplicated or truncated those bursts even when each individual command was
# just below 512 characters. Keep each fallback line near 300 characters and
# pace submission; the raw binary transport remains the preferred fast path.
OCTOPRINT_SAFE_BULK_CHUNK_SIZE = 192
BULK_COMMAND_PACING_SECONDS = 0.01
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
        """Cancel an active upload/download at its next response boundary."""
        self._cancel.set()
        with self._condition:
            if self._active:
                self._error = "cancelled"
            self._condition.notify_all()
        try:
            # The active worker performs the raw abort handshake so it can
            # wait for RME_FILE_BINARY_ABORTED before releasing line traffic.
            if not (self._binary_active and self.send_binary):
                self.send_command("@RME FILE ABORT")
        except Exception:
            pass

    def handle_response(self, record):
        """Wake the active HTTP/API worker for relevant parsed file records."""
        if not record or not str(record.get("record", "")).startswith("file_"):
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
            if record["record"] == "file_error":
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
                            code = error.get("code") or error.get("message")
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
        starting=None, cancel_check=None,
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
                if use_binary:
                    try:
                        offset = self._write_binary(
                            local_path, encoded, size, digest, progress
                        )
                    except Exception as exc:
                        if self._cancel.is_set():
                            raise
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
                            offset = self._write_bulk(
                                local_path, encoded, size, digest, progress
                            )
                        else:
                            offset = self._write_legacy(
                                local_path, encoded, size, digest, progress
                            )
                elif bool(int(self._capabilities.get("bulk", 0))):
                    offset = self._write_bulk(local_path, encoded, size, digest, progress)
                else:
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
                elif bool(int(self._capabilities.get("bulk", 0))):
                    self._exchange(
                        "@RME FILE WRITE_BULK_END path=%s" % encoded,
                        ("file_bulk_complete", "file_write_complete"), timeout=60,
                    )
                else:
                    self._exchange(
                        "@RME FILE WRITE_END path=%s" % encoded,
                        "file_write_complete", timeout=60,
                    )
            except Exception:
                try:
                    if self._binary_active and self.send_binary:
                        self._abort_binary_transport()
                    elif not self._binary_mode_uncertain:
                        self.send_command("@RME FILE ABORT")
                except Exception:
                    pass
                raise

    def _abort_binary_transport(self):
        """Return firmware and OctoPrint to line mode after a raw failure."""
        failure = None
        confirmed = False
        try:
            if self._binary_active and self.send_binary:
                self._exchange(
                    self._binary_frame(0xFFFFFFFF, b""),
                    "file_binary_aborted",
                    timeout=10,
                    respect_cancel=False,
                )
                confirmed = True
        except Exception as exc:
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
        negotiated_chunk_size = min(
            65535,
            max(1, int(ready.get("chunk", self._capabilities.get("binary_chunk", 1024)))),
        )
        # The current Buddy endpoint advertises 1024 bytes, but field traces
        # show occasional header/CRC loss at that exact boundary followed by a
        # bogus oversized-length discard. 512-byte payloads retain the raw
        # protocol's throughput advantage while fitting safely inside common
        # CDC packet/FIFO boundaries. Never climb back above a size that has
        # produced a reasoned NACK during this upload.
        chunk_size = min(512, negotiated_chunk_size)
        adaptive_ceiling = chunk_size
        window_size = min(
            64,
            max(1, int(ready.get("window", self._capabilities.get("binary_window", 8)))),
        )
        offset = int(ready.get("offset", 0))
        if self.logger:
            self.logger.info(
                "Using RME binary upload: chunk=%d window=%d", chunk_size, window_size
            )
        retries = 0
        rejected_offset = None
        successful_windows = 0
        with open(local_path, "rb") as source:
            while offset < size:
                source.seek(offset)
                frames = []
                expected_offset = offset
                for _ in range(window_size):
                    payload = source.read(chunk_size)
                    if not payload:
                        break
                    frames.append(self._binary_frame(expected_offset, payload))
                    expected_offset += len(payload)
                records = self._exchange(
                    frames, ("file_binary_ack", "file_binary_nack"),
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
                    reasons = {
                        str(item.get("reason") or "").lower() for item in nacks
                    }
                else:
                    response = responses[-1]
                    acknowledged = int(response.get("offset", -1))
                    reasons = set()
                if not offset <= acknowledged <= expected_offset:
                    raise FileServiceError("Printer returned an invalid binary upload offset")
                if nacks:
                    successful_windows = 0
                    retries += 1
                    reasoned_failure = bool(
                        reasons.intersection(("crc_mismatch", "chunk_too_large"))
                    )
                    repeated_failure = acknowledged == rejected_offset and retries >= 2
                    if (reasoned_failure or repeated_failure) and chunk_size > 256:
                        # Repeating the identical maximum-size frame cannot
                        # recover from a marginal CDC packet boundary. Smaller
                        # raw frames retain the firmware-advertised cumulative
                        # window while reducing each USB write atomically.
                        chunk_size = max(256, chunk_size // 2)
                        adaptive_ceiling = min(adaptive_ceiling, chunk_size)
                        if self.logger:
                            self.logger.warning(
                                "RME binary upload NACK at offset %d (%s); "
                                "reducing and capping raw chunk at %d bytes",
                                acknowledged,
                                ",".join(sorted(reason for reason in reasons if reason))
                                or "unreported",
                                chunk_size,
                            )
                    rejected_offset = acknowledged
                    if retries > 6:
                        raise FileServiceError(
                            "Printer repeatedly rejected binary data at offset %d"
                            % acknowledged
                        )
                    # Frames after the failed one were already handed to the
                    # USB driver and can produce stale NACKs for this same
                    # offset. Let those drain before retransmitting. Firmware
                    # acknowledges only after its advertised full window, so
                    # recovery must retain that negotiated cadence.
                    offset = acknowledged
                    self._wait_for_binary_quiet()
                    continue
                if acknowledged != expected_offset:
                    raise FileServiceError("Printer returned an incomplete binary upload ACK")
                retries = 0
                rejected_offset = None
                offset = acknowledged
                successful_windows += 1
                if chunk_size < adaptive_ceiling and successful_windows >= 16:
                    chunk_size = min(adaptive_ceiling, chunk_size * 2)
                    successful_windows = 0
                    if self.logger:
                        self.logger.info(
                            "RME binary upload stable; increasing raw chunk to %d bytes",
                            chunk_size,
                        )
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
            OCTOPRINT_SAFE_BULK_CHUNK_SIZE,
            max(1, int(ready.get("chunk", self._capabilities.get("bulk_chunk", BULK_CHUNK_SIZE)))),
        )
        window_size = min(
            64,
            max(1, int(ready.get("window", self._capabilities.get("bulk_window", BULK_WINDOW_SIZE)))),
        )
        offset = int(ready.get("offset", 0))
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
                    send_delay=BULK_COMMAND_PACING_SECONDS,
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
