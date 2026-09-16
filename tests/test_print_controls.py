import types
import unittest

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.protocol import parse_line


class PrintControlsTests(unittest.TestCase):
    def plugin(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["machine"].update(tune=1, tool_capacity=8)
        plugin._send_command = self.commands.append
        return plugin

    def setUp(self):
        self.commands = []

    def test_snapshot_parser(self):
        snapshot = parse_line("RME_TUNE speed=125 stealth=1 light=2 F0=99 F7=101")
        self.assertEqual(snapshot, dict(record="tune", speed=125, stealth=1, light=2, F0=99, F7=101))

    def test_commands_target_physical_flow_and_do_not_require_idle(self):
        for data, expected in [
            ({"kind": "flow", "tool": 7, "value": 101}, "M221 T7 P1 S101"),
            ({"kind": "speed", "value": 120}, "M220 S120"),
            ({"kind": "stealth", "value": 1}, "M9150"),
            ({"kind": "stealth", "value": 0}, "M9140"),
            ({"kind": "light", "value": 2}, "@RME LIGHT MODE value=2"),
        ]:
            self.plugin()._set_print_override(data)
            self.assertEqual(self.commands[-1], expected)

    def test_invalid_controls_never_send(self):
        for data in [
            {"kind": "flow", "tool": 8, "value": 100},
            {"kind": "flow", "tool": 0, "value": 151},
            {"kind": "speed", "value": 0},
            {"kind": "speed", "value": "100\nM112"},
            {"kind": "light", "value": 3},
            {"kind": "stealth", "value": 2},
        ]:
            with self.assertRaises(ValueError):
                self.plugin()._set_print_override(data)
        self.assertEqual(self.commands, [])

    def test_poll_backpressure_and_rate_limit(self):
        plugin = self.plugin()
        for _ in range(10000):
            plugin._query_tune()
        self.assertEqual(self.commands, ["@RME TUNE QUERY"])
        plugin._set_print_override(dict(kind="speed", value=100))
        with self.assertRaises(ValueError):
            plugin._set_print_override(dict(kind="speed", value=101))
        self.assertEqual(len(self.commands), 2)

    def test_transfer_and_lock_guards(self):
        for service in ("_uploader", "_file_service"):
            plugin = self.plugin()
            setattr(plugin, service, types.SimpleNamespace(busy=True))
            with self.assertRaises(RuntimeError):
                plugin._set_print_override(dict(kind="speed", value=100))
        plugin = self.plugin()
        plugin._state["lock"]["locked"] = 1
        with self.assertRaises(ValueError):
            plugin._set_print_override(dict(kind="speed", value=100))
        self.assertEqual(self.commands, [])

    def test_old_firmware_is_not_polled(self):
        plugin = self.plugin()
        plugin._state["machine"].pop("tune")
        plugin._query_tune()
        self.assertEqual(self.commands, [])
        with self.assertRaises(ValueError):
            plugin._set_print_override(dict(kind="speed", value=100))

    def test_received_snapshots_replace_bounded_state(self):
        plugin = self.plugin()
        plugin._schedule_publish = lambda: None
        for i in range(1000):
            plugin._tune_query_pending = True
            plugin._handle_record(dict(record="tune", speed=100, stealth=0,
                                       light=i % 3, F0=100, F7=101, F99=999))
            self.assertFalse(plugin._tune_query_pending)
        self.assertEqual(set(plugin._state["tune"]),
                         {"speed", "stealth", "light", "F0", "F7", "updated"})
