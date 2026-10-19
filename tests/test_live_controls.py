import unittest
from octoprint_rme_compatibility.live_controls import command


class LiveControlsTests(unittest.TestCase):
    def test_live_non_motion_controls(self):
        state = dict(machine=dict(live_controls=1, tool_capacity=8),
                     active_tool=dict(physical=4),
                     toolmap=dict(enabled=True, mapping={"0": 4}))
        for code, expected in [
            ("M104 S240", "nozzle=240 tool=4"),
            ("M104 T0 S240", "nozzle=240 tool=4"),
            ("M140 S55", "bed=55"), ("M106 S128", "fan=128"),
            ("M107", "fan=0"), ("M220 S120", "speed=120"),
            ("M221 T4 P1 S99", "flow=99 tool=4"),
        ]:
            self.assertEqual(command(code, state), "@RME CONTROL SET " + expected)
        for code in ("M104 S999", "M106 P6 S255", "G1 X10", "M104 T8 S240", "M220 S0"):
            self.assertIsNone(command(code, state))
        self.assertIsNone(command("M140 S55", {}))
