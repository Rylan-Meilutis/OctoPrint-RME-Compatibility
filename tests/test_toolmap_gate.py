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
        PRINT_PAUSED="PrintPaused", PRINT_RESUMED="PrintResumed",
    )
    sys.modules["octoprint.events"] = events


_install_octoprint_stubs()

from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


class _Settings(object):
    def __init__(self):
        self.values = {
            "prompt_toolmap_on_print": True,
            "toolmap_timeout_seconds": 120,
            "stats_poll_interval": 30,
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
        self.forced_commands = []

    def set_job_on_hold(self, value, blocking=True):
        self.holds.append(value)
        return True

    def is_operational(self):
        return True

    def commands(self, commands, tags=None, force=False):
        self.command_batches.append(commands)
        if force:
            self.forced_commands.append((commands, tags))


class _Validator(object):
    def __init__(self):
        self.mapping = None

    def set_tool_mapping(self, mapping):
        self.mapping = mapping


class _Comm(object):
    def __init__(self):
        self.sent = []
        self.continued = 0

    def _use_up_clear(self, gcode):
        return False

    def _do_send(self, command, gcode=None):
        self.sent.append((command, gcode))

    def _continue_sending(self):
        self.continued += 1


class ToolmapGateTests(unittest.TestCase):
    def test_update_information_exposes_stable_and_beta_channels(self):
        plugin = RmeCompatibilityPlugin()
        plugin._plugin_version = "0.1.0.dev2"
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

    def test_sent_tool_uses_remapped_physical_filament_and_color(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-active-tool-test")
        plugin._defer = lambda callback, *args: None
        plugin._state.update(
            supported=True,
            toolmap={"enabled": True, "mapping": {0: 2, 1: 0, 2: 1}},
        )

        plugin.gcode_sent_hook(
            None, "sent", "T1", None, "T", tags={"source:file"}
        )
        plugin._handle_record({
            "record": "loaded_filament",
            "tool": 0,
            "material": "PETG",
            "color_name": "Orange",
            "color": "#ff8000",
        })

        self.assertEqual({
            "logical": 1,
            "physical": 0,
            "material": "PETG",
            "color_name": "Orange",
            "color": "#ff8000",
            "updated": plugin._state["active_tool"]["updated"],
        }, plugin._state["active_tool"])

    def test_filament_report_matches_orca_provider_neutral_shape(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["machine"] = {"logical_tools": 2}
        plugin._state["spoolmanager"].update(
            provider="internal",
            selected=[{
                "tool": 0, "database_id": 4, "display_name": "Orange PLA",
                "material": "PLA", "color": "#ff8000", "color_name": "Orange",
                "vendor": "RME",
            }],
            inventory=[],
            last_sync=123,
        )
        plugin._state["loaded_filaments"] = [{
            "tool": 1, "material": "PETG", "color": "#193a8a",
            "color_name": "Blue",
        }]

        report = plugin._filament_report()

        self.assertEqual("internal", report["provider"])
        self.assertEqual("4", report["data"]["tools"][0]["spool_id"])
        self.assertEqual("PETG", report["data"]["tools"][1]["material"])
        self.assertEqual("RME firmware", report["data"]["tools"][1]["provider"])

    def test_stats_polling_starts_only_after_firmware_support_response(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        queued = []
        plugin._defer = lambda callback, *args: queued.append((callback, args))

        plugin._poll_stats_if_due(True, now=100)
        self.assertEqual([], queued)

        plugin._handle_record({
            "record": "stats", "distance_total_m": 12.5,
            "extruded_m": 4.25, "print_time_s": 900,
        })
        plugin._handle_record({
            "record": "stats", "tool_picks": 6, "mmu_changes": 3,
            "filtering_time_s": 120,
        })
        plugin._handle_record({
            "record": "stats", "crash_x": 1, "power_panics": 2,
        })
        self.assertTrue(plugin._state["stats"]["supported"])
        self.assertEqual(12.5, plugin._state["stats"]["values"]["distance_total_m"])
        self.assertEqual(6, plugin._state["stats"]["values"]["tool_picks"])
        self.assertEqual(3, plugin._state["stats"]["values"]["mmu_changes"])
        self.assertEqual(1, plugin._state["stats"]["values"]["crash_x"])
        queued.clear()
        plugin._last_stats_poll = 100

        plugin._poll_stats_if_due(True, now=129)
        self.assertEqual([], queued)
        plugin._poll_stats_if_due(True, now=130)
        self.assertEqual("@RME STATS QUERY", queued[0][1][0])

        queued.clear()
        plugin._handle_record({"record": "rme_error", "message": "RME_ERROR STATS unsupported"})
        plugin._poll_stats_if_due(True, now=200)
        self.assertEqual(1, len(queued))  # state publication only; no command

    def test_pause_resume_cancel_use_forced_priority_path(self):
        from octoprint.events import Events

        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-priority-control-test")
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state.update(connected=True, supported=True)

        result = plugin.gcode_queuing_hook(
            None, "queuing", "M400", None, "M400", tags={"trigger:pause"}
        )
        self.assertIsNone(result)
        self.assertEqual("M601", plugin._printer.forced_commands[-1][0])

        result = plugin.gcode_queuing_hook(
            None, "queuing", "M601", None, "M601", tags={"source:api"}
        )
        self.assertEqual((None,), result)
        self.assertEqual(1, len(plugin._printer.forced_commands))

        plugin.on_event(Events.PRINT_RESUMED, {})
        plugin.on_event(Events.PRINT_CANCELLING, {})
        self.assertEqual(
            ["M601", "M602", "M604"],
            [item[0] for item in plugin._printer.forced_commands],
        )
        self.assertTrue(all("rme:priority_control" in item[1]
                            for item in plugin._printer.forced_commands))

    def test_priority_controls_are_inert_without_rme_discovery(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state.update(connected=True, supported=False)

        result = plugin.gcode_queuing_hook(
            None, "queuing", "M604", None, "M604", tags={"source:api"}
        )

        self.assertIsNone(result)
        self.assertEqual([], plugin._printer.forced_commands)

    def test_priority_tag_uses_out_of_band_serial_send(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-out-of-band-test")
        plugin._state.update(connected=True, supported=True)
        comm = _Comm()

        result = plugin.gcode_queuing_hook(
            comm, "queuing", "M604", None, "M604",
            tags={"plugin:rme_compatibility", "rme:priority_control"},
        )

        self.assertEqual((None,), result)
        self.assertEqual([("M604", "M604")], comm.sent)
        self.assertEqual(1, comm.continued)

    def test_firmware_completed_pause_does_not_echo_service_command(self):
        from octoprint.events import Events

        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state.update(connected=True, supported=True)

        plugin.action_command_hook(None, "//action:paused", "paused", name="paused")
        plugin.on_event(Events.PRINT_PAUSED, {})

        self.assertEqual([], plugin._printer.forced_commands)


if __name__ == "__main__":
    unittest.main()
