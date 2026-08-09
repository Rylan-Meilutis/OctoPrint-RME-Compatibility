import logging
import sys
import types
import unittest


def _install_octoprint_stubs():
    """Provide only the OctoPrint surface needed to import the plugin class."""
    flask = types.ModuleType("flask")
    flask.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
    flask.abort = lambda status, **kwargs: None
    flask.request = types.SimpleNamespace(files={})
    sys.modules["flask"] = flask
    werkzeug = types.ModuleType("werkzeug")
    werkzeug_utils = types.ModuleType("werkzeug.utils")
    werkzeug_utils.secure_filename = lambda value: str(value).replace("/", "_")
    werkzeug.utils = werkzeug_utils
    sys.modules["werkzeug"] = werkzeug
    sys.modules["werkzeug.utils"] = werkzeug_utils

    plugin_module = types.ModuleType("octoprint.plugin")
    for name in (
        "StartupPlugin", "ShutdownPlugin", "SettingsPlugin", "AssetPlugin",
        "TemplatePlugin", "SimpleApiPlugin", "EventHandlerPlugin",
    ):
        setattr(plugin_module, name, type(name, (), {}))

    class BlueprintPlugin(object):
        @staticmethod
        def route(*args, **kwargs):
            return lambda callback: callback

    plugin_module.BlueprintPlugin = BlueprintPlugin
    octoprint = types.ModuleType("octoprint")
    octoprint.plugin = plugin_module
    sys.modules["octoprint"] = octoprint
    sys.modules["octoprint.plugin"] = plugin_module

    permissions = types.ModuleType("octoprint.access.permissions")
    permissions.Permissions = types.SimpleNamespace()
    access = types.ModuleType("octoprint.access")
    sys.modules["octoprint.access"] = access
    sys.modules["octoprint.access.permissions"] = permissions

    events = types.ModuleType("octoprint.events")
    events.Events = types.SimpleNamespace(
        CONNECTED="Connected", DISCONNECTING="Disconnecting", DISCONNECTED="Disconnected",
        PRINT_STARTED="PrintStarted", PRINT_DONE="PrintDone", PRINT_FAILED="PrintFailed",
        PRINT_CANCELLING="PrintCancelling", PRINT_CANCELLED="PrintCancelled",
    )
    sys.modules["octoprint.events"] = events


_install_octoprint_stubs()

from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


class _Settings(object):
    def __init__(self):
        self.values = {
            "prompt_toolmap_on_print": True,
            "toolmap_timeout_seconds": 120,
            "default_toolmap": {"0": 0, "1": 1},
            "default_toolmap_enabled": True,
        }

    def get_boolean(self, path):
        return bool(self.values.get(path[0]))

    def get_int(self, path):
        return int(self.values.get(path[0], 0))

    def get(self, path, **kwargs):
        return self.values.get(path[0])

    def set(self, path, value):
        self.values[path[0]] = value

    def set_boolean(self, path, value):
        self.values[path[0]] = bool(value)

    def save(self):
        pass


class _Printer(object):
    def __init__(self):
        self.holds = []
        self.command_batches = []

    def set_job_on_hold(self, value, blocking=True):
        self.holds.append(value)
        return True

    def is_operational(self):
        return True

    def commands(self, commands, tags=None):
        self.command_batches.append(commands)


class _Validator(object):
    def __init__(self):
        self.mapping = None

    def set_tool_mapping(self, mapping):
        self.mapping = mapping


class ToolmapGateTests(unittest.TestCase):
    def test_update_information_exposes_stable_and_beta_channels(self):
        plugin = RmeCompatibilityPlugin()
        plugin._plugin_version = "0.1.0.dev1"
        config = plugin.get_update_information()["rme_compatibility"]
        self.assertEqual("main", config["stable_branch"]["branch"])
        self.assertEqual("beta", config["prerelease_branches"][0]["branch"])
        self.assertIn("{target_version}", config["pip"])

    def test_prestart_hold_pauses_timeout_then_configures_validator_before_release(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-toolmap-test")
        plugin._identifier = "rme_compatibility"
        validator = _Validator()
        plugin._plugin_manager = types.SimpleNamespace(
            plugins={"Nozzle_Filament_Validator": types.SimpleNamespace(implementation=validator)},
            send_plugin_message=lambda *args: None,
        )
        plugin._state.update(supported=True, machine={"logical_tools": 2})
        plugin._defer = lambda callback, *args: None

        plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        self.assertEqual([True], plugin._printer.holds)
        self.assertEqual("toolmap", plugin._state["prompt"]["kind"])
        self.assertIsNotNone(plugin._state["prompt"]["deadline"])

        plugin._pause_toolmap_timeout()
        self.assertTrue(plugin._state["prompt"]["timer_paused"])
        self.assertIsNone(plugin._state["prompt"]["deadline"])

        plugin._apply_toolmap({0: 1, 1: 0}, True, release_hold=True)
        self.assertEqual({0: 1, 1: 0}, validator.mapping)
        self.assertEqual([True, False], plugin._printer.holds)
        self.assertIsNone(plugin._state["prompt"])
        self.assertEqual("@RME TOOLMAP SET logical=0 physical=1",
                         plugin._printer.command_batches[0][0])

    def test_untouched_timeout_keeps_current_mapping_without_sending_commands(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-toolmap-timeout-test")
        plugin._identifier = "rme_compatibility"
        validator = _Validator()
        plugin._plugin_manager = types.SimpleNamespace(
            plugins={"Nozzle_Filament_Validator": types.SimpleNamespace(implementation=validator)},
            send_plugin_message=lambda *args: None,
        )
        plugin._state.update(
            supported=True,
            machine={"logical_tools": 2},
            toolmap={"enabled": True, "mapping": {0: 1, 1: 0}},
        )
        plugin._defer = lambda callback, *args: None

        plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
        plugin._expire_toolmap_prompt()
        self.assertEqual({0: 1, 1: 0}, validator.mapping)
        self.assertEqual([], plugin._printer.command_batches)
        self.assertEqual([True, False], plugin._printer.holds)

    def test_cancel_macro_is_skipped_only_before_first_job_command(self):
        plugin = RmeCompatibilityPlugin()
        plugin._preflight_gate_started = True
        plugin.gcode_script_hook(None, "gcode", "afterPrintCancelled")
        result = plugin.gcode_queuing_hook(
            None, "queuing", "G91", None, "G91", tags={"script:afterPrintCancelled"}
        )
        self.assertEqual((None,), result)

        plugin._skip_cancel_script = False
        plugin.gcode_sent_hook(None, "sent", "M110 N0", None, "M110", tags={"source:job"})
        plugin.gcode_script_hook(None, "gcode", "afterPrintCancelled")
        result = plugin.gcode_queuing_hook(
            None, "queuing", "G91", None, "G91", tags={"script:afterPrintCancelled"}
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
