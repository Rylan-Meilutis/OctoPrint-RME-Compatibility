import types
import unittest
from unittest.mock import patch

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility import octopod_light


class OctopodLightTests(unittest.TestCase):
    def setUp(self):
        self.backend = RmeCompatibilityPlugin()
        self.backend._state.update(connected=True, machine={"tune": 1}, tune={"light": 1})
        self.commands = []
        self.backend._set_print_override = self.commands.append
        self.bridge = octopod_light.RmeOctopodLight()
        self.bridge._plugin_manager = types.SimpleNamespace(get_plugin_info=lambda *a, **kw: types.SimpleNamespace(implementation=self.backend))
        octopod_light.Permissions.STATUS = types.SimpleNamespace(can=lambda: True)
        octopod_light.Permissions.CONTROL = types.SimpleNamespace(can=lambda: True)

    def request(self, action):
        return self.bridge.on_api_get(types.SimpleNamespace(args={"action": action}))

    def test_discovery_and_actions_use_rme_only(self):
        self.assertEqual(self.bridge.get_settings_defaults()["backend"], "rme_compatibility")
        self.assertEqual(self.request("getState"), {"state": True, "pending": False})
        for action, expected in (("turnOff", 0), ("turnOn", 1), ("toggle", 0)):
            self.assertEqual(self.request(action), {"state": bool(expected), "pending": True})
            self.assertEqual(self.commands[-1], {"kind": "light", "value": expected})

    def test_unknown_offline_and_permissions_never_send(self):
        with patch.object(octopod_light.flask, "abort") as abort:
            self.request("invalid")
            abort.assert_called_with(400)
            self.backend._state["connected"] = False
            self.request("turnOn")
            abort.assert_called_with(503)
            octopod_light.Permissions.CONTROL.can = lambda: False
            self.request("turnOn")
            abort.assert_called_with(403)
        self.assertEqual(self.commands, [])

    def test_real_home_assistant_plugin_is_not_replaced(self):
        with patch.object(octopod_light.importlib.util, "find_spec", return_value=object()):
            self.assertFalse(octopod_light.__plugin_check__())
