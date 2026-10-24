import types
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility.protocol import parse_line


class PrintControlsTests(unittest.TestCase):
    def test_live_snapshot_replaces_all_print_settings_without_sending_writes(self):
        plugin = self.plugin()
        plugin._schedule_publish = lambda: None
        for fan in (0, 128, 255, 64):
            record = dict(record='tune', fan=fan, dock_fan=100, speed=123,
                stealth=1, auto_pa=2, lcd=0, light=1, screen_print=25,
                chamber_print=50, status_print=75, material_cooling=0,
                print_filter=1, post_filter=0, F0=95, F7=110)
            plugin._handle_record(record)
            for key, value in record.items():
                if key != 'record':
                    self.assertEqual(plugin._state['tune'][key], value)
        self.assertEqual(self.commands, [])
        plugin._handle_record(dict(record='tune', speed=100))
        self.assertNotIn('fan', plugin._state['tune'])

    def test_fan_override_requires_telemetry_and_uses_pwm_not_percent(self):
        for value, pwm in ((0, 0), (25, 64), (50, 128), (100, 255)):
            plugin = self.plugin()
            with self.assertRaises(ValueError):
                plugin._set_print_override(dict(kind='fan', value=value))
            plugin._state['tune']['fan'] = 0
            plugin._set_print_override(dict(kind='fan', value=value))
            self.assertEqual(self.commands[-1], 'M106 S%d' % pwm)

    def test_lost_tune_reply_retries_boundedly_and_live_poll_bypasses_wait(self):
        plugin = self.plugin()
        plugin._state['session']['active'] = True
        plugin._state['machine']['live_controls'] = 1
        plugin._printer.get_state_id = lambda: 'PRINTING'
        priority = []
        plugin._send_priority_service = lambda command, trigger: priority.append((command, trigger))
        for now, count in ((100, 1), (102, 1), (109, 1), (110, 2)):
            with patch('octoprint_rme_compatibility.plugin.time.monotonic', return_value=now):
                plugin._query_tune(periodic=True)
            self.assertEqual(len(priority), count)
        plugin._handle_record = lambda record: None
        self.assertEqual(self.commands, [])

    def test_streamed_chamber_manifest_inspects_all_used_materials(self):
        for types_text, usage, expected in (
            ("PLA;ABS", "100;100", 0),
            ("PLA;ASA", "100;100", 0),
            ("PLA;HIPS", "100;100", 0),
            ("PLA;UNKNOWN", "100;100", 0),
            ("PLA;", "100;100", 0),
            ("PLA;PETG;TPU", "100;100;100", 1),
            ("PLA;ABS", "100;0", 1),
            ("", "", 0),
        ):
            with self.subTest(materials=types_text, usage=usage), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "job.gcode"
                path.write_text("; filament_type = %s\n; filament_used_mm = %s\nG28\n" % (types_text, usage))
                plugin = self.plugin()
                plugin._state["supported"] = True
                plugin._state["tune"]["material_cooling"] = 0
                plugin._printer.get_current_data = lambda: {"job": {"file": {"origin": "local", "path": "job.gcode"}}}
                plugin._file_manager = types.SimpleNamespace(path_on_disk=lambda *args: str(path))
                plugin._prepare_chamber_job()
                expected_command = "@RME CHAMBER JOB allow_cooling=%d" % expected
                self.assertEqual(plugin._chamber_job_command, expected_command)
                result = plugin.gcode_queuing_hook(None, "queuing", "M75", None, "M75", tags={"source:file"})
                self.assertEqual(result, [("M75", None), (expected_command, None)])
                self.assertIsNone(plugin.gcode_queuing_hook(None, "queuing", "G28", None, "G28", tags={"source:file"}))

    def test_chamber_manifest_fifo_and_missing_capability(self):
        plugin = self.plugin()
        plugin._state["supported"] = True
        plugin._chamber_job_command = "@RME CHAMBER JOB allow_cooling=0"
        self.assertIsNone(plugin.gcode_queuing_hook(None, "queuing", "M117 hi", None, "M117", tags={"source:api"}))
        result = plugin.gcode_queuing_hook(None, "queuing", "G28", None, "G28", tags={"source:file"})
        self.assertEqual(result, [("M75", None), ("@RME CHAMBER JOB allow_cooling=0", None), ("G28", None)])
        plugin._prepare_chamber_job()
        self.assertIsNone(plugin._chamber_job_command)

    def test_chamber_controls_require_support_and_reject_invalid_values(self):
        for kind in ("material_cooling", "print_filter", "post_filter"):
            plugin = self.plugin()
            with self.assertRaises(ValueError):
                plugin._set_print_override(dict(kind=kind, value=1))
            self.assertEqual(self.commands, [])
            plugin._state["tune"][kind] = 0
            for value in (2, -1, "1\nM112", True):
                with self.assertRaises(ValueError):
                    plugin._set_print_override(dict(kind=kind, value=value))
            for value in (0, 1):
                plugin._set_print_override(dict(kind=kind, value=value))
                self.assertEqual(self.commands[-2:], ["@RME CHAMBER SET %s=%d" % (kind, value), "@RME TUNE QUERY"])
            self.commands.clear()

    def test_chamber_snapshot_uses_printer_values_without_history_growth(self):
        plugin = self.plugin()
        plugin._schedule_publish = lambda: None
        for value in (0, 1, 0):
            plugin._handle_record(parse_line("RME_TUNE material_cooling=%d print_filter=1 post_filter=0" % value))
            self.assertEqual(plugin._state["tune"]["material_cooling"], value)
            self.assertEqual(plugin._state["tune"]["print_filter"], 1)
            self.assertEqual(plugin._state["tune"]["post_filter"], 0)
        plugin._handle_record(parse_line("RME_TUNE speed=100"))
        self.assertNotIn("material_cooling", plugin._state["tune"])

    def test_temperature_plan_keeps_parent_workflow_through_all_points(self):
        plugin = self.plugin()
        plugin._schedule_publish = lambda: None
        plugin._state.update(connected=True, supported=True)
        command = "M976 A 0:0:HIPS:250/240"
        self.assertIsNone(plugin.gcode_queuing_hook(None, "queuing", command, None, "M976", tags={"source:job"}))
        plugin.gcode_sent_hook(None, "sent", command, None, "M976")
        plugin._observe_pressure_advance_output("PA_CALIBRATION batch accepted entries=1")
        for point, temperature in ((1, 250), (2, 240)):
            plugin._observe_pressure_advance_output(
                "PA_CALIBRATION temperature_point tool=0 slot=0 temperature=%d point=%d points=2" % (temperature, point))
            self.assertTrue(plugin._auto_pa_active)
            self.assertIn("%d°C" % temperature, plugin._state["workflow"]["message"])
            self.assertIn("%d/2" % point, plugin._state["workflow"]["message"])
            plugin._observe_pressure_advance_output("PA_CALIBRATION tool=0 slot=0 result=0.046 max_flow=12 confidence=0.81")
            self.assertTrue(plugin._auto_pa_active)
        plugin._observe_pressure_advance_output("PA_CALIBRATION batch complete entries=1")
        self.assertFalse(plugin._auto_pa_active)
        self.assertIsNone(plugin._state["workflow"])

    def test_operator_chamber_target_is_live_only_with_capability(self):
        plugin = self.plugin()
        plugin._state.update(supported=True)
        plugin._state["machine"]["live_chamber"] = 1
        result = plugin.gcode_queuing_hook(None, "queuing", "M141 S35", None, "M141", tags={"source:api"})
        self.assertEqual(result, (None,))
        self.assertEqual(self.commands, ["@RME TEMPERATURE SET chamber=35"])

    def test_file_chamber_command_keeps_fifo_and_old_firmware_falls_back(self):
        plugin = self.plugin()
        plugin._state.update(supported=True)
        for tags in ({"source:file"}, {"source:api"}):
            self.assertIsNone(plugin.gcode_queuing_hook(None, "queuing", "M141 S35", None, "M141", tags=tags))
        self.assertEqual(self.commands, [])

    def test_live_heaters_only_bypass_for_operator_commands(self):
        plugin = self.plugin()
        plugin._state.update(supported=True)
        plugin._state["machine"]["live_controls"] = 1
        self.assertIsNone(plugin.gcode_queuing_hook(None, "queuing", "M140 S55", None, "M140", tags={"source:file"}))
        self.assertEqual(self.commands, [])
        self.assertEqual(plugin.gcode_queuing_hook(None, "queuing", "M140 S55", None, "M140", tags={"source:api"}), (None,))
        self.assertEqual(self.commands, ["@RME CONTROL SET bed=55"])


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
