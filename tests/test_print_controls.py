import types
import unittest

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.protocol import parse_line


class PrintControlsTests(unittest.TestCase):
    def plugin(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["machine"].update(tune=1, tool_capacity=8)
        plugin._send_command = self.commands.append
        plugin._send_priority_services = lambda commands, trigger: self.commands.extend(commands)
        plugin._printer = types.SimpleNamespace(is_operational=lambda: True)
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
            self.assertIn(expected, self.commands)

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

    def test_auto_pa_modes_require_firmware_support(self):
        for mode in (0, 1, 2):
            plugin = self.plugin()
            plugin._state["tune"]["auto_pa"] = 1
            plugin._set_print_override(dict(kind="auto_pa", value=mode))
            self.assertIn("M976 W%d" % mode, self.commands)
        self.commands.clear()
        with self.assertRaises(ValueError):
            self.plugin()._set_print_override(dict(kind="auto_pa", value=1))
        plugin = self.plugin()
        plugin._state["tune"]["auto_pa"] = 1
        with self.assertRaises(ValueError):
            plugin._set_print_override(dict(kind="auto_pa", value=3))
        self.assertEqual(self.commands, [])

    def test_auto_pa_snapshot_is_preserved(self):
        plugin = self.plugin()
        plugin._schedule_publish = lambda: None
        plugin._handle_record(parse_line("RME_TUNE speed=100 auto_pa=2"))
        self.assertEqual(plugin._state["tune"]["auto_pa"], 2)

    def test_skipped_and_cached_batches_close_auto_pa_workflow(self):
        for line in ("PA_CALIBRATION skipped mode=off",
                     "PA_CALIBRATION batch cached; no calibration moves"):
            plugin = self.plugin()
            plugin._schedule_publish = lambda: None
            plugin._start_pressure_advance_workflow()
            plugin._observe_pressure_advance_output(line)
            self.assertFalse(plugin._auto_pa_active)
            self.assertIsNone(plugin._state["workflow"])

    def test_independent_lcd_and_single_channel_print_brightness(self):
        for kind, value, expected in [
            ("lcd", 0, "@RME LIGHT LCD value=0"),
            ("lcd", 1, "@RME LIGHT LCD value=1"),
            ("screen", 35, "@RME LIGHT TEMP screen=35"),
            ("chamber", 0, "@RME LIGHT TEMP chamber=0"),
            ("status", 27, "@RME LIGHT TEMP status=27"),
        ]:
            plugin = self.plugin()
            plugin._state["tune"] = dict(lcd=1, screen_print=100, chamber_print=100, status_print=100)
            plugin._print_job_active = lambda: True
            plugin._set_print_override(dict(kind=kind, value=value))
            self.assertIn(expected, self.commands)

    def test_print_light_is_binary_and_rapid_clicks_are_not_motion_throttled(self):
        plugin = self.plugin()
        plugin._print_job_active = lambda: True
        for value in (0, 1, 0, 2):
            plugin._set_print_override(dict(kind="light", value=value))
        self.assertEqual(self.commands, [
            "@RME LIGHT MODE value=0", "@RME TUNE QUERY",
            "@RME LIGHT MODE value=1", "@RME TUNE QUERY",
            "@RME LIGHT MODE value=0", "@RME TUNE QUERY",
            "@RME LIGHT MODE value=1", "@RME TUNE QUERY",
        ])

    def test_new_lighting_controls_require_capability_and_print(self):
        plugin = self.plugin()
        plugin._print_job_active = lambda: False
        for kind in ("lcd", "screen", "chamber", "status"):
            with self.assertRaises(ValueError):
                plugin._set_print_override(dict(kind=kind, value=0))
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
