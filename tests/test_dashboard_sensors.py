import unittest
from unittest.mock import Mock
from test_toolmap_gate import RmeCompatibilityPlugin, _Settings
from octoprint_rme_compatibility.protocol import parse_line


class DashboardSensors(unittest.TestCase):
    def test_snapshot_replaces_fields_and_preserves_unknown(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._schedule_publish = Mock()
        plugin._handle_record(parse_line('RME_TUNE speed=100 dock_fan=0 dock_rpm=-1 heatbreak_pwm=128 heatbreak_rpm=3000 buddy_mcu=42.5 buddy_board=unknown head_board=33 head_ambient=25.5 chamber_temp=29'))
        tune = plugin._state['tune']
        self.assertEqual(tune['dock_fan'], 0)
        self.assertEqual(tune['dock_rpm'], -1)
        self.assertEqual(tune['heatbreak_rpm'], 3000)
        self.assertEqual(tune['buddy_mcu'], 42.5)
        self.assertEqual(tune['buddy_board'], 'unknown')
        self.assertIn('updated', tune)
        plugin._handle_record(parse_line('RME_TUNE speed=100'))
        self.assertNotIn('buddy_mcu', plugin._state['tune'], 'old firmware/printer cannot retain prior readings')

    def test_optional_sensors_default_off_but_existing_widgets_stay_on(self):
        defaults = RmeCompatibilityPlugin().get_settings_defaults()
        for key in ('dashboard_rme_progress', 'dashboard_rme_bar', 'dashboard_tool_changes'):
            self.assertTrue(defaults[key])
        for key in ('dashboard_dock_fan', 'dashboard_heatbreak_fan', 'dashboard_part_fan_rpm',
                    'dashboard_controller_fan', 'dashboard_heatbreak_temp', 'dashboard_buddy_mcu',
                    'dashboard_buddy_board', 'dashboard_head_mcu', 'dashboard_head_board',
                    'dashboard_head_ambient', 'dashboard_chamber_temp'):
            self.assertFalse(defaults[key])
