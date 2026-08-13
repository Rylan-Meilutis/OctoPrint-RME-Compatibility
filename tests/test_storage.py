import json
import tempfile
import time
import unittest
from pathlib import Path

from octoprint_rme_compatibility.storage import StateStore, TransferManifestStore


class StorageTests(unittest.TestCase):
    def test_state_store_coalesces_and_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = {"prompt": {"actions": ["Retry"]}}
            path = Path(temporary) / "state.json"
            store = StateStore(str(path), lambda: state)
            store.start()
            store.request_save()
            deadline = time.monotonic() + 2
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            store.stop()
            self.assertEqual(json.loads(path.read_text()), state)
            self.assertEqual(StateStore(str(path), lambda: {}).load(), state)

    def test_transfer_manifest_is_immediately_durable_and_clearable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transfer.json"
            value = {
                "remote_path": "jobs/part.bgcode", "size": 123,
                "sha256": "a" * 64, "transport": "bulk",
            }
            store = TransferManifestStore(str(path))
            store.save(value)
            self.assertEqual(value, json.loads(path.read_text()))
            self.assertEqual(value, TransferManifestStore(str(path)).load())
            store.clear()
            self.assertFalse(path.exists())
            self.assertIsNone(store.get())
