"""BBF embedded machine/version checks; filename is never compatibility proof."""
import hashlib
import os

from .firmware_support import SUPPORTED
from .uploader import UploadError

IDENTITIES = {
    (7, 1, 0): "coreone", (7, 10, 0): "coreone_indx",
    (8, 1, 0): "coreonel", (3, 1, 0): "xl", (2, 1, 0): "mini",
    (1, 4, 0): "mk4", (1, 3, 5): "mk3.5",
}


def validate(path, expected):
    if not expected:
        raise UploadError("Cannot verify the connected printer; identify its exact firmware variant first")
    with open(path, "rb") as source:
        header = source.read(576)
        if len(header) != 576:
            raise UploadError("Truncated BBF header")
        length = int.from_bytes(header[96:100], "little")
        if not 0 < length <= 32 * 1024 * 1024 - 576 or os.fstat(source.fileno()).st_size < 576 + length:
            raise UploadError("Invalid BBF application length")
        # Printer type is at 111, BBF format at 112 (v2) or printer version (v1).
        variant = IDENTITIES.get((header[111], header[114] if header[112] == 2 else header[112], header[113]))
        version = ".".join(str(v) for v in header[100:103])
        expected_base = "mini" if expected.startswith("mini-en-") else expected
        if variant != expected_base or variant not in SUPPORTED.get(version, set()):
            raise UploadError("BBF %s/%s is not supported for connected %s" % (version, variant or "unknown machine", expected))
        checksum = hashlib.sha256(header[96:])
        application_checksum = hashlib.sha256()
        remaining = length
        while remaining:
            block = source.read(min(65536, remaining))
            if not block:
                raise UploadError("Truncated BBF application")
            checksum.update(block)
            application_checksum.update(block)
            remaining -= len(block)
        if checksum.digest() != header[64:96]:
            raise UploadError("BBF embedded checksum mismatch")
    return dict(version=version, variant=variant, application_size=length,
                application_sha256=application_checksum.hexdigest())
