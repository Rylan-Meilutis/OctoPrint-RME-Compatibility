import ast
import logging
import os
import queue
import sys
import tempfile
import threading
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
from octoprint_rme_compatibility.file_service import FileServiceError
from octoprint_rme_compatibility.protocol import parse_line


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

    def test_external_provider_clears_unselected_builtin_tool_assignment(self):
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
                return [dict(record)]

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
            machine={"logical_tools": 2},
        )
        plugin._state["spoolmanager"].update(provider="internal")
        plugin._state["manufacturers"]["profiles"] = [
            {"builtin": 1, "slot": 6, "name": "Atomic Filament"}
        ]

        plugin._sync_spoolmanager(True)

        commands = [command for batch in plugin._printer.command_batches
                    for command in (batch if isinstance(batch, list) else [batch])]
        self.assertIn('M865 U0 L0 O"#193a8a"', commands)
        self.assertIn('M865 V0 O"#193a8a" N"Blue"', commands)
        self.assertTrue(any(
            command.startswith("@RME MANUFACTURER ASSIGN tool=0 name=Atomic%20Filament tx=")
            for command in commands
        ))
        self.assertIn('M865 S"---" L1', commands)
        self.assertTrue(any(
            command.startswith("@RME MANUFACTURER ASSIGN tool=1 name=none tx=")
            for command in commands
        ))

        # A manufacturer-query completion reconciles with force=False. Once
        # this exact snapshot has been published, it must not enqueue the
        # colors, presets, assignments, or another M865 query again.
        batch_count = len(plugin._printer.command_batches)
        plugin._sync_spoolmanager(False, True)
        self.assertEqual(batch_count, len(plugin._printer.command_batches))

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
        self.assertTrue(any(
            command.startswith('@RME FILAMENT SET slot=0 name=PLA-00C nozzle=215 preheat=175 bed=60 visible=1 tx=')
            for command in commands
        ))
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
            status="ready", progress=100, staged_path="/usb/FWUPD.BBF"
        )
        self.assertEqual(["M997 /usb/FWUPD.BBF"], plugin._printer.command_batches)
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

            def stat(self, remote_path):
                self.assert_remote_path = remote_path
                return {
                    "type": "file",
                    "size": os.path.getsize(self.upload[0]),
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
            self.assertEqual("FWUPD.RME", plugin._file_service.assert_remote_path)
            self.assertEqual("queued", plugin._file_service.status_before_start)
            self.assertEqual("ready", plugin._state["firmware"]["status"])
            self.assertEqual("/usb/FWUPD.RME", plugin._state["firmware"]["staged_path"])

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

    def test_uncertain_binary_transport_locks_commands_until_printer_reboot(self):
        plugin = RmeCompatibilityPlugin()
        plugin._printer = _Printer()
        plugin._logger = logging.getLogger("rme-reboot-lock-test")
        plugin._publish = lambda: None
        plugin._persist_and_publish = lambda: None
        plugin._defer = lambda callback, *args: callback(*args)
        plugin._state["firmware"].update(
            status="error", recovery_required=True,
            error="Printer reboot required",
        )

        with self.assertRaisesRegex(RuntimeError, "Printer reboot required"):
            plugin._send_command("@RME MACHINE QUERY")
        plugin.on_event("Connected", {})
        self.assertEqual([], plugin._printer.command_batches)
        self.assertEqual(1, plugin._printer.disconnect_calls)
        self.assertTrue(
            plugin._persistent_snapshot()["firmware"]["recovery_required"]
        )
        self.assertTrue(plugin._printer_transfer_active())

        plugin.gcode_received_hook(None, "start")

        self.assertFalse(plugin._state["firmware"]["recovery_required"])
        self.assertEqual(["@RME MACHINE QUERY"], plugin._printer.command_batches)

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
        self.assertIn("self.navbarTransfer = ko.pureComputed", javascript)
        self.assertIn("Firmware → printer", javascript)
        self.assertIn("Printer → Pi", javascript)
        self.assertIn("formatDurationLong", javascript)
        self.assertIn("formatDistance", javascript)
        self.assertIn("applyPersistentLights", javascript)
        self.assertIn("stageAndFlashFirmware", javascript)
        self.assertIn("unstageFirmware", javascript)
        self.assertIn("Filament resynchronization required", javascript)
        self.assertIn("MMU · idle", javascript)
        self.assertIn("deleteFirmware", javascript)
        self.assertIn('self.command("storage_download"', javascript)
        self.assertIn("storage/downloads/", javascript)
        self.assertIn("installFileManagerBridge", javascript)
        self.assertIn("Download to Pi", settings_template)
        self.assertIn("Download to device", settings_template)
        self.assertIn("storageDownloadWidth", settings_template)
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
            "record": "loaded_filament", "tool": 0, "material": "PLA-00D",
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
            'loaded_filament T2 S"PET-00L" O"Black" H"#000000" M"Polymaker"'
        )
        plugin._handle_record(record)

        self.assertEqual("Polymaker", plugin._state["loaded_filaments"][0]["vendor"])
        self.assertEqual("#000000", plugin._state["loaded_filaments"][0]["color"])
        self.assertEqual("PET-00L", accepted[0]["material"])

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

    def test_stage_reconciliation_never_promotes_an_unclaimed_usb_file(self):
        plugin = RmeCompatibilityPlugin()
        plugin._file_service = types.SimpleNamespace(
            stat=lambda path: {"type": "file", "size": 3921020}
        )
        plugin._persist_and_publish = lambda: None

        self.assertFalse(plugin._reconcile_firmware_stage())
        self.assertEqual("idle", plugin._state["firmware"]["status"])

        plugin._state["firmware"].update(status="ready", size=1)
        self.assertTrue(plugin._reconcile_firmware_stage())
        self.assertEqual("ready", plugin._state["firmware"]["status"])
        self.assertEqual(3921020, plugin._state["firmware"]["size"])

    def test_unstage_deletes_only_the_protected_stage_and_is_idempotent(self):
        class FileService(object):
            def __init__(self):
                self.present = True
                self.mutations = []

            def stat(self, path):
                if not self.present:
                    raise FileServiceError("Printer USB operation failed: not_found")
                return {"type": "file", "size": 1234}

            def mutate(self, action, path):
                self.mutations.append((action, path))
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
        self.assertEqual([("DELETE", "FWUPD.RME")], plugin._file_service.mutations)
        self.assertEqual("idle", plugin._state["firmware"]["status"])

        plugin._unstage_firmware()
        self.assertEqual([("DELETE", "FWUPD.RME")], plugin._file_service.mutations)

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
