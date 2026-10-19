import unittest

from test_toolmap_gate import RmeCompatibilityPlugin


class SnapshotOrderTests(unittest.TestCase):
    def test_snapshots_are_ordered_without_persisting_process_metadata(self):
        plugin = RmeCompatibilityPlugin()
        first = plugin._public_state()
        plugin._state["storage"]["download"] = {"id": "job1", "status": "ready", "progress": 100}
        second = plugin._public_state()
        self.assertEqual(first["_snapshot"]["epoch"], second["_snapshot"]["epoch"])
        self.assertGreater(second["_snapshot"]["revision"], first["_snapshot"]["revision"])
        self.assertEqual(100, second["storage"]["download"]["progress"])
        self.assertNotIn("_snapshot", plugin._persistent_snapshot())

    def test_new_process_has_a_distinct_epoch(self):
        self.assertNotEqual(RmeCompatibilityPlugin()._public_state()["_snapshot"]["epoch"],
                            RmeCompatibilityPlugin()._public_state()["_snapshot"]["epoch"])
