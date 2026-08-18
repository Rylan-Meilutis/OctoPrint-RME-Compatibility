import ast
import hashlib
import importlib.machinery
import logging
import os
import queue
import sys
import tempfile
import threading
import time
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
    octoprint.__spec__ = importlib.machinery.ModuleSpec("octoprint", loader=None)
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
from octoprint_rme_compatibility.file_service import FileServiceError
from octoprint_rme_compatibility.protocol import parse_line
from octoprint_rme_compatibility.storage import TransferManifestStore


class _Settings(object):
    def __init__(self):
        self.values = {
            "prompt_toolmap_on_print": True,
            "toolmap_timeout_seconds": 120,
            "spool_provider": "auto",
            "spoolmanager_enabled": True,
            "default_toolmap": {"0": 0, "1": 1},
            "default_toolmap_enabled": True,
        }

    def get_boolean(self, path):
        return bool(self.values.get(path[0]))

    def get_int(self, path):
        return int(self.values.get(path[0], 0))

    def get_float(self, path):
        return float(self.values.get(path[0], 0))

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
        self.cancel_calls = []
        self.disconnect_calls = 0
        self.connect_calls = 0

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

    def cancel_print(self, tags=None):
        self.cancel_calls.append(tags)

    def disconnect(self):
        self.disconnect_calls += 1

    def connect(self):
        self.connect_calls += 1


