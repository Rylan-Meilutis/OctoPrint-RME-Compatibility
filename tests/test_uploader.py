import base64
import hashlib
import re
import tempfile
import threading
import unittest
from pathlib import Path

from octoprint_rme_compatibility.protocol import parse_line
from octoprint_rme_compatibility.uploader import FirmwareUploader, firmware_metadata


class FirmwareSimulator(object):
    def __init__(self):
        self.uploader = None
        self.received = bytearray()
        self.expected_size = None
        self.expected_hash = None
        self.commands = []

    def send(self, command):
        self.commands.append(command)
        if command.startswith("M998 _ P0"):
            self.expected_size = int(re.search(r" S(\d+)", command).group(1))
            self.expected_hash = re.search(r" H([0-9a-f]{64})", command).group(1)
            self.reply("FW_UPLOAD READY chunk=48")
        elif command.startswith("M998 _ P1"):
            offset = int(re.search(r" O(\d+)", command).group(1))
            assert offset == len(self.received)
            self.received.extend(base64.b64decode(command.split(" D", 1)[1]))
            self.reply("FW_UPLOAD OFFSET %d" % len(self.received))
        elif command == "M998 _ P2":
            assert len(self.received) == self.expected_size
            assert hashlib.sha256(self.received).hexdigest() == self.expected_hash
            self.reply("FW_UPLOAD COMPLETE /usb/FWUPD.BBF")

    def reply(self, line):
        self.uploader.handle_response(line, parse_line(line))
        self.uploader.handle_response("ok", None)


class UploaderTests(unittest.TestCase):
    def test_acknowledged_upload_round_trip(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        firmware = Path(temporary.name) / "release.bbf"
        content = bytes(range(256)) * 2 + b"signed-tail"
        firmware.write_bytes(content)
        states = []
        finished = threading.Event()

        simulator = FirmwareSimulator()

        def changed(**update):
            states.append(update)
            if update.get("status") in ("staged", "error"):
                finished.set()

        uploader = FirmwareUploader(simulator.send, changed, response_timeout=1)
        simulator.uploader = uploader
        uploader.start(str(firmware), firmware_metadata(str(firmware)))
        self.assertTrue(finished.wait(3))
        self.assertEqual(states[-1]["status"], "staged")
        self.assertEqual(bytes(simulator.received), content)
        self.assertTrue(simulator.commands[0].startswith("M998 _ P0"))
        self.assertEqual(simulator.commands[-1], "M998 _ P2")
        self.assertTrue(all(len(command) <= 96 for command in simulator.commands))
