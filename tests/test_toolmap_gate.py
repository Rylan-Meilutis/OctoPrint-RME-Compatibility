import ast
import logging
import os
import sys
import tempfile
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
            "spool_provider": "auto",
            "spoolmanager_enabled": True,
            "default_toolmap": {"0": 0, "1": 1},
            "default_toolmap_enabled": True,
        }

    def get_boolean(self, path):
        return bool(self.values.get(path[0]))

    def get_int(self, path):
        return int(self.values.get(path[0], 0))

    def get(self, path, **kwargs):
        return self.values.get(path[0])

    def global_get(self, path):
        return {"pathSuffix": "path", "nameSuffix": "name"}.get(path[-1])

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

    def is_printing(self):
        return False

    def is_paused(self):
        return False

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
    def test_external_spool_provider_exclusively_disables_internal_backend(self):
        class Provider(object):
            def __init__(self, available):
                self._available = available

            def available(self):
                return self._available

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        spoolmanager = Provider(False)
        spoolman = Provider(True)
        internal = Provider(True)
        plugin._spoolmanager_bridge = spoolmanager
        plugin._spoolman_bridge = spoolman
        plugin._internal_spool_bridge = internal

        plugin._settings.values["spool_provider"] = "spoolmanager"
        provider, name = plugin._resolve_spool_provider()
        self.assertIs(spoolmanager, provider)
        self.assertEqual("spoolmanager", name)

        plugin._settings.values["spool_provider"] = "auto"
        provider, name = plugin._resolve_spool_provider()
        self.assertIs(spoolman, provider)
        self.assertEqual("spoolman", name)

        spoolman._available = False
        provider, name = plugin._resolve_spool_provider()
        self.assertIs(internal, provider)
        self.assertEqual("internal", name)

    def test_external_provider_clears_unselected_builtin_tool_assignment(self):
        record = {
            "database_id": 7, "display_name": "External PETG", "vendor": "",
            "material": "PETG", "color_name": "Blue", "color": "#193a8a",
            "nozzle_temperature": 245, "bed_temperature": 85,
            "remaining_weight": 600, "is_active": True, "is_template": False,
        }

        class Provider(object):
            def available(self):
                return True

            def inventory(self):
                return [dict(record)]

            def selected(self):
                return [dict(record)]

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["spool_provider"] = "spoolmanager"
        plugin._spoolmanager_bridge = Provider()
        plugin._spoolman_bridge = None
        plugin._internal_spool_bridge = None
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-exclusive-provider-test")
        plugin._state.update(
            connected=True,
            supported=True,
            machine={"logical_tools": 2},
        )
        plugin._state["spoolmanager"].update(provider="internal")

        plugin._sync_spoolmanager(True)

        commands = [command for batch in plugin._printer.command_batches
                    for command in (batch if isinstance(batch, list) else [batch])]
        self.assertIn('M865 U0 L0 O"#193a8a"', commands)
        self.assertIn('M865 S"---" L1', commands)

    def test_pending_provider_change_waits_for_confirmation(self):
        record = {
            "database_id": 12, "display_name": "Orange PLA", "vendor": "",
            "material": "PLA", "color_name": "Orange", "color": "#ff7700",
            "nozzle_temperature": 215, "bed_temperature": 60,
            "remaining_weight": 700, "is_active": True, "is_template": False,
        }

        class Provider(object):
            def available(self):
                return True

            def inventory(self):
                return [dict(record)]

            def selected(self):
                return [dict(record)]

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["spool_provider"] = "spoolmanager"
        plugin._spoolmanager_bridge = Provider()
        plugin._spoolman_bridge = None
        plugin._internal_spool_bridge = None
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-provider-confirm-test")
        plugin._state.update(
            connected=True, supported=True, machine={"logical_tools": 1}
        )
        plugin._state["spoolmanager"]["pending_provider_sync"] = {
            "tool": 0, "database_id": 12, "message": "Apply?"
        }

        plugin._sync_spoolmanager(True, True)
        self.assertEqual([], plugin._printer.command_batches)

        plugin._sync_filaments_to_printer()
        commands = [
            command
            for batch in plugin._printer.command_batches
            for command in (batch if isinstance(batch, list) else [batch])
        ]
        self.assertIn('@RME FILAMENT SET slot=0 name=PLA-00C nozzle=215 preheat=175 bed=60 visible=1', commands)
        self.assertIn('M865 U0 L0 O"#ff7700"', commands)

    def test_identical_spoolmanager_read_event_is_a_noop(self):
        plugin = RmeCompatibilityPlugin()
        plugin._active_spool_provider = lambda: (None, "spoolmanager")
        plugin._state["spoolmanager"]["selected"] = [
            {"tool": 4, "database_id": 17, "display_name": "White PLA"}
        ]
        calls = []
        plugin._sync_spoolmanager = lambda *args: calls.append(("sync", args))
        plugin._queue_provider_sync_prompt = lambda *args: calls.append(("prompt", args))

        plugin._handle_spoolmanager_event(
            "plugin_spoolmanager_spool_selected", {"toolId": 4, "databaseId": 17}
        )

        self.assertEqual([], calls)

    def test_connection_imports_printer_before_publishing_provider_assignments(self):
        class EmptyProvider(object):
            def available(self):
                return True

            def inventory(self):
                return []

            def selected(self):
                return []

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["spool_provider"] = "spoolmanager"
        plugin._spoolmanager_bridge = EmptyProvider()
        plugin._spoolman_bridge = None
        plugin._internal_spool_bridge = None
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-connection-import-test")
        plugin._state.update(
            connected=True, supported=True, machine={"logical_tools": 2}
        )

        plugin._initialize_spool_sync()

        self.assertEqual(["M865 Q"], plugin._printer.command_batches)

    def test_terminal_firmware_records_tolerate_absent_prompt(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-none-prompt-test")
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state["prompt"] = None

        plugin._handle_record({
            "record": "event", "seq": 1, "workflow": "firmware_update",
            "state": "completed", "type": "progress", "message": "Done",
        })
        plugin._handle_record({"record": "prompt", "actions": []})

        self.assertIsNone(plugin._state["prompt"])

    def test_one_click_firmware_flash_waits_for_verified_staged_state(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-one-click-flash-test")
        plugin._uploader = types.SimpleNamespace(busy=False)
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state.update(connected=True, supported=True)
        plugin._state["firmware"]["flash_after_stage"] = True

        plugin._firmware_state_changed(status="verifying", progress=100)
        self.assertEqual([], plugin._printer.command_batches)

        plugin._firmware_state_changed(
            status="staged", progress=100, staged_path="/usb/FWUPD.BBF"
        )
        self.assertEqual(["M997 /usb/FWUPD.BBF"], plugin._printer.command_batches)
        self.assertEqual("flashing", plugin._state["firmware"]["status"])
        self.assertFalse(plugin._state["firmware"]["flash_after_stage"])

    def test_update_information_exposes_stable_and_beta_channels(self):
        plugin = RmeCompatibilityPlugin()
        plugin._plugin_version = "0.1.0b10"
        templates = plugin.get_template_configs()
        navbar = next(item for item in templates if item["type"] == "navbar")
        self.assertEqual("rme_compatibility_navbar.jinja2", navbar["template"])
        self.assertEqual("visible: navbarVisible", navbar["data_bind"])
        with open(
            "octoprint_rme_compatibility/templates/rme_compatibility_settings.jinja2"
        ) as template_file:
            settings_template = template_file.read()
        self.assertIn("Plugin status", settings_template)
        self.assertIn("Firmware update", settings_template)
        self.assertIn("Current theme", settings_template)
        self.assertIn("Saved lighting", settings_template)
        self.assertIn("Printer lock", settings_template)
        self.assertIn("Printer USB storage", settings_template)
        self.assertIn("Download", settings_template)
        with open(
            "octoprint_rme_compatibility/static/js/rme_compatibility.js"
        ) as javascript_file:
            javascript = javascript_file.read()
        self.assertIn("OctoPrint.postForm", javascript)
        self.assertIn("formatDurationLong", javascript)
        self.assertIn("formatDistance", javascript)
        self.assertIn("applyPersistentLights", javascript)
        self.assertIn("stageAndFlashFirmware", javascript)
        self.assertIn("storage/download?path=", javascript)
        self.assertIn('"plugin/rme_compatibility/storage/upload"', javascript)
        with open(
            "octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2"
        ) as template_file:
            tab_template = template_file.read()
        self.assertIn("RME printer statistics", tab_template)
        self.assertNotIn("Firmware update", tab_template)
        self.assertNotIn("RME printer controls", tab_template)
        self.assertIn('"plugin/rme_compatibility/firmware"', javascript)
        self.assertNotIn(
            'OctoPrint.postForm(\n                PLUGIN_BASEURL', javascript
        )
        config = plugin.get_update_information()["rme_compatibility"]
        self.assertEqual("main", config["stable_branch"]["branch"])
        self.assertEqual("beta", config["prerelease_branches"][0]["branch"])
        self.assertEqual("python", config["release_compare"])
        self.assertFalse(config["force_base"])
        self.assertEqual("octoprint", config["restart"])
        self.assertIn("{target_version}", config["pip"])
        self.assertEqual([
            ("POST", r"/firmware", 33 * 1024 * 1024),
            ("POST", r"/storage/upload", 1025 * 1024 * 1024),
        ], plugin.bodysize_hook([]))

    def test_firmware_route_accepts_octoprint_spooled_upload(self):
        """Large uploads are rewritten to file.path/file.name by OctoPrint."""
        import flask
        from octoprint.access.permissions import Permissions

        with tempfile.TemporaryDirectory() as source_directory, tempfile.TemporaryDirectory() as firmware_directory:
            source = os.path.join(source_directory, "octoprint-upload.tmp")
            with open(source, "wb") as firmware_file:
                firmware_file.write(b"signed-bbf-test")
            flask.request.files = {}
            flask.request.values = {
                "file.path": source,
                "file.name": "coreone_6.5.7-RME.bbf",
            }
            Permissions.CONTROL = types.SimpleNamespace(can=lambda: True)
            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._logger = logging.getLogger("rme-upload-test")
            plugin._firmware_directory = firmware_directory
            plugin._public_state = lambda: {"supported": True}

            response = plugin.upload_firmware()

            self.assertEqual("coreone_6.5.7-RME.bbf", response["file"]["name"])
            with open(os.path.join(firmware_directory, response["file"]["name"]), "rb") as stored:
                self.assertEqual(b"signed-bbf-test", stored.read())

    def test_package_declares_python_compatibility_before_import(self):
        """OctoPrint's AST preflight must see compatibility in __init__.py."""
        with open("octoprint_rme_compatibility/__init__.py") as package_file:
            package_ast = ast.parse(package_file.read())
        assignments = {
            target.id: ast.literal_eval(node.value)
            for node in package_ast.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(">=3.8,<4", assignments["__plugin_pythoncompat__"])

    def test_rme_atcommand_is_forwarded_to_serial_writer(self):
        """OctoPrint consumes @ commands unless a sending hook forwards them."""
        sent = []
        comm = types.SimpleNamespace(
            _do_send=lambda command, gcode=None: sent.append((command, gcode))
        )
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-atcommand-test")

        plugin.atcommand_sending_hook(
            comm, "sending", "RME", "MACHINE QUERY", tags={"source:api"}
        )
        plugin.atcommand_sending_hook(comm, "sending", "pause", "", tags=set())
        plugin.atcommand_sending_hook(
            comm, "queuing", "RME", "SESSION QUERY", tags=set()
        )

        self.assertEqual([("@RME MACHINE QUERY", None)], sent)

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