class _Validator(object):
    def __init__(self):
        self.mapping = None
        self.mapping_calls = []

    def set_tool_mapping(self, mapping):
        self.mapping = mapping
        self.mapping_calls.append(mapping)


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
    def test_provider_material_labels_map_to_current_firmware_bases(self):
        expected = {
            "PLA": "PLA", "PLA_plus": "PLA", "PETG-CF": "PETG",
            "TPU 95A": "FLEX", "Nylon 12": "PA", "PA6-CF": "PA",
            "Polycarbonate": "PC", "Polypropylene": "PP",
        }
        for material, base in expected.items():
            with self.subTest(material=material):
                self.assertEqual(
                    base,
                    RmeCompatibilityPlugin._firmware_filament_base(material),
                )

    def test_provider_assignments_wait_for_verified_profile_materials(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-provider-profile-barrier-test")
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._schedule_publish = lambda *args: None
        expected = {
            slot: {
                "name": "PET-00L" if slot == 2 else "EMPTY",
                "base": "PETG" if slot == 2 else "none",
                "nozzle": 260 if slot == 2 else 215,
                "preheat": 220 if slot == 2 else 170,
                "bed": 85 if slot == 2 else 60,
                "visible": 1 if slot == 2 else 0,
            }
            for slot in range(8)
        }
        expected[7].update(
            name="NEW", base="PLA", nozzle=215, preheat=170, bed=60,
            visible=1,
        )
        plugin._pending_provider_profile_sync = {
            "expected": expected,
            "assignments": ['M865 U2 L2 O"#000000"', "M865 Q"],
            "signature": ("verified",),
            "retries": 0,
            "mismatches": [],
        }

        for slot in range(8):
            plugin._handle_record(dict(
                record="filament", user=1, slot=slot,
                **{key: value for key, value in expected[slot].items()
                   if key != "visible"}
            ))

        commands = [
            command
            for batch in plugin._printer.command_batches
            for command in (batch if isinstance(batch, list) else [batch])
        ]
        self.assertIn('M865 U2 L2 O"#000000"', commands)
        self.assertEqual(("verified",), plugin._provider_firmware_signature)
        self.assertIsNone(plugin._pending_provider_profile_sync)

    def test_comment_only_continuous_print_control_job_skips_toolmap_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            control_path = os.path.join(directory, "continuousprint_start_print.gcode")
            with open(control_path, "w", encoding="utf-8") as control_file:
                control_file.write("; Continuous Print state transition\n\n(comment only)\n")

            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._printer = _Printer()
            plugin._printer.get_current_data = lambda: {
                "job": {"file": {
                    "origin": "local",
                    "path": "ContinuousPrint/tmp/continuousprint_start_print.gcode",
                }}
            }
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: control_path
            )
            plugin._logger = logging.getLogger("rme-continuous-print-control-test")
            plugin._state.update(supported=True, machine={"logical_tools": 5})

            plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
            plugin.on_event("PrintStarted", {"name": "continuousprint_start_print.gcode"})

            self.assertEqual([], plugin._printer.holds)
            self.assertIsNone(plugin._state["prompt"])

    def test_executable_gcode_still_acquires_toolmap_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            job_path = os.path.join(directory, "real.gcode")
            with open(job_path, "w", encoding="utf-8") as job_file:
                job_file.write("; sliced job\nG28\n")

            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._printer = _Printer()
            plugin._printer.get_current_data = lambda: {
                "job": {"file": {"origin": "local", "path": "real.gcode"}}
            }
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: job_path
            )
            plugin._logger = logging.getLogger("rme-real-print-preflight-test")
            plugin._identifier = "rme_compatibility"
            plugin._plugin_manager = types.SimpleNamespace(
                plugins={}, send_plugin_message=lambda *args: None,
            )
            plugin._state.update(supported=True, machine={"logical_tools": 5})
            plugin._defer = lambda callback, *args: None

            plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")

            self.assertEqual([True], plugin._printer.holds)
            self.assertEqual("toolmap", plugin._state["prompt"]["kind"])

    def test_leading_comment_can_skip_toolmap_hold_for_executable_gcode(self):
        for marker in ("skip-rme-toolmapping", "skip-rme-spoolmapping"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                job_path = os.path.join(directory, "opted-out.gcode")
                with open(job_path, "w", encoding="utf-8") as job_file:
                    job_file.write("; %s\nG28\nT0\n" % marker)

                plugin = RmeCompatibilityPlugin()
                plugin._settings = _Settings()
                plugin._printer = _Printer()
                plugin._printer.get_current_data = lambda: {
                    "job": {"file": {"origin": "local", "path": "opted-out.gcode"}}
                }
                plugin._file_manager = types.SimpleNamespace(
                    path_on_disk=lambda origin, path: job_path
                )
                plugin._logger = logging.getLogger("rme-toolmap-opt-out-test")
                plugin._state.update(supported=True, machine={"logical_tools": 5})

                plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")

                self.assertEqual([], plugin._printer.holds)
                self.assertIsNone(plugin._state["prompt"])

    def test_control_prelude_can_precede_toolmap_skip_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            job_path = os.path.join(directory, "continuous-control.gcode")
            with open(job_path, "w", encoding="utf-8") as job_file:
                job_file.write(
                    "M77\n@pause\n; skip_validation\n; skip-rme-toolmapping\n"
                )

            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._printer = _Printer()
            plugin._printer.get_current_data = lambda: {
                "job": {"file": {"origin": "local", "path": "continuous-control.gcode"}}
            }
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: job_path
            )
            plugin._logger = logging.getLogger("rme-control-prelude-opt-out-test")
            plugin._state.update(supported=True, machine={"logical_tools": 5})

            plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")

            self.assertEqual([], plugin._printer.holds)
            self.assertIsNone(plugin._state["prompt"])

    def test_print_started_reuses_synchronous_skip_decision_after_job_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            job_path = os.path.join(directory, "opted-out.gcode")
            with open(job_path, "w", encoding="utf-8") as job_file:
                job_file.write("; skip-rme-spoolmapping\nG28\n")

            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._printer = _Printer()
            selected_jobs = iter((
                {"job": {"file": {"origin": "local", "path": "opted-out.gcode"}}},
                {"job": {"file": {}}},
            ))
            plugin._printer.get_current_data = lambda: next(selected_jobs)
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: job_path
            )
            plugin._logger = logging.getLogger("rme-toolmap-opt-out-race-test")
            plugin._state.update(supported=True, machine={"logical_tools": 5})

            plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")
            plugin.on_event("PrintStarted", {"name": "opted-out.gcode"})

            self.assertEqual([], plugin._printer.holds)
            self.assertIsNone(plugin._state["prompt"])
            # A second inspection would consume the missing-job value and
            # conservatively reacquire the mapping hold.
            self.assertEqual({"job": {"file": {}}}, next(selected_jobs))

    def test_print_started_without_script_preflight_still_checks_job(self):
        with tempfile.TemporaryDirectory() as directory:
            job_path = os.path.join(directory, "real.gcode")
            with open(job_path, "w", encoding="utf-8") as job_file:
                job_file.write("G28\n")

            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._printer = _Printer()
            plugin._printer.get_current_data = lambda: {
                "job": {"file": {"origin": "local", "path": "real.gcode"}}
            }
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: job_path
            )
            plugin._logger = logging.getLogger("rme-toolmap-event-backstop-test")
            plugin._identifier = "rme_compatibility"
            plugin._plugin_manager = types.SimpleNamespace(
                plugins={}, send_plugin_message=lambda *args: None,
            )
            plugin._state.update(supported=True, machine={"logical_tools": 5})
            plugin._defer = lambda callback, *args: None

            plugin.on_event("PrintStarted", {"name": "real.gcode"})

            self.assertEqual([True], plugin._printer.holds)
            self.assertEqual("toolmap", plugin._state["prompt"]["kind"])

    def test_toolmap_skip_marker_after_first_command_does_not_bypass_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            job_path = os.path.join(directory, "late-marker.gcode")
            with open(job_path, "w", encoding="utf-8") as job_file:
                job_file.write("G28\n; skip-rme-toolmapping\n")

            plugin = RmeCompatibilityPlugin()
            plugin._settings = _Settings()
            plugin._printer = _Printer()
            plugin._printer.get_current_data = lambda: {
                "job": {"file": {"origin": "local", "path": "late-marker.gcode"}}
            }
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: job_path
            )
            plugin._logger = logging.getLogger("rme-late-toolmap-marker-test")
            plugin._identifier = "rme_compatibility"
            plugin._plugin_manager = types.SimpleNamespace(
                plugins={}, send_plugin_message=lambda *args: None,
            )
            plugin._state.update(supported=True, machine={"logical_tools": 5})
            plugin._defer = lambda callback, *args: None

            plugin.gcode_script_hook(None, "gcode", "beforePrintStarted")

            self.assertEqual([True], plugin._printer.holds)
            self.assertEqual("toolmap", plugin._state["prompt"]["kind"])

    def test_toolmap_hold_is_actionable_and_notified_in_frontend(self):
        with open(
            "octoprint_rme_compatibility/static/js/rme_compatibility.js"
        ) as javascript_file:
            javascript = javascript_file.read()
        with open(
            "octoprint_rme_compatibility/templates/rme_compatibility_navbar.jinja2"
        ) as template_file:
            navbar = template_file.read()

        self.assertIn("self.hasToolmapPrompt() ||", javascript)
        self.assertIn('title: "Tool mapping required"', javascript)
        self.assertIn("self.updateToolmapNotice(prompt)", javascript)
        self.assertIn("Use current mapping", navbar)
        self.assertIn("click: showRmeTab", navbar)

    def test_pausing_state_defers_provider_writes_until_job_is_idle(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-pausing-spool-sync-test")
        plugin._printer = _Printer()
        state = {"value": "PAUSING"}
        plugin._printer.get_state_id = lambda: state["value"]
        plugin._spoolmanager = types.SimpleNamespace(
            available=lambda: (_ for _ in ()).throw(
                AssertionError("provider must not be queried while pausing")
            )
        )
        resumed = []
        plugin._defer = lambda callback, *args: resumed.append((callback, args))

        plugin._sync_spoolmanager(True, True)

        self.assertEqual((True, True), plugin._spool_sync_pending)
        self.assertEqual(
            "synchronization deferred until print is idle",
            plugin._state["spoolmanager"]["status"],
        )

        state["value"] = "OPERATIONAL"
        plugin._resume_background_queries()
        self.assertIsNone(plugin._spool_sync_pending)
        self.assertEqual((plugin._sync_spoolmanager, (True, True)), resumed[-1])

    def test_mmu_loadout_expands_early_single_tool_profile_to_five(self):
        class ProfileManager(object):
            def __init__(self):
                self.saved = None

            def get_current_or_default(self):
                return {
                    "volume": {}, "extruder": {},
                    "axes": {"x": {}, "y": {}, "z": {}},
                }

            def save(self, profile, allow_overwrite=False):
                self.saved = profile

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["auto_machine_profile"] = True
        plugin._printer_profile_manager = ProfileManager()
        plugin._logger = logging.getLogger("rme-mmu-profile-test")
        plugin._schedule_publish = lambda: None
        plugin._accept_firmware_spool = lambda record: None
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state["machine"] = {
            "hotends": 1, "logical_tools": 1, "tool_capacity": 5,
            "single_nozzle": 1,
            "x_min": 0, "x_max": 250,
            "y_min": 0, "y_max": 220,
            "z_min": 0, "z_max": 270,
            "feed_x": 500, "feed_y": 500, "feed_z": 30,
        }

        plugin._handle_record({
            "record": "loaded_filament", "tool": 4,
            "material": "PLA", "color_name": "Blue", "color": "#0073ff",
        })

        self.assertEqual(5, plugin._state["machine"]["logical_tools"])
        extruder = plugin._printer_profile_manager.saved["extruder"]
        self.assertEqual(5, extruder["count"])
        self.assertTrue(extruder["sharedNozzle"])
        self.assertEqual([(0, 0)] * 5, extruder["offsets"])

    def test_mmu_machine_count_is_bounded_by_five_slot_capacity(self):
        self.assertEqual(
            8,
            RmeCompatibilityPlugin._normalize_machine_topology({
                "logical_tools": 8, "tool_capacity": 8,
            })["logical_tools"],
        )

        class ProfileManager(object):
            def __init__(self):
                self.saved = None

            def get_current_or_default(self):
                return {
                    "volume": {}, "extruder": {},
                    "axes": {"x": {}, "y": {}, "z": {}},
                }

            def save(self, profile, allow_overwrite=False):
                self.saved = profile

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._printer_profile_manager = ProfileManager()
        plugin._logger = logging.getLogger("rme-mmu-capacity-test")
        plugin._schedule_publish = lambda: None
        plugin._defer = lambda *args: None
        plugin._handle_record({
            "record": "machine", "hotends": 1, "logical_tools": 6,
            "tool_capacity": 5, "single_nozzle": 1,
            "x_min": 0, "x_max": 250, "y_min": 0, "y_max": 220,
            "z_min": 0, "z_max": 270,
            "feed_x": 500, "feed_y": 500, "feed_z": 30,
        })

        self.assertEqual(5, plugin._state["machine"]["logical_tools"])
        plugin._apply_machine_profile()
        extruder = plugin._printer_profile_manager.saved["extruder"]
        self.assertEqual(5, extruder["count"])
        self.assertTrue(extruder["sharedNozzle"])
        self.assertEqual([(0, 0)] * 5, extruder["offsets"])

    def test_print_and_printer_transfer_ownership_are_mutually_exclusive(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-transfer-print-exclusion-test")
        plugin._uploader = types.SimpleNamespace(busy=False)
        plugin._file_service = types.SimpleNamespace(busy=True)
        plugin._release_toolmap_hold = lambda: None

        plugin.on_event("PrintStarted", {})
        plugin.on_event("PrintCancelling", {})

        self.assertEqual([True, False], plugin._printer.holds)
        self.assertEqual(1, len(plugin._printer.cancel_calls))
        self.assertEqual([], plugin._printer.command_batches)

        plugin._printer.is_printing = lambda: True
        plugin._state.update(connected=True, supported=True)
        with self.assertRaisesRegex(RuntimeError, "while a print is active"):
            plugin._require_storage()
        with self.assertRaisesRegex(RuntimeError, "while a print is active"):
            plugin._require_print_idle("Firmware transfers")

    def test_automatic_storage_refreshes_quietly_defer_during_print(self):
        def unexpected_usb_call(*args, **kwargs):
            raise AssertionError("automatic USB reads must not run while printing")

        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._printer.is_printing = lambda: True
        plugin._logger = logging.getLogger("rme-print-storage-deferral-test")
        plugin._state.update(connected=True, supported=True)
        plugin._state["storage"].update(
            supported=True, status="ready", error=None,
        )
        plugin._file_service = types.SimpleNamespace(
            capabilities=unexpected_usb_call,
            list_directory=unexpected_usb_call,
        )

        self.assertFalse(plugin._initialize_storage())
        self.assertFalse(plugin._refresh_storage("/"))
        self.assertFalse(plugin._refresh_native_storage_files())
        self.assertEqual("ready", plugin._state["storage"]["status"])
        self.assertIsNone(plugin._state["storage"]["error"])

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

        # A legacy saved "internal" preference cannot reactivate the local
        # backend while a better external provider is available.
        plugin._settings.values["spool_provider"] = "internal"
        provider, name = plugin._resolve_spool_provider()
        self.assertIs(spoolman, provider)
        self.assertEqual("spoolman", name)

        spoolman._available = False
        provider, name = plugin._resolve_spool_provider()
        self.assertIs(internal, provider)
        self.assertEqual("internal", name)

    def test_external_provider_preserves_unselected_firmware_material(self):
        record = {
            "database_id": 7, "display_name": "External PETG", "vendor": "Atomic Filament",
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
                # Reproduce a stale six-extruder OctoPrint profile. The last
                # empty entry is not a sixth MMU slot and must not reach RME.
                return [dict(record), None, None, None, None, None]

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["spool_provider"] = "spoolmanager"
        plugin._settings.values["spoolmanager_enabled"] = False
        plugin._spoolmanager_bridge = Provider()
        plugin._spoolman_bridge = None
        plugin._internal_spool_bridge = None
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-exclusive-provider-test")
        plugin._state.update(
            connected=True,
            supported=True,
            machine={"logical_tools": 6, "tool_capacity": 5},
        )
        plugin._state["spoolmanager"].update(provider="internal")
        plugin._state["manufacturers"]["profiles"] = [
            {"builtin": 1, "slot": 6, "name": "Atomic Filament"}
        ]

        plugin._sync_spoolmanager(True)

        commands = [command for batch in plugin._printer.command_batches
                    for command in (batch if isinstance(batch, list) else [batch])]
        self.assertNotIn('M865 U0 L0 O"#193a8a"', commands)
        self.assertTrue(commands[-1] == "@RME FILAMENT QUERY")
        pending = plugin._pending_provider_profile_sync
        self.assertIsNotNone(pending)
        actual = {
            slot: dict(profile, user=1, slot=slot)
            for slot, profile in pending["expected"].items()
        }
        pending["mismatches"] = plugin._provider_profile_mismatches(
            pending["expected"], actual
        )
        plugin._complete_provider_profile_sync()
        commands = [command for batch in plugin._printer.command_batches
                    for command in (batch if isinstance(batch, list) else [batch])]
        self.assertIn('M865 U0 L0 O"#193a8a"', commands)
        self.assertFalse(any(' J"' in command for command in commands if command.startswith("M865 U")))
        self.assertIn('M865 V0 O"#193a8a" N"Blue"', commands)
        self.assertTrue(any(
            command.startswith("@RME MANUFACTURER ASSIGN tool=0 name=Atomic%20Filament tx=")
            for command in commands
        ))
        self.assertFalse(any('M865 S"---"' in command for command in commands))
        self.assertTrue(any(
            command.startswith("@RME MANUFACTURER ASSIGN tool=1 name=none tx=")
            for command in commands
        ))
        self.assertFalse(any("tool=5" in command for command in commands))
        self.assertTrue(any(
            command.startswith(
                "@RME FILAMENT SET slot=0 name=PET-007 material=PETG base=PETG "
            )
            for command in commands
        ))
        self.assertTrue(any(
            command.startswith(
                "@RME FILAMENT ASSIGN tool=0 profile=PET-007 material=PETG tx="
            )
            for command in commands
        ))

        # A manufacturer-query completion reconciles with force=False. Once
        # this exact snapshot has been published, it must not enqueue the
        # colors, presets, assignments, or another M865 query again.
        batch_count = len(plugin._printer.command_batches)
        plugin._sync_spoolmanager(False, True)
        self.assertEqual(batch_count, len(plugin._printer.command_batches))

    def test_spoolmanager_mapping_lists_all_spools_without_publishing_unavailable(self):
        active = {
            "database_id": 7, "display_name": "Active PETG", "vendor": "Vendor",
            "material": "PETG", "color_name": "Blue", "color": "#193a8a",
            "nozzle_temperature": 245, "bed_temperature": 85,
            "remaining_weight": 600, "is_active": True, "is_template": False,
        }
        empty = dict(active, database_id=8, display_name="Empty PETG", remaining_weight=0)
        template = dict(
            active, database_id=9, display_name="Grey Blue Pla",
            material="PLA", is_template=True,
        )

        class Provider(object):
            def available(self):
                return True

            def inventory(self, include_unavailable=False):
                if include_unavailable:
                    return [dict(active), dict(empty), dict(template)]
                return [dict(active), dict(template)]

            def selected(self):
                return []

        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["spool_provider"] = "spoolmanager"
        plugin._spoolmanager_bridge = Provider()
        plugin._spoolman_bridge = None
        plugin._internal_spool_bridge = None
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-full-mapping-inventory-test")
        plugin._state.update(connected=False, supported=True, machine={"logical_tools": 1})

        plugin._sync_spoolmanager(True, False)

        self.assertEqual(
            [7, 8, 9],
            [item["database_id"] for item in plugin._state["spoolmanager"]["inventory"]],
        )
        self.assertEqual(
            [7, 9],
            [item["database_id"] for item in plugin._state["spoolmanager"]["published"]],
        )

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
        self.assertIsNone(
            plugin._state["spoolmanager"]["pending_provider_sync"]
        )
        commands = [
            command
            for batch in plugin._printer.command_batches
            for command in (batch if isinstance(batch, list) else [batch])
        ]
        self.assertTrue(any(
            command.startswith('@RME FILAMENT SET slot=0 name=PLA-00C material=PLA base=PLA nozzle=215 preheat=175 bed=60 visible=1 tx=')
            for command in commands
        ))
        self.assertNotIn('M865 U0 L0 O"#ff7700"', commands)
        pending = plugin._pending_provider_profile_sync
        actual = {
            slot: dict(profile, user=1, slot=slot)
            for slot, profile in pending["expected"].items()
        }
        pending["mismatches"] = plugin._provider_profile_mismatches(
            pending["expected"], actual
        )
        plugin._complete_provider_profile_sync()
        commands = [
            command
            for batch in plugin._printer.command_batches
            for command in (batch if isinstance(batch, list) else [batch])
        ]
        self.assertIn('M865 U0 L0 O"#ff7700"', commands)
        self.assertTrue(any(
            command.startswith(
                "@RME FILAMENT ASSIGN tool=0 profile=PLA-00C material=PLA tx="
            )
            for command in commands
        ))
        self.assertFalse(any(' J"' in command for command in commands if command.startswith("M865 U")))

    def test_confirmed_provider_change_stays_cleared_when_print_defers_apply(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-provider-deferred-confirm-test")
        plugin._state["spoolmanager"]["pending_provider_sync"] = {
            "tool": 2, "database_id": 12, "message": "Apply?",
        }
        plugin._provider_firmware_signature = ("stale",)
        plugin._print_job_active = lambda: True
        persisted = []
        plugin._persist_and_publish = lambda: persisted.append(True)

        plugin._sync_filaments_to_printer()

        self.assertIsNone(
            plugin._state["spoolmanager"]["pending_provider_sync"]
        )
        self.assertIsNone(plugin._provider_firmware_signature)
        self.assertEqual([True], persisted)
        self.assertEqual((True, True), plugin._spool_sync_pending)

    def test_provider_prompt_accumulates_changes_for_multiple_tools(self):
        plugin = RmeCompatibilityPlugin()
        plugin._persist_and_publish = lambda: None
        plugin._state["spoolmanager"]["selected"] = [
            {"tool": 0, "database_id": 10, "display_name": "Grey PLA"},
            {"tool": 2, "database_id": 12, "display_name": "Black PETG"},
        ]

        plugin._queue_provider_sync_prompt(0, 10)
        plugin._queue_provider_sync_prompt(2, 12)

        pending = plugin._state["spoolmanager"]["pending_provider_sync"]
        self.assertEqual([0, 2], [entry["tool"] for entry in pending["changes"]])
        self.assertIn("T0 to Grey PLA", pending["message"])
        self.assertIn("T2 to Black PETG", pending["message"])
        self.assertIn("Apply all 2 changes", pending["message"])

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

    def test_structured_session_suppresses_legacy_progress_notifications(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._schedule_publish = lambda: None
        plugin._state.update(supported=True)
        plugin._state["session"]["active"] = True

        self.assertIsNone(plugin.gcode_received_hook(
            None, "//action:notification Heating hotend 15%"
        ))
        self.assertEqual("heating", plugin._state["workflow"]["workflow"])
        self.assertEqual(15, plugin._state["workflow"]["progress"])
        self.assertEqual("active", plugin._state["workflow"]["state"])
        self.assertGreater(plugin._state["workflow"]["legacy_expires_at"], 0)

        plugin._state["workflow"] = {
            "record": "event", "seq": 9, "workflow": "probing",
            "state": "active", "message": "Structured probe",
            "received_at": int(time.time()),
        }
        self.assertIsNone(plugin.gcode_received_hook(
            None, "//action:notification Heating hotend 16%"
        ))
        self.assertEqual("probing", plugin._state["workflow"]["workflow"])
        self.assertEqual(
            "//action:pause",
            plugin.gcode_received_hook(None, "//action:pause"),
        )

        plugin._settings.values["legacy_notifications"] = True
        self.assertEqual(
            "//action:notification Heating hotend 20%",
            plugin.gcode_received_hook(
                None, "//action:notification Heating hotend 20%"
            ),
        )

        plugin._settings.values["legacy_notifications"] = False
        plugin._state["session"]["active"] = False
        plugin._state["workflow"] = None
        for message in (
            "Homing",
            "Heating hotend 25%",
            "Heating bed 92%",
            "Heat soak 40%",
        ):
            self.assertIsNone(plugin.gcode_received_hook(
                None, "//action:notification " + message
            ))
            self.assertEqual(message, plugin._state["workflow"]["message"])
            plugin._state["workflow"] = None

        plugin._state["supported"] = False
        self.assertEqual(
            "//action:notification Heating bed 97%",
            plugin.gcode_received_hook(
                None, "//action:notification Heating bed 97%"
            ),
        )

    def test_shared_nozzle_no_tool_sentinel_does_not_invalidate_t0(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._state.update(supported=True)
        plugin._state["machine"].update(single_nozzle=1, logical_tools=5)

        self.assertIsNone(plugin.gcode_received_hook(
            None, "echo: Invalid extruder -1"
        ))
        self.assertEqual(
            "echo: Invalid extruder 3",
            plugin.gcode_received_hook(None, "echo: Invalid extruder 3"),
        )

        plugin._state["machine"]["single_nozzle"] = 0
        self.assertEqual(
            "echo: Invalid extruder -1",
            plugin.gcode_received_hook(None, "echo: Invalid extruder -1"),
        )

    def test_one_click_firmware_flash_waits_for_verified_staged_state(self):
        mutations = []
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-one-click-flash-test")
        plugin._uploader = types.SimpleNamespace(busy=False)
        plugin._file_service = types.SimpleNamespace(
            mutate=lambda action, path: mutations.append((action, path))
        )
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state.update(connected=True, supported=True)
        plugin._state["firmware"]["flash_after_stage"] = True

        plugin._firmware_state_changed(status="verifying", progress=100)
        self.assertEqual([], plugin._printer.command_batches)

        plugin._firmware_state_changed(
            status="ready", progress=100, staged_path="/usb/FWUPD.BBF"
        )
        self.assertEqual([("FLASH", "FWUPD.RME")], mutations)
        self.assertEqual([], plugin._printer.command_batches)
        self.assertEqual("flash_queued", plugin._state["firmware"]["status"])
        self.assertFalse(plugin._state["firmware"]["flash_after_stage"])

    def test_current_firmware_stages_bbf_through_file_service(self):
        class FileService(object):
            # A Settings-page directory refresh may still be winding down when
            # the user starts firmware staging. The real service serializes the
            # upload behind it, so this must not fail preflight with HTTP 409.
            busy = True

            def __init__(self):
                self.upload = None
                self.status = None
                self.status_before_start = None

            def write_file(
                self, local_path, remote_path, progress=None, finalizing=None,
                starting=None, cancel_check=None,
            ):
                self.upload = (local_path, remote_path)
                self.status_before_start = self.status()
                starting()
                size = os.path.getsize(local_path)
                progress(size, size)
                finalizing()

            def firmware_status(self):
                return {
                    "candidate": 1,
                    "armed": 0,
                    "state": "ready",
                    "path": "FWUPD.RME",
                    "size": os.path.getsize(self.upload[0]),
                    "sha256": hashlib.sha256(b"signed-rme-bbf").hexdigest(),
                }

        with tempfile.TemporaryDirectory() as firmware_directory:
            filename = "coreone-rme.bbf"
            path = os.path.join(firmware_directory, filename)
            with open(path, "wb") as firmware_file:
                firmware_file.write(b"signed-rme-bbf")

            plugin = RmeCompatibilityPlugin()
            plugin._printer = _Printer()
            plugin._logger = logging.getLogger("rme-file-firmware-test")
            plugin._uploader = types.SimpleNamespace(busy=False)
            plugin._file_service = FileService()
            plugin._file_service.status = lambda: plugin._state["firmware"]["status"]
            plugin._firmware_directory = firmware_directory
            plugin._persist_and_publish = lambda: None
            plugin._defer = lambda callback, *args: None
            plugin._state.update(connected=True, supported=True)
            plugin._state["storage"].update(
                supported=True, caps={"write": 1, "flash": 1}
            )

            plugin._start_firmware_upload(filename)
            plugin._firmware_file_thread.join(timeout=2)

            self.assertEqual((path, "FWUPD.BBF"), plugin._file_service.upload)
            self.assertEqual("queued", plugin._file_service.status_before_start)
            self.assertEqual("ready", plugin._state["firmware"]["status"])
            self.assertEqual("/usb/FWUPD.RME", plugin._state["firmware"]["staged_path"])

    def test_interrupted_upload_retains_source_and_resumes_from_durable_manifest(self):
        class FileService(object):
            binary_mode_uncertain = False

            def __init__(self):
                self.calls = []

            def write_file(
                self, local_path, remote_path, manifest_update=None,
                manifest_complete=None, **kwargs
            ):
                self.calls.append((local_path, remote_path))
                with open(local_path, "rb") as source:
                    payload = source.read()
                manifest_update(
                    "bulk", len(payload), hashlib.sha256(payload).hexdigest()
                )
                if len(self.calls) == 1:
                    raise FileServiceError("Printer disconnected")
                manifest_complete()

        with tempfile.TemporaryDirectory() as directory:
            original = os.path.join(directory, "job.bgcode")
            with open(original, "wb") as source:
                source.write(b"durable upload bytes")
            transfer_directory = os.path.join(directory, "retained")
            os.makedirs(transfer_directory)

            plugin = RmeCompatibilityPlugin()
            plugin._logger = logging.getLogger("rme-manifest-test")
            plugin._file_service = FileService()
            plugin._transfer_directory = transfer_directory
            plugin._manifest_store = TransferManifestStore(
                os.path.join(directory, "manifest.json")
            )
            plugin._manifest_store.load()
            plugin._persist_and_publish = lambda: None

            with self.assertRaisesRegex(FileServiceError, "disconnected"):
                plugin._write_file_with_manifest(
                    original, "jobs/job.bgcode", kind="file"
                )
            manifest = plugin._manifest_store.get()
            self.assertIsNotNone(manifest)
            self.assertTrue(os.path.isfile(manifest["source_path"]))
            self.assertEqual("jobs/job.bgcode", manifest["remote_path"])

            plugin._write_file_with_manifest(
                manifest["source_path"], manifest["remote_path"],
                kind="file", resume_manifest=manifest,
            )
            self.assertIsNone(plugin._manifest_store.get())
            self.assertFalse(os.path.exists(manifest["source_path"]))
            self.assertEqual(
                plugin._file_service.calls[0][0], plugin._file_service.calls[1][0]
            )

    def test_stale_partial_status_does_not_claim_recovery_is_active(self):
        partial = RmeCompatibilityPlugin._public_manifest(
            {
                "source_path": "/missing/retained-file",
                "source_name": "job.bgcode",
                "remote_path": "jobs/job.bgcode",
                "kind": "file",
                "size": 123,
                "sha256": "0" * 64,
                "transport": "bulk",
                "offset": 48,
                "status": "transferring",
            }
        )

        self.assertEqual("transferring", partial["status"])
        self.assertFalse(partial["recovery_active"])

    def test_discard_replaces_a_stuck_partial_resume_worker(self):
        class FileService(object):
            binary_mode_uncertain = False

            def __init__(self):
                self.resume_started = threading.Event()
                self.release_resume = threading.Event()
                self.cancel_calls = 0
                self.discard_calls = []

            def write_file(self, *args, **kwargs):
                self.resume_started.set()
                self.release_resume.wait(timeout=5)
                raise FileServiceError("Printer USB operation cancelled")

            def cancel(self):
                self.cancel_calls += 1
                self.release_resume.set()

            def discard_partial(self, remote_path, size, digest):
                self.discard_calls.append((remote_path, size, digest))

        with tempfile.TemporaryDirectory() as directory:
            retained = os.path.join(directory, "retained.bgcode")
            payload = b"interrupted upload"
            with open(retained, "wb") as source:
                source.write(payload)
            digest = hashlib.sha256(payload).hexdigest()
            manifest_store = TransferManifestStore(
                os.path.join(directory, "manifest.json")
            )
            manifest_store.load()
            manifest_store.save(
                {
                    "source_path": retained,
                    "source_name": "job.bgcode",
                    "remote_path": "jobs/job.bgcode",
                    "kind": "file",
                    "size": len(payload),
                    "sha256": digest,
                    "transport": "bulk",
                    "offset": 0,
                    "status": "interrupted",
                }
            )

            plugin = RmeCompatibilityPlugin()
            plugin._printer = _Printer()
            plugin._logger = logging.getLogger("rme-partial-replace-test")
            plugin._file_service = FileService()
            plugin._manifest_store = manifest_store
            plugin._persist_and_publish = lambda: None
            plugin._defer = lambda callback, *args: None
            plugin._state.update(connected=True, supported=True)

            plugin._resume_partial_transfer()
            self.assertTrue(plugin._file_service.resume_started.wait(timeout=1))

            plugin._discard_partial_transfer()
            with plugin._partial_thread_lock:
                discard_worker = plugin._partial_thread
            discard_worker.join(timeout=2)

            self.assertFalse(discard_worker.is_alive())
            self.assertEqual(1, plugin._file_service.cancel_calls)
            self.assertEqual(
                [("jobs/job.bgcode", len(payload), digest)],
                plugin._file_service.discard_calls,
            )
            self.assertIsNone(plugin._manifest_store.get())
            self.assertIsNone(plugin._state["storage"]["partial"])
            self.assertFalse(os.path.exists(retained))

    def test_current_firmware_flashes_through_file_service(self):
        class FileService(object):
            def __init__(self):
                self.mutations = []

            def mutate(self, action, path):
                self.mutations.append((action, path))

        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._file_service = FileService()
        plugin._persist_and_publish = lambda: None
        plugin._state["firmware"]["status"] = "ready"
        plugin._state["storage"].update(
            supported=True, caps={"write": 1, "flash": 1}
        )

        plugin._flash_firmware()

        self.assertEqual([("FLASH", "FWUPD.RME")], plugin._file_service.mutations)
        self.assertEqual([], plugin._printer.command_batches)
        self.assertEqual("flash_queued", plugin._state["firmware"]["status"])

    def test_firmware_flash_uses_bounded_update_only_reconnect(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-firmware-reconnect-test")
        plugin._file_service = types.SimpleNamespace(mutate=lambda action, path: None)
        plugin._persist_and_publish = lambda: None
        plugin._publish = lambda: None
        plugin._defer = lambda callback, *args: callback(*args)
        scheduled = []
        plugin._schedule_firmware_reconnect = (
            lambda delay: scheduled.append(delay) or True
        )
        plugin._state.update(connected=True, supported=True)
        plugin._state["firmware"]["status"] = "ready"

        plugin._flash_firmware()
        self.assertTrue(plugin._firmware_reconnect_pending())
        plugin._handle_record(parse_line("RME_FIRMWARE_RESTART reconnect=1"))

        self.assertEqual(1, plugin._printer.disconnect_calls)
        self.assertEqual([2.0], scheduled)
        self.assertEqual("restarting", plugin._state["firmware"]["status"])

        plugin._state["connected"] = False
        scheduled[:] = []
        plugin._attempt_firmware_reconnect()
        self.assertEqual(1, plugin._printer.connect_calls)
        self.assertEqual([15.0], scheduled)

        # If CONNECTED never arrives, the next watchdog pass must tear down
        # OctoPrint's stuck Connecting state before scheduling another try.
        scheduled[:] = []
        plugin._attempt_firmware_reconnect()
        self.assertEqual(2, plugin._printer.disconnect_calls)
        self.assertEqual(1, plugin._printer.connect_calls)
        self.assertEqual([2.0], scheduled)

        self.assertTrue(plugin._complete_firmware_reconnect())
        self.assertFalse(plugin._firmware_reconnect_pending())
        self.assertEqual("reconnected", plugin._state["firmware"]["status"])

        ordinary_disconnect = RmeCompatibilityPlugin()
        ordinary_disconnect._printer = _Printer()
        ordinary_disconnect._logger = logging.getLogger(
            "rme-ordinary-disconnect-test"
        )
        self.assertFalse(ordinary_disconnect._attempt_firmware_reconnect())
        self.assertEqual(0, ordinary_disconnect._printer.connect_calls)

    def test_sd_upload_hook_uses_verified_rme_file_transfer(self):
        class FileService(object):
            def __init__(self):
                self.upload = None

            def write_file(self, path, remote_name, progress=None):
                self.upload = (path, remote_name)
                progress(12, 12)

            def list_directory(self, path):
                return []

        completed = threading.Event()
        callbacks = []
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-sd-upload-test")
        plugin._file_service = FileService()
        plugin._persist_and_publish = lambda: None
        plugin._state.update(connected=True, supported=True)
        plugin._state["storage"].update(supported=True, caps={"write": 1})
        printer = types.SimpleNamespace(
            _get_free_remote_name=lambda filename: "jobs/remote.gcode"
        )

        remote = plugin.sd_card_upload_hook(
            printer, "local.gcode", "/tmp/local.gcode",
            lambda local, target: callbacks.append(("start", local, target)),
            lambda local, target, elapsed: (
                callbacks.append(("success", local, target)), completed.set()
            ),
            lambda local, target, elapsed: completed.set(),
        )

        self.assertEqual("jobs/remote.gcode", remote)
        self.assertTrue(completed.wait(2))
        self.assertEqual(
            ("/tmp/local.gcode", "jobs/remote.gcode"), plugin._file_service.upload
        )
        self.assertEqual("start", callbacks[0][0])
        self.assertEqual("success", callbacks[-1][0])

    def test_firmware_errors_are_not_persisted(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state["firmware"].update(
            status="error", filename="old.bbf", error="old transfer failed"
        )

        persisted = plugin._persistent_snapshot()["firmware"]

        self.assertEqual("idle", persisted["status"])
        self.assertIsNone(persisted["error"])

    def test_uncertain_binary_transport_unlocks_after_line_mode_reconnect(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-reboot-lock-test")
        plugin._publish = lambda: None
        plugin._persist_and_publish = lambda: None
        plugin._defer = lambda callback, *args: None
        plugin._state["firmware"].update(
            status="error", recovery_required=True,
            error="Printer reboot required",
        )

        with self.assertRaisesRegex(RuntimeError, "Printer reboot required"):
            plugin._send_command("@RME MACHINE QUERY")
        plugin.on_event("Connected", {})
        self.assertFalse(plugin._state["firmware"]["recovery_required"])
        self.assertEqual(["@RME MACHINE QUERY"], plugin._printer.command_batches)
        self.assertEqual(0, plugin._printer.disconnect_calls)
        self.assertFalse(
            plugin._persistent_snapshot()["firmware"]["recovery_required"]
        )
        self.assertFalse(plugin._printer_transfer_active())

    def test_update_information_exposes_stable_and_beta_channels(self):
        plugin = RmeCompatibilityPlugin()
        plugin._plugin_version = "0.1.0b20"
        templates = plugin.get_template_configs()
        navbar = next(item for item in templates if item["type"] == "navbar")
        self.assertEqual("rme_compatibility_navbar.jinja2", navbar["template"])
        self.assertEqual("visible: navbarVisible", navbar["data_bind"])
        with open(
            "octoprint_rme_compatibility/templates/rme_compatibility_navbar.jinja2"
        ) as navbar_file:
            navbar_template = navbar_file.read()
        self.assertIn("navbarTransferActive", navbar_template)
        self.assertIn("navbarTransferWidth", navbar_template)
        with open(
            "octoprint_rme_compatibility/templates/rme_compatibility_settings.jinja2"
        ) as template_file:
            settings_template = template_file.read()
        self.assertIn("Plugin status", settings_template)
        self.assertIn("Firmware update", settings_template)
        self.assertNotIn("Restart required after installation or update", settings_template)
        self.assertIn("spoolOwnershipText", settings_template)
        self.assertIn("Pending tools (choose any order)", settings_template)
        self.assertIn("Open SpoolManager mapping", settings_template)
        self.assertIn("Open filament mapping", settings_template)
        self.assertNotIn('option value="internal"', settings_template)
        self.assertIn("Current theme", settings_template)
        self.assertIn("rme-theme-swatch", settings_template)
        self.assertNotIn("Encoder −", settings_template)
        self.assertNotIn(">Back</button>", settings_template)
        self.assertNotIn(">Home</button>", settings_template)
        self.assertIn("piUploadStatus", settings_template)
        self.assertIn("Theme presets", settings_template)
        self.assertIn("Delete from Pi", settings_template)
        self.assertIn("Unstage from printer", settings_template)
        self.assertIn("Saved lighting", settings_template)
        self.assertIn("Printer lock", settings_template)
        self.assertIn("Printer USB storage", settings_template)
        self.assertNotIn("stats_poll_interval", settings_template)
        self.assertNotIn("Poll supported firmware every", settings_template)
        self.assertNotIn("spoolmanager_sync_interval", settings_template)
        self.assertNotIn("Reconcile every", settings_template)
        self.assertIn("Download", settings_template)
        with open(
            "octoprint_rme_compatibility/static/js/rme_compatibility.js"
        ) as javascript_file:
            javascript = javascript_file.read()
        self.assertIn("OctoPrint.postForm", javascript)
        self.assertIn('request.upload.addEventListener("progress"', javascript)
        self.assertIn("scheduleCoreWorkflowRender", javascript)
        self.assertIn("renderDashboardWorkflow", javascript)
        self.assertIn("rme-dashboard-workflow-gauge", javascript)
        self.assertIn('target.append(overlay)', javascript)
        self.assertIn('target.removeClass("rme-workflow-active rme-workflow-indeterminate")', javascript)
        self.assertIn('#rme-workflow-overlay, .rme-dashboard-workflow-active', javascript)
        self.assertNotIn('target.after(strip)', javascript)
        self.assertNotIn("self.printerState.printTime(", javascript)
        self.assertNotIn("self.printerState.printTimeLeft(", javascript)
        self.assertIn("function updateCorePrintClock()", javascript)
        self.assertIn("rme-smoothed-print-time", javascript)
        self.assertIn("window.formatDuration(elapsed)", javascript)
        self.assertIn("reported > expected", javascript)
        self.assertIn("pausing|resuming|cancelling|finishing", javascript)
        self.assertIn("self.navbarTransfer = ko.pureComputed", javascript)
        self.assertIn("Firmware → printer", javascript)
        self.assertIn("Printer → Pi", javascript)
        self.assertIn("formatDurationLong", javascript)
        self.assertIn("formatDistance", javascript)
        self.assertIn("applyPersistentLights", javascript)
        self.assertIn("self.pendingNewSpools = ko.pureComputed", javascript)
        self.assertIn("self.activatePendingSpool", javascript)
        self.assertIn("self.loadedFilamentLabel", javascript)
        self.assertIn("self.openNativeSpoolSelector", javascript)
        self.assertIn("sidebarOpenSelectSpoolDialog", javascript)
        self.assertIn("handleOpenSpoolSelector", javascript)
        self.assertIn("Native select…", javascript)
        self.assertIn("if (filament.display_name) details.push(filament.display_name)", javascript)
        self.assertIn("installSpoolManagerPanel", javascript)
        self.assertIn("ensureRmeSpoolMappingDialog", javascript)
        self.assertIn("rme-spool-mapping-dialog", javascript)
        self.assertIn("Loaded on printer", javascript)
        self.assertIn("Provider spool", javascript)
        self.assertIn("Printer loadout mapping", javascript)
        self.assertIn('$("#settings_dialog").modal("hide")', javascript)
        self.assertIn("showAllMappingSpools", javascript)
        self.assertIn("self.showAllMappingSpools = ko.observable(true)", javascript)
        self.assertIn("matching.concat(otherMaterials)", javascript)
        self.assertIn("Show all materials (complete inventory)", javascript)
        self.assertIn('self.command("refresh_spool_inventory")', javascript)
        self.assertIn("options: availableSpools", javascript)
        self.assertIn("Manufacturer: ", javascript)
        self.assertIn("spoolManager.addNewSpool()", javascript)
        self.assertIn('self.command("apply_spool_selections"', javascript)
        self.assertIn("stageAndFlashFirmware", javascript)
        self.assertIn("unstageFirmware", javascript)
        self.assertIn("stage_octoprint_firmware", javascript)
        self.assertIn("rme-local-firmware-action", javascript)
        self.assertIn("children: [], size: 0, date: null, rme: true", javascript)
        self.assertIn("date: item.date == null ? null", javascript)
        self.assertIn("applyPackedBrightness", javascript)
        self.assertIn("applyDecodedBrightness", javascript)
        self.assertIn("lightPolicyText", javascript)
        self.assertIn("Filament resynchronization required", javascript)
        self.assertIn("MMU · idle", javascript)
        self.assertIn("deleteFirmware", javascript)
        self.assertIn('self.command("storage_download"', javascript)
        self.assertIn("storage/downloads/", javascript)
        self.assertIn("installFileManagerBridge", javascript)
        self.assertIn("Download to Pi", settings_template)
        self.assertIn("Download to device", settings_template)
        self.assertIn('class="rme-storage-actions"', settings_template)
        self.assertIn('class="rme-action-group"', settings_template)
        self.assertIn('class="rme-firmware-controls"', settings_template)
        self.assertIn('class="rme-firmware-action-row"', settings_template)
        self.assertIn("Flash verified candidate…", settings_template)
        self.assertIn("storageDownloadWidth", settings_template)
        self.assertIn("Schema 2 firmware", settings_template)
        self.assertIn('"plugin/rme_compatibility/storage/upload"', javascript)
        self.assertIn("octoprint.printer.sdcardupload", __import__(
            "octoprint_rme_compatibility.plugin", fromlist=["__plugin_hooks__"]
        ).__plugin_hooks__)
        self.assertIn("octoprint.filemanager.extension_tree", __import__(
            "octoprint_rme_compatibility.plugin", fromlist=["__plugin_hooks__"]
        ).__plugin_hooks__)
        with open(
            "octoprint_rme_compatibility/templates/rme_compatibility_tab.jinja2"
        ) as template_file:
            tab_template = template_file.read()
        self.assertIn("RME printer statistics", tab_template)
        self.assertNotIn("Firmware update", tab_template)
        self.assertNotIn("RME printer controls", tab_template)
        self.assertIn('"plugin/rme_compatibility/firmware"', javascript)
        with open(
            "octoprint_rme_compatibility/static/css/rme_compatibility.css"
        ) as stylesheet_file:
            stylesheet = stylesheet_file.read()
        self.assertIn("overflow-wrap: anywhere", stylesheet)
        self.assertIn("white-space: normal", stylesheet)
        self.assertIn("flex-wrap: wrap", stylesheet)
        self.assertNotIn("td:last-child { white-space: nowrap", stylesheet)
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

    def test_standard_octoprint_file_can_start_guarded_firmware_workflow(self):
        with tempfile.TemporaryDirectory() as local_directory:
            firmware = os.path.join(local_directory, "coreone-current.bbf")
            with open(firmware, "wb") as stream:
                stream.write(b"signed-bbf-test")
            plugin = RmeCompatibilityPlugin()
            plugin._file_manager = types.SimpleNamespace(
                path_on_disk=lambda origin, path: firmware
            )
            started = []
            plugin._start_firmware_path = lambda path, flash_after_stage=False: started.append(
                (path, flash_after_stage)
            )

            plugin._start_octoprint_firmware_upload(
                "updates/coreone-current.bbf", flash_after_stage=True
            )

            self.assertEqual([(firmware, True)], started)

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

    def test_terminal_rme_atcommand_is_forwarded_when_transfer_is_idle(self):
        sent = []
        comm = types.SimpleNamespace(
            _do_send=lambda command, gcode=None: sent.append((command, gcode))
        )
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-terminal-atcommand-test")
        plugin._file_service = types.SimpleNamespace(busy=False)

        plugin.atcommand_sending_hook(
            comm,
            "sending",
            "RME",
            "FIRMWARE QUERY",
            tags={"source:terminal"},
        )

        self.assertEqual([("@RME FIRMWARE QUERY", None)], sent)

    def test_terminal_rme_atcommand_cannot_interrupt_active_transfer(self):
        sent = []
        comm = types.SimpleNamespace(
            _do_send=lambda command, gcode=None: sent.append((command, gcode))
        )
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-terminal-transfer-latch-test")
        plugin._file_service = types.SimpleNamespace(busy=True)

        plugin.atcommand_sending_hook(
            comm,
            "sending",
            "RME",
            "MACHINE QUERY",
            tags={"source:terminal"},
        )
        plugin.atcommand_sending_hook(
            comm,
            "sending",
            "RME",
            "FILE WRITE_BULK_CHUNK offset=0 data=YQ==",
            tags={"plugin:rme_compatibility"},
        )

        self.assertEqual(
            [("@RME FILE WRITE_BULK_CHUNK offset=0 data=YQ==", None)], sent
        )

    def test_binary_marker_writes_raw_frame_on_octoprint_send_thread(self):
        class RawSerial(object):
            def __init__(self):
                self.data = bytearray()

            def write(self, data):
                # Exercise partial writes as a real serial adapter may return
                # fewer bytes than requested.
                count = min(3, len(data))
                self.data.extend(data[:count])
                return count

        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-binary-marker-test")
        token = "1" * 32
        pending_data = {
            "frame": b"\x00\xffbinary-frame",
            "event": threading.Event(),
            "error": None,
        }
        pending_end = {
            "frame": b"\x0e\x00\x00\x00\x00\x00\x00\x00\x00\x00",
            "event": threading.Event(),
            "error": None,
        }
        session_queue = queue.Queue()
        session_done = threading.Event()
        session_queue.put(pending_data)
        session_queue.put(pending_end)
        plugin._binary_session = {
            "token": token, "queue": session_queue, "done": session_done,
        }
        serial = RawSerial()
        comm = types.SimpleNamespace(_serial=serial)

        worker = threading.Thread(
            target=plugin.atcommand_sending_hook,
            args=(comm, "sending", "RME", "FILE RAW_SESSION token=%s" % token),
            kwargs={"tags": set()},
        )
        worker.start()
        self.assertTrue(pending_end["event"].wait(1))
        self.assertTrue(worker.is_alive())
        session_queue.put(None)
        worker.join(1)

        self.assertTrue(pending_data["event"].is_set())
        self.assertTrue(pending_end["event"].is_set())
        self.assertIsNone(pending_data["error"])
        self.assertEqual(
            pending_data["frame"] + pending_end["frame"], bytes(serial.data)
        )
        self.assertFalse(worker.is_alive())
        self.assertTrue(session_done.is_set())
        self.assertIsNone(plugin._binary_session)

    def test_binary_marker_bypasses_normal_octoprint_command_backlog(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()

        marker = "@RME FILE RAW_SESSION token=" + "a" * 32
        plugin._send_command(marker)

        self.assertEqual(
            [
                (
                    marker,
                    {"plugin:rme_compatibility", "rme:binary_session"},
                )
            ],
            plugin._printer.forced_commands,
        )

    def test_configuration_commands_are_blocked_during_file_transfer(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._file_service = types.SimpleNamespace(busy=True)

        with self.assertRaisesRegex(RuntimeError, "deferred during a file transfer"):
            plugin._send_command("M865 Q")
        with self.assertRaisesRegex(RuntimeError, "deferred during a file transfer"):
            plugin._send_commands(["@RME FILAMENT QUERY", "M865 Q"])

        plugin._send_command("@RME FILE CAPS")
        self.assertEqual(["@RME FILE CAPS"], plugin._printer.command_batches)

    def test_end_binary_transport_waits_for_writer_release(self):
        plugin = RmeCompatibilityPlugin()
        session_queue = queue.Queue()
        done = threading.Event()
        plugin._binary_session = {
            "token": "4" * 32, "queue": session_queue, "done": done,
        }

        def release():
            self.assertIsNone(session_queue.get(timeout=1))
            done.set()

        worker = threading.Thread(target=release)
        worker.start()
        plugin._end_binary_transport()
        worker.join(1)

        self.assertTrue(done.is_set())

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

    def test_validator_mapping_publication_is_idempotent_but_survives_reload(self):
        plugin = RmeCompatibilityPlugin()
        first_validator = _Validator()
        info = types.SimpleNamespace(implementation=first_validator)
        plugin._plugin_manager = types.SimpleNamespace(
            plugins={"Nozzle_Filament_Validator": info},
        )

        self.assertTrue(plugin._configure_validator_mapping({0: 1, 1: 0}))
        self.assertFalse(plugin._configure_validator_mapping({1: 0, 0: 1}))
        self.assertEqual([{0: 1, 1: 0}], first_validator.mapping_calls)

        self.assertTrue(plugin._configure_validator_mapping({0: 0, 1: 1}))
        self.assertEqual(2, len(first_validator.mapping_calls))

        replacement_validator = _Validator()
        info.implementation = replacement_validator
        self.assertTrue(plugin._configure_validator_mapping({0: 0, 1: 1}))
        self.assertEqual([{0: 0, 1: 1}], replacement_validator.mapping_calls)

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

    def test_firmware_alias_recovers_provider_manufacturer(self):
        plugin = RmeCompatibilityPlugin()
        plugin._defer = lambda callback, *args: None
        plugin._state["machine"] = {"logical_tools": 1}
        plugin._state["spoolmanager"].update(
            provider="spoolmanager",
            published=[{
                "alias": "PLA-00D", "database_id": 13,
                "display_name": "Galaxy Black PLA", "vendor": "Prusament",
                "material": "PLA", "color": "#808080",
            }],
        )

        plugin._handle_record({
            "record": "loaded_filament", "tool": 0, "material": "PLA",
            "profile": "PLA-00D",
            "color_name": "Custom", "color": "#808080",
        })

        loaded = plugin._state["loaded_filaments"][0]
        self.assertEqual("PLA-00D", loaded["firmware_alias"])
        self.assertEqual("PLA", loaded["material"])
        self.assertEqual("Prusament", loaded["vendor"])
        self.assertEqual("Galaxy Black PLA", loaded["display_name"])
        report = plugin._filament_report()["data"]["tools"][0]
        self.assertEqual("13", report["spool_id"])
        self.assertEqual("Prusament", report["vendor"])

    def test_current_loaded_filament_wire_format_reaches_provider_bridge(self):
        plugin = RmeCompatibilityPlugin()
        accepted = []
        plugin._schedule_publish = lambda: None
        plugin._defer = lambda callback, *args: accepted.append(args[0]) if callback == plugin._accept_firmware_spool else None
        plugin._state["spoolmanager"]["published"] = []

        record = parse_line(
            'loaded_filament T2 S"PETG" P"PET-00L" O"Black" H"#000000" M"Polymaker"'
        )
        plugin._handle_record(record)

        self.assertEqual("Polymaker", plugin._state["loaded_filaments"][0]["vendor"])
        self.assertEqual("#000000", plugin._state["loaded_filaments"][0]["color"])
        self.assertTrue(
            plugin._state["loaded_filaments"][0]["material_family_reported"]
        )
        self.assertEqual("PETG", accepted[0]["material"])
        self.assertEqual("PET-00L", accepted[0]["profile"])

    def test_current_firmware_duplicate_alias_recovers_provider_base_family(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-duplicate-alias-base-test")
        plugin._schedule_publish = lambda: None
        plugin._defer = lambda callback, *args: None
        plugin._state.update(connected=True, supported=True)
        plugin._state["spoolmanager"].update(
            provider="spoolmanager",
            published=[{
                "alias": "PET-00O", "database_id": 24,
                "display_name": "Black PETG", "vendor": "Polymaker",
                "material": "PETG", "color": "#000000",
            }],
        )

        plugin._handle_record(parse_line(
            'loaded_filament T2 S"PET-00O" P"PET-00O" '
            'O"Black" H"#000000" M"Polymaker"'
        ))

        loaded = plugin._state["loaded_filaments"][0]
        self.assertEqual("PET-00O", loaded["firmware_reported_material"])
        self.assertEqual("PET-00O", loaded["firmware_profile"])
        self.assertEqual("PETG", loaded["firmware_material"])
        self.assertEqual("PETG", loaded["material"])
        self.assertEqual("Black PETG", loaded["display_name"])
        self.assertTrue(loaded["material_family_reported"])
        command = "M976 A 0:2:PETG:255"
        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing", command, None, "M976", tags={"source:job"},
        ))

    def test_machine_manufacturer_is_retained_regardless_of_query_order(self):
        plugin = RmeCompatibilityPlugin()
        plugin._schedule_publish = lambda: None
        plugin._defer = lambda callback, *args: None

        plugin._handle_record({
            "record": "manufacturer_loaded", "tool": 2, "name": "Polymaker",
        })
        plugin._handle_record(parse_line(
            'loaded_filament T2 S"PETG" P"PET-00L" O"Black" H"#000000"'
        ))

        loaded = plugin._state["loaded_filaments"][0]
        self.assertEqual("Polymaker", loaded["manufacturer"])
        self.assertEqual("Polymaker", loaded["vendor"])

        plugin._handle_record({
            "record": "manufacturer_loaded", "tool": 2, "name": "Prusament",
        })
        loaded = plugin._state["loaded_filaments"][0]
        self.assertEqual("Prusament", loaded["manufacturer"])
        self.assertEqual("Prusament", loaded["vendor"])

    def test_machine_to_provider_sync_matches_current_profile_not_base_material(self):
        class Provider(object):
            def __init__(self):
                self.selections = []

            def select(self, tool, database_id):
                self.selections.append((tool, database_id))

        provider = Provider()
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-machine-provider-profile-test")
        plugin._active_spool_provider = lambda: (provider, "spoolmanager")
        plugin._sync_spoolmanager = lambda *args: syncs.append(args)
        plugin._state["spoolmanager"].update(
            published=[{
                "alias": "PET-00L", "database_id": 13,
                "display_name": "Black PETG", "material": "PETG",
                "vendor": "Polymaker", "color": "#000000",
            }],
            selected=[],
        )
        syncs = []

        plugin._accept_firmware_spool({
            "tool": 2, "material": "PETG", "profile": "PET-00L",
            "color_name": "Black", "color": "#000000",
            "vendor": "Polymaker",
        })

        self.assertEqual([(2, 13)], provider.selections)
        self.assertEqual([(True, True)], syncs)
        self.assertIsNone(plugin._state["spoolmanager"]["pending_new"])

    def test_machine_to_provider_sync_imports_all_five_mmu_tools(self):
        class Provider(object):
            def __init__(self):
                self.selections = []

            def select(self, tool, database_id):
                self.selections.append((tool, database_id))

        provider = Provider()
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-machine-provider-five-tool-test")
        plugin._active_spool_provider = lambda: (provider, "spoolmanager")
        plugin._sync_spoolmanager = lambda *args: None
        profiles = [
            ("PLA", "PLA-00D"),
            ("PLA", "PLA-00C"),
            ("PETG", "PET-00L"),
            ("PLA", "PLA-002"),
            ("PLA", "PLA-00H"),
        ]
        plugin._state["spoolmanager"].update(
            published=[
                {
                    "alias": profile, "database_id": 20 + tool,
                    "display_name": "Spool %d" % tool,
                    "material": material, "vendor": "Vendor",
                    "color": "#808080",
                }
                for tool, (material, profile) in enumerate(profiles)
            ],
            selected=[],
        )

        for tool, (material, profile) in enumerate(profiles):
            plugin._accept_firmware_spool({
                "tool": tool, "material": material, "profile": profile,
                "color_name": "Grey", "color": "#808080",
                "vendor": "Vendor",
            })

        self.assertEqual(
            [(tool, 20 + tool) for tool in range(5)],
            provider.selections,
        )
        self.assertIsNone(plugin._state["spoolmanager"]["pending_new"])

    def test_unknown_firmware_profiles_queue_every_tool_and_allow_any_order(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._logger = logging.getLogger("rme-machine-provider-pending-queue-test")
        plugin._active_spool_provider = lambda: (types.SimpleNamespace(), "spoolmanager")
        plugin._persist_and_publish = lambda: None
        plugin._state["spoolmanager"].update(published=[], selected=[])

        for tool in range(5):
            plugin._accept_firmware_spool({
                "tool": tool,
                "material": "PETG" if tool == 2 else "PLA",
                "profile": "PROFILE-%d" % tool,
                "color_name": "Color %d" % tool,
                "color": "#808080",
                "vendor": "Vendor",
            })

        self.assertEqual(
            [0, 1, 2, 3, 4],
            [item["tool"] for item in
             plugin._state["spoolmanager"]["pending_new_queue"]],
        )
        self.assertEqual(0, plugin._state["spoolmanager"]["pending_new"]["tool"])

        plugin._activate_pending_spool(3)

        self.assertEqual(3, plugin._state["spoolmanager"]["pending_new"]["tool"])
        self.assertEqual(
            [0, 1, 2, 3, 4],
            [item["tool"] for item in
             plugin._state["spoolmanager"]["pending_new_queue"]],
        )

    def test_completing_or_skipping_pending_spool_preserves_other_tools(self):
        class Provider(object):
            def __init__(self):
                self.created = []
                self.selected = []

            def create(self, values):
                self.created.append(values)
                return {"database_id": 91}

            def select(self, tool, database_id):
                self.selected.append((tool, database_id))

        provider = Provider()
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._active_spool_provider = lambda: (provider, "spoolmanager")
        plugin._persist_and_publish = lambda: None
        plugin._sync_spoolmanager = lambda *args: None
        for tool in range(3):
            plugin._begin_new_spool(tool)

        plugin._activate_pending_spool(1)
        plugin._create_spool({
            "display_name": "Middle spool", "material": "PLA",
            "color": "#808080", "total_weight": 1000,
        })

        self.assertEqual([(1, 91)], provider.selected)
        self.assertEqual(
            [0, 2],
            [item["tool"] for item in
             plugin._state["spoolmanager"]["pending_new_queue"]],
        )
        self.assertEqual(0, plugin._state["spoolmanager"]["pending_new"]["tool"])

        plugin._cancel_pending_spool()

        self.assertEqual([2], [
            item["tool"] for item in
            plugin._state["spoolmanager"]["pending_new_queue"]
        ])
        self.assertEqual(2, plugin._state["spoolmanager"]["pending_new"]["tool"])

    def test_mapping_loaded_tool_to_existing_spool_clears_only_that_draft(self):
        class Provider(object):
            def __init__(self):
                self.selected = []

            def select(self, tool, database_id):
                self.selected.append((tool, database_id))

        provider = Provider()
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._active_spool_provider = lambda: (provider, "spoolmanager")
        plugin._persist_and_publish = lambda: None
        plugin._sync_spoolmanager = lambda *args: None
        for tool in range(3):
            plugin._begin_new_spool(tool)
        plugin._activate_pending_spool(1)

        plugin._select_spool_from_octoprint(2, 47)

        self.assertEqual([(2, 47)], provider.selected)
        self.assertEqual(
            [0, 1],
            [item["tool"] for item in
             plugin._state["spoolmanager"]["pending_new_queue"]],
        )
        self.assertEqual(1, plugin._state["spoolmanager"]["pending_new"]["tool"])

    def test_staged_spool_mapping_applies_all_tools_with_one_firmware_sync(self):
        class Provider(object):
            def __init__(self):
                self.selected = []
                self.deselected = []
                self.refreshes = 0

            def get(self, database_id):
                return {"database_id": database_id} if database_id in (41, 44) else None

            def select(self, tool, database_id):
                self.selected.append((tool, database_id))

            def deselect(self, tool):
                self.deselected.append(tool)

            def refresh_clients(self):
                self.refreshes += 1

        provider = Provider()
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._logger = logging.getLogger("rme-staged-provider-mapping-test")
        plugin._active_spool_provider = lambda: (provider, "spoolmanager")
        plugin._persist_and_publish = lambda: None
        sync_calls = []
        plugin._sync_spoolmanager = lambda *args: sync_calls.append(args)
        plugin._mark_expected_provider_event = lambda *args: None
        plugin._state["spoolmanager"]["pending_provider_sync"] = {
            "tool": 2, "database_id": 44, "message": "Apply?",
        }
        for tool in range(3):
            plugin._begin_new_spool(tool)

        plugin._apply_spool_selections([
            {"tool": 0, "database_id": 41},
            {"tool": 1, "database_id": None},
            {"tool": 2, "database_id": 44},
        ])

        self.assertEqual([(0, 41), (2, 44)], provider.selected)
        self.assertEqual([1], provider.deselected)
        self.assertEqual(1, provider.refreshes)
        self.assertEqual([(True,)], sync_calls)
        self.assertIsNone(plugin._state["spoolmanager"]["pending_provider_sync"])
        self.assertEqual(
            [1],
            [item["tool"] for item in
             plugin._state["spoolmanager"]["pending_new_queue"]],
        )

    def test_legacy_loaded_profile_provider_enrichment_does_not_edit_m976(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-m976-provider-legacy-test")
        plugin._schedule_publish = lambda: None
        plugin._defer = lambda callback, *args: None
        plugin._state.update(connected=True, supported=True)
        plugin._state["spoolmanager"].update(
            provider="spoolmanager",
            published=[{
                "alias": "PET-00L", "database_id": 13,
                "display_name": "Black PETG", "vendor": "Polymaker",
                "material": "PETG", "color": "#000000",
            }],
        )

        plugin._handle_record(parse_line(
            'loaded_filament T2 S"PET-00L" O"Black" H"#000000" M"Polymaker"'
        ))

        loaded = plugin._state["loaded_filaments"][0]
        self.assertEqual("PETG", loaded["material"])
        self.assertEqual("PET-00L", loaded["firmware_material"])
        self.assertFalse(loaded["material_family_reported"])
        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing", "M976 A 0:2:PETG:255", None, "M976",
            tags={"source:job"},
        ))

    def test_stats_use_connection_and_print_lifecycle_snapshots_without_polling(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        queued = []
        plugin._send_command = queued.append
        plugin._schedule_publish = lambda: queued.append(("publish", ()))
        plugin._state.update(connected=True, supported=True)

        plugin._probe_stats()
        plugin._probe_stats()
        self.assertEqual(["@RME STATS QUERY"], queued)

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
        plugin._refresh_stats_snapshot()
        self.assertEqual("@RME STATS QUERY", queued[0])

        # The print-completion refresh must still yield while a job owns serial.
        queued.clear()
        plugin._printer = _Printer()
        plugin._printer.is_printing = lambda: True
        plugin._refresh_stats_snapshot()
        self.assertEqual([], queued)

        plugin._printer.is_printing = lambda: False
        plugin._refresh_stats_snapshot()
        self.assertEqual("@RME STATS QUERY", queued[0])

        queued.clear()
        plugin._handle_record({"record": "rme_error", "message": "RME_ERROR STATS unsupported"})
        plugin._refresh_stats_snapshot()
        self.assertEqual(1, len(queued))  # state publication only; no command

    def test_latest_session_fields_and_firmware_restart_are_preserved(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        plugin._settings.values["auto_open_session"] = False
        commands = []
        refreshes = []
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._send_command = commands.append
        plugin._schedule_configuration_refresh = refreshes.append
        plugin._schedule_publish = lambda: None

        session = parse_line(
            "RME_SESSION lease=1 printer_state=idle legacy=0 preferred_baud=1000000 "
            "fallback_baud=250000,230400,115200"
        )
        plugin._handle_record(session)
        self.assertEqual(1000000, plugin._state["session"]["preferred_baud"])
        self.assertTrue(plugin._state["session"]["active"])
        self.assertEqual("idle", plugin._state["session"]["printer_state"])
        self.assertEqual(
            "250000,230400,115200",
            plugin._state["session"]["fallback_baud"],
        )
        self.assertEqual(["@RME DIALOG QUERY"], commands)
        self.assertEqual(["all"], refreshes)

        # The reply to each periodic KEEPALIVE updates lease metadata but must
        # not turn into another complete configuration snapshot.
        plugin._handle_record(dict(session))
        self.assertEqual(["@RME DIALOG QUERY"], commands)
        self.assertEqual(["all"], refreshes)

        plugin._handle_record(parse_line("RME_FIRMWARE_RESTART reconnect=1"))
        self.assertEqual("restarting", plugin._state["firmware"]["status"])
        self.assertTrue(plugin._state["firmware"]["reconnect_expected"])

        # If USB never leaves and a later authoritative session still says
        # IDLE, the handoff failed or was stale. It must not cancel a new job.
        plugin._printer = _Printer()
        plugin._state["workflow"] = {
            "workflow": "firmware_update", "state": "restarting",
            "message": "Firmware staged; USB will reconnect after installation",
        }
        plugin._handle_record(dict(session))
        self.assertEqual("idle", plugin._state["firmware"]["status"])
        self.assertIsNone(plugin._state["workflow"])
        self.assertEqual([False], plugin._printer.holds)
        self.assertFalse(plugin._printer_transfer_active())

    def test_unsupported_toolmap_probe_is_suppressed_after_capability_reply(self):
        plugin = RmeCompatibilityPlugin()
        plugin._schedule_publish = lambda: None

        plugin._handle_record({
            "record": "rme_error",
            "message": "echo:RME_ERROR code=unsupported feature=tool_mapping",
        })

        self.assertFalse(plugin._toolmap_supported)
        self.assertNotIn("@RME TOOLMAP QUERY", plugin._configuration_queries())
        self.assertEqual([], plugin._state["errors"])

    def test_stage_reconciliation_never_promotes_an_unclaimed_usb_file(self):
        records = [
            {"candidate": 0, "armed": 0, "state": "idle"},
            {
                "candidate": 1, "armed": 0, "state": "ready",
                "path": "FWUPD.RME", "size": 3921020, "sha256": "a" * 64,
            },
        ]
        plugin = RmeCompatibilityPlugin()
        plugin._file_service = types.SimpleNamespace(
            firmware_status=lambda: records.pop(0)
        )
        plugin._persist_and_publish = lambda: None

        self.assertFalse(plugin._reconcile_firmware_stage())
        self.assertEqual("idle", plugin._state["firmware"]["status"])

        plugin._state["firmware"].update(status="ready", size=1)
        self.assertTrue(plugin._reconcile_firmware_stage())
        self.assertEqual("ready", plugin._state["firmware"]["status"])
        self.assertEqual(3921020, plugin._state["firmware"]["size"])

    def test_current_firmware_stage_state_is_authoritative(self):
        records = [
            {
                "record": "firmware_status", "candidate": 1, "armed": 0,
                "state": "ready", "path": "FWUPD.RME", "size": 3921020,
                "sha256": "c" * 64,
            },
            {
                "record": "firmware_status", "candidate": 0, "armed": 0,
                "state": "idle",
            },
        ]
        plugin = RmeCompatibilityPlugin()
        plugin._file_service = types.SimpleNamespace(
            firmware_status=lambda: records.pop(0)
        )
        plugin._state["storage"]["caps"] = {"firmware_status": 1}
        plugin._persist_and_publish = lambda: None

        self.assertTrue(plugin._reconcile_firmware_stage())
        self.assertEqual("ready", plugin._state["firmware"]["status"])
        self.assertEqual("c" * 64, plugin._state["firmware"]["sha256"])
        self.assertFalse(plugin._state["firmware"]["armed"])

        self.assertFalse(plugin._reconcile_firmware_stage())
        self.assertEqual("idle", plugin._state["firmware"]["status"])

    def test_validating_firmware_candidate_remains_verifying(self):
        plugin = RmeCompatibilityPlugin()
        with plugin._state_lock:
            self.assertTrue(plugin._apply_authoritative_firmware_locked({
                "record": "firmware_status", "candidate": 1, "armed": 0,
                "state": "validating", "path": "FWUPD.RME",
                "size": 4000, "progress": 1000,
            }))

        firmware = plugin._state["firmware"]
        self.assertEqual("verifying", firmware["status"])
        self.assertEqual(1000, firmware["offset"])
        self.assertEqual(25, firmware["progress"])

    def test_flash_disconnect_enters_expected_reconnect_without_409(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-flash-disconnect-test")
        plugin._printer = _Printer()
        plugin._file_service = types.SimpleNamespace(
            mutate=lambda *args: (_ for _ in ()).throw(
                FileServiceError("Printer disconnected")
            )
        )
        plugin._state["firmware"].update(status="ready", candidate=True)
        plugin._persist_and_publish = lambda: None
        reconnects = []
        plugin._begin_firmware_reconnect = lambda *args, **kwargs: reconnects.append(True)

        try:
            plugin._flash_firmware()
            self.assertEqual("restarting", plugin._state["firmware"]["status"])
            self.assertTrue(plugin._state["firmware"]["reconnect_expected"])
            self.assertEqual([True], reconnects)
        finally:
            plugin._cancel_firmware_reconnect()

    def test_current_firmware_unstage_uses_dedicated_command(self):
        calls = []
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._file_service = types.SimpleNamespace(
            unstage_firmware=lambda: calls.append("unstage") or {
                "record": "firmware_unstaged", "candidate": 0, "armed": 0,
            }
        )
        plugin._state.update(connected=True, supported=True)
        plugin._state["firmware"].update(
            status="ready", staged_path="/usb/FWUPD.RME"
        )
        plugin._state["storage"].update(
            supported=True, caps={"firmware_unstage": 1}
        )
        plugin._persist_and_publish = lambda: None
        plugin._defer = lambda callback, *args: None

        plugin._unstage_firmware()
        self.assertEqual(["unstage"], calls)
        self.assertEqual("idle", plugin._state["firmware"]["status"])

    def test_unstage_deletes_only_the_protected_stage_and_is_idempotent(self):
        class FileService(object):
            def __init__(self):
                self.present = True
                self.mutations = []

            def unstage_firmware(self):
                self.mutations.append("unstage")
                self.present = False

        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._file_service = FileService()
        plugin._state.update(connected=True, supported=True)
        plugin._state["firmware"].update(status="ready", staged_path="/usb/FWUPD.RME")
        plugin._state["storage"]["supported"] = True
        plugin._persist_and_publish = lambda: None
        plugin._defer = lambda callback, *args: None

        plugin._unstage_firmware()
        self.assertEqual(["unstage"], plugin._file_service.mutations)
        self.assertEqual("idle", plugin._state["firmware"]["status"])

        plugin._unstage_firmware()
        self.assertEqual(["unstage", "unstage"], plugin._file_service.mutations)

    def test_schema_two_lighting_snapshot_is_aggregated_without_polling(self):
        plugin = RmeCompatibilityPlugin()
        plugin._schedule_publish = lambda: None
        records = [
            "RME_LIGHT screen_persistent=60 chamber_print=100 screen_print=60 "
            "status_print=100 schema=2 screen_supported=1 chamber_supported=1 "
            "status_supported=1 screen=336862780 chamber=336862820 status=336862820",
            "RME_LIGHT_STATE state=deep_idle screen=20 chamber=20 status=20",
            "RME_LIGHT_STATE state=idle screen=20 chamber=20 status=20",
            "RME_LIGHT_STATE state=active screen=100 chamber=100 status=100",
            "RME_LIGHT_STATE state=printing screen=60 chamber=100 status=100",
            "RME_LIGHT_POLICY activity_timeout_s=120 event_timeout_s=300 "
            "off_timeout_s=120 door_holds_active=1 post_print_hold=1 "
            "status_finished_hold_s=300",
            "RME_LIGHT_LIVE state=idle screen=20 chamber=20 print_screen=60 "
            "print_chamber=100 print_status=100",
        ]
        for line in records:
            plugin._handle_record(parse_line(line))

        light = plugin._state["light"]
        self.assertEqual(2, light["schema"])
        self.assertEqual(60, light["states"]["printing"]["screen"])
        self.assertEqual(300, light["policy"]["event_timeout_s"])
        self.assertEqual("idle", light["live"]["state"])
        self.assertEqual(20, light["live"]["chamber"])

    def test_lighting_ui_uses_current_firmware_byte_order(self):
        with open(
            "octoprint_rme_compatibility/static/js/rme_compatibility.js"
        ) as javascript_file:
            javascript = javascript_file.read()

        # Current Buddy firmware defines deep_idle=0, idle=1, active=2, and
        # printing=3 with light_state_shift(state) == state * 8.  Keep both
        # browser directions aligned with that 0xPPAAIIDD representation.
        self.assertIn("profile.deepIdle(value & 0xff)", javascript)
        self.assertIn("profile.idle((value >>> 8) & 0xff)", javascript)
        self.assertIn("profile.active((value >>> 16) & 0xff)", javascript)
        self.assertIn("profile.printing((value >>> 24) & 0xff)", javascript)
        self.assertIn(
            "byte(profile.deepIdle()) + byte(profile.idle()) * 0x100 +",
            javascript,
        )
        self.assertIn(
            "byte(profile.active()) * 0x10000 + "
            "byte(profile.printing()) * 0x1000000",
            javascript,
        )

    def test_configuration_changes_drive_domain_refresh_without_polling(self):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = _Settings()
        refreshed = []
        plugin._schedule_publish = lambda: None
        plugin._schedule_configuration_refresh = refreshed.append
        plugin._state["session"].update(last_seq=10, configuration_revision=4)

        plugin._handle_record({
            "record": "change", "seq": 11, "revision": 5,
            "domain": "theme", "key": "colors", "origin": "local",
        })
        self.assertEqual(["theme"], refreshed)
        self.assertEqual(11, plugin._state["session"]["last_seq"])
        self.assertEqual(5, plugin._state["session"]["configuration_revision"])

        # Provider publication transactions are acknowledgements of commands
        # this plugin just sent. They advance sequence state but must not start
        # another full catalog query/publication cycle.
        command = plugin._with_transaction(
            "@RME FILAMENT SET slot=0 name=PLA-001 nozzle=215 preheat=175 bed=60 visible=1",
            suppress_refresh=True,
        )
        transaction = int(command.rsplit("tx=", 1)[1])
        plugin._handle_record({
            "record": "change", "seq": 12, "revision": 6,
            "domain": "filament", "key": "preset", "origin": "host",
            "tx": transaction,
        })
        self.assertEqual(["theme"], refreshed)
        self.assertEqual(12, plugin._state["session"]["last_seq"])
        self.assertEqual(6, plugin._state["session"]["configuration_revision"])

        commands = []
        plugin._send_command = commands.append
        plugin._open_session()
        self.assertEqual("@RME SESSION OPEN events=31 legacy=0", commands[-1])

    def test_extrusion_fault_cause_survives_shared_recovery_progress(self):
        plugin = RmeCompatibilityPlugin()
        commands = []
        plugin._send_command = commands.append
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._schedule_publish = lambda: None

        plugin._handle_record({
            "record": "event", "seq": 1, "type": "error",
            "workflow": "filament_movement", "state": "waiting",
            "code": "not_moving",
            "message": "Loadcell detected filament not moving",
        })
        self.assertEqual(
            ["@RME DIALOG QUERY", "@RME STUCK QUERY"], commands,
        )
        plugin._handle_record({
            "record": "prompt", "actions": ["Continue", "Unload", "Abort"],
        })

        plugin._handle_record({
            "record": "event", "seq": 2, "type": "progress",
            "workflow": "filament_unload", "state": "active", "progress": 40,
            "message": "Unloading filament",
        })
        workflow = plugin._state["workflow"]
        self.assertEqual("filament_movement", workflow["workflow"])
        self.assertEqual("not_moving", workflow["code"])
        self.assertEqual("filament_unload", workflow["recovery"]["workflow"])
        self.assertEqual(
            "Loadcell detected filament not moving",
            plugin._state["prompt"]["message"],
        )

        plugin._handle_record({
            "record": "event", "seq": 3, "type": "progress",
            "workflow": "filament_unload", "state": "completed", "progress": 100,
            "message": "Filament unloaded",
        })
        self.assertEqual("filament_unload", plugin._state["workflow"]["workflow"])
        self.assertIsNone(plugin._state["prompt"])

    def test_runout_uses_dialog_only_and_print_end_clears_fault(self):
        from octoprint.events import Events

        plugin = RmeCompatibilityPlugin()
        commands = []
        plugin._send_command = commands.append
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._schedule_publish = lambda: None
        plugin._state.update(connected=True, supported=True)

        plugin._handle_record({
            "record": "event", "seq": 1, "type": "error",
            "workflow": "filament_runout", "state": "waiting",
            "code": "runout", "message": "Loadcell detected filament runout",
        })
        self.assertEqual(["@RME DIALOG QUERY"], commands)
        plugin._state["prompt"] = {"kind": "firmware", "actions": ["Continue"]}

        plugin._defer = lambda callback, *args: None
        plugin.on_event(Events.PRINT_CANCELLED, {})
        self.assertIsNone(plugin._state["workflow"])
        self.assertIsNone(plugin._state["prompt"])

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

    def test_m976_batch_is_never_rewritten_from_loaded_filament_state(self):
        plugin = RmeCompatibilityPlugin()
        plugin._logger = logging.getLogger("rme-m976-translation-test")
        plugin._state.update(connected=True, supported=True)
        plugin._state["loaded_filaments"] = [
            {"tool": 0, "material": "PLA-00D"},
            {"tool": 2, "material": "PET-00L"},
        ]

        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing", "M976 A 0:2:PETG:255", None, "M976",
            tags={"source:job"},
        ))
        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing",
            "M976 A 0:0:PLA:215,0:2:PETG:255 ; calibrate",
            None, "M976", tags={"source:job"},
        ))

    def test_m976_batch_preserves_current_authoritative_material_family(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state.update(connected=True, supported=True)
        plugin._state["loaded_filaments"] = [{
            "tool": 2, "material": "PETG", "profile": "PET-00L",
            "firmware_material": "PETG", "firmware_profile": "PET-00L",
            "material_family_reported": True,
        }]
        command = "M976 A 0:2:PETG:255"

        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing", command, None, "M976", tags={"source:job"},
        ))

    def test_motors_enabled_m117_is_not_mirrored_to_rme_printer(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state.update(connected=True, supported=True)

        self.assertEqual((None,), plugin.gcode_queuing_hook(
            None, "queuing", "M117 Motors enabled.", None, "M117",
            tags={"source:plugin"},
        ))
        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing", "M117 Heating bed", None, "M117",
            tags={"source:plugin"},
        ))

    def test_m976_batch_is_untouched_without_an_exact_loaded_assignment(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state.update(connected=True, supported=True)
        command = "M976 A 0:2:PETG:255"

        self.assertIsNone(plugin.gcode_queuing_hook(
            None, "queuing", command, None, "M976", tags={"source:job"},
        ))

    def test_m20_is_replaced_only_after_rme_storage_discovery(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        deferred = []
        plugin._defer = lambda callback, *args: deferred.append(callback)
        plugin._state.update(connected=True, supported=True)

        result = plugin.gcode_queuing_hook(
            None, "queuing", "M20", None, "M20", tags={"source:api"}
        )
        self.assertIsNone(result)

        plugin._state["storage"]["supported"] = True
        result = plugin.gcode_queuing_hook(
            None, "queuing", "M20", None, "M20", tags={"source:api"}
        )
        self.assertEqual((None,), result)
        self.assertEqual([plugin._refresh_native_storage_files], deferred)

        plugin._printer.is_printing = lambda: True
        result = plugin.gcode_queuing_hook(
            None, "queuing", "M20", None, "M20", tags={"source:api"}
        )
        self.assertEqual((None,), result)
        self.assertEqual(1, len(deferred))

    def test_rme_artifact_extensions_are_visible_but_not_machinecode(self):
        self.assertEqual(
            {"model": {"rme_artifact": ["bbf", "bin"]}},
            RmeCompatibilityPlugin.file_extension_hook(),
        )
        plugin = RmeCompatibilityPlugin()
        self.assertEqual("machinecode", plugin._native_storage_extension("part.bgcode"))
        self.assertEqual("model", plugin._native_storage_extension("firmware.bbf"))
        self.assertEqual("model", plugin._native_storage_extension("buddy-dump.bin"))

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
