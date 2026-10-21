"""Durable host proof for an explicitly requested firmware handoff.

Never infer a flash from a version string or from a matching staged file alone.
The separate receipt is not transient UI state and survives plugin restarts.
"""
import os
import re

from .storage import TransferManifestStore


class FlashReceiptStore(TransferManifestStore):
    def _sync_directory(self):
        descriptor = os.open(os.path.dirname(self.path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def save(self, value):
        super().save(value)
        self._sync_directory()

    def clear(self):
        super().clear()
        self._sync_directory()


def cleanup_matches(receipt, candidate, running, printer_uuid):
    """Fail closed on stale, malformed, different-printer or same-image proof."""
    if not isinstance(receipt, dict) or receipt.get("schema") != 1:
        return False
    digest = lambda value: isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
    if not all(digest(receipt.get(key)) for key in (
            "sha256", "application_sha256", "previous_application_sha256")):
        return False
    # An attempted reflash of the image already running is not evidence of a
    # completed bootloader attempt. Leave that case to explicit operator cleanup.
    if receipt["previous_application_sha256"] == receipt["application_sha256"]:
        return False
    return bool(
        printer_uuid and receipt.get("printer_uuid") == printer_uuid
        and isinstance(receipt.get("model"), str) and receipt["model"]
        and receipt["model"] == running.get("model")
        and candidate.get("candidate") == 1 and candidate.get("armed") == 0
        and candidate.get("state") == "ready"
        and candidate.get("path") == "FWUPD.RME"
        and type(receipt.get("size")) is int and receipt["size"] > 0
        and candidate.get("size") == receipt["size"]
        and candidate.get("sha256") == receipt["sha256"]
        and running.get("algorithm") == "app-sha256-v1"
        and type(receipt.get("application_size")) is int and receipt["application_size"] > 0
        and running.get("size") == receipt["application_size"]
        and running.get("sha256") == receipt["application_sha256"]
    )
