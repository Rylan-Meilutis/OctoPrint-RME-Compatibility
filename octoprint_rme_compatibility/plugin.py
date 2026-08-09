"""OctoPrint integration for the Prusa Buddy RME serial protocol.

This module owns lifecycle, API, UI-state, and optional-plugin coordination.
Wire parsing and firmware transport live in smaller independently testable
modules so the serial receive hook stays fast and easy to audit.
"""

from __future__ import absolute_import

import copy
import functools
import os
import re
import shutil
import tempfile
import threading
import time

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.events import Events
from werkzeug.utils import secure_filename

from .file_service import FileServiceError, RmeFileService
from .protocol import (
    MAX_FIRMWARE_SIZE,
    classify_workflow,
    dialog_response_command,
    parse_line,
    toolmap_commands,
    workflow_is_terminal,
)
from .spoolmanager import (
    InternalSpoolBridge,
    SpoolManagerBridge,
    SpoolManagerUnavailable,
    SpoolmanBridge,
    spool_alias,
)
from .storage import StateStore
from .uploader import FirmwareUploader, UploadError, firmware_metadata


def api_errors(callback):
    """Translate expected command failures into useful Simple API responses."""
    @functools.wraps(callback)
    def wrapped(*args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except (UploadError, ValueError, RuntimeError) as exc:
            return flask.jsonify({"error": str(exc)}), 409

    return wrapped


class RmeCompatibilityPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.BlueprintPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    """Bridge OctoPrint state and controls to a connected RME Buddy printer."""
    def __init__(self):
        """Initialize only process-local state; OctoPrint injects services later."""
        self._state_lock = threading.RLock()
        self._state = self._empty_state()
        self._store = None
        self._uploader = None
        self._file_service = None
        self._firmware_file_thread = None
        self._stop = threading.Event()
        self._keepalive_thread = None
        self._firmware_directory = None
        self._last_fw_publish = 0
        self._spoolmanager = None
        self._spool_sync_lock = threading.Lock()
        self._last_spool_sync = 0
        self._expected_provider_events = {}
        self._expected_spoolman_event_until = 0
        self._toolmap_hold_active = False
        self._toolmap_timer_generation = 0
        self._preflight_gate_started = False
        self._print_job_gcode_sent = False
        self._skip_cancel_script = False
        self._stats_supported = None
        self._last_stats_poll = 0
        self._priority_controls_sent = set()
        self._firmware_completed_controls = set()
        self._firmware_action_lock = threading.Lock()
        self._last_storage_publish = 0

    @staticmethod
    def _empty_state():
        """Build a complete JSON-safe snapshot for the UI and persistence layer."""
        return {
            "connected": False,
            "supported": False,
            "session": {"active": False, "legacy": True, "last_seq": 0},
            "machine": {},
            "toolmap": {"enabled": False, "mapping": {}},
            "lock": {},
            "theme": {},
            "light": {},
            "filaments": [],
            # M865 loadout metadata is keyed by the firmware's physical tool
            # index. ``active_tool`` combines it with the current logical tool
            # and RME remapping for a browser-ready status indicator.
            "loaded_filaments": [],
            "active_tool": {
                "logical": None,
                "physical": None,
                "material": None,
                "color_name": None,
                "color": None,
                "updated": None,
            },
            "internal_spools": {"next_id": 1, "inventory": [], "selected": {}},
            "stats": {"supported": None, "updated": None, "values": {}},
            "spoolmanager": {
                "available": False,
                "provider": None,
                "status": "not checked",
                "inventory": [],
                "published": [],
                "selected": [],
                "pending_new": None,
                "pending_provider_sync": None,
                "last_sync": None,
                "error": None,
            },
            "workflow": None,
            "prompt": None,
            "firmware": {
                "status": "idle",
                "filename": None,
                "size": 0,
                "sha256": None,
                "offset": 0,
                "progress": 0,
                "error": None,
                "staged_path": None,
                "flash_after_stage": False,
            },
            "storage": {
                "supported": False, "caps": {}, "path": "/", "entries": [],
                "status": "not checked", "progress": None, "error": None,
                "updated": None,
            },
            "errors": [],
        }

    # -- OctoPrint lifecycle -------------------------------------------------

    def on_after_startup(self):
        """Restore durable UI state and start non-serial background workers."""
        data_folder = self.get_plugin_data_folder()
        self._firmware_directory = os.path.join(data_folder, "firmware")
        os.makedirs(self._firmware_directory, exist_ok=True)
        self._store = StateStore(
            os.path.join(data_folder, "state.json"), self._persistent_snapshot, self._logger
        )
        persisted = self._store.load()
        with self._state_lock:
            for key in (
                "machine", "toolmap", "workflow", "prompt", "firmware",
                "spoolmanager", "loaded_filaments", "active_tool", "internal_spools",
                "stats",
            ):
                if key in persisted:
                    self._state[key] = persisted[key]
            if self._state["firmware"].get("status") in (
                "starting",
                "uploading",
                "verifying",
            ):
                self._state["firmware"].update(
                    status="error", error="OctoPrint restarted during the firmware transfer"
                )
            # A one-click flash request is deliberately process-local. Never
            # carry a bootloader handoff intent across an OctoPrint restart.
            self._state["firmware"]["flash_after_stage"] = False
        self._store.start()
        self._uploader = FirmwareUploader(
            self._send_command, self._firmware_state_changed, self._logger
        )
        self._file_service = RmeFileService(self._send_command, self._logger)
        self._spoolmanager_bridge = SpoolManagerBridge(self._plugin_manager, self._logger)
        self._spoolman_bridge = SpoolmanBridge(self._plugin_manager, self._logger)
        self._internal_spool_bridge = InternalSpoolBridge(
            self._state, self._state_lock, self._logger
        )
        self._spoolmanager, _ = self._resolve_spool_provider()
        self._stop.clear()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="rme-keepalive", daemon=True
        )
        self._keepalive_thread.start()
        self._defer(self._sync_spoolmanager, True)
        self._logger.info("RME Compatibility initialized")

    def on_shutdown(self):
        # A disabled/reloaded plugin must never strand OctoPrint's counted job
        # hold. In-flight prints remain under OctoPrint's normal control.
        self._release_toolmap_hold()
        self._stop.set()
        if self._uploader and self._uploader.busy:
            self._uploader.cancel()
        if self._file_service:
            with self._state_lock:
                firmware_transfer = self._state["firmware"].get("status") in (
                    "starting", "uploading", "verifying"
                )
            if firmware_transfer:
                self._file_service.cancel()
            self._file_service.reset("OctoPrint is shutting down")
        if self._keepalive_thread:
            self._keepalive_thread.join(timeout=2)
        if self._store:
            self._store.stop()

    def get_settings_defaults(self):
        return {
            "auto_open_session": True,
            "legacy_notifications": False,
            "auto_machine_profile": True,
            "prompt_toolmap_on_print": True,
            "default_toolmap": {},
            "default_toolmap_enabled": True,
            "toolmap_timeout_seconds": 120,
            "spoolmanager_enabled": True,
            "spool_provider": "auto",
            "spoolmanager_sync_interval": 30,
            "spoolmanager_default_weight": 1000,
            "spoolmanager_default_diameter": 1.75,
            "spoolmanager_default_density": 1.24,
            "stats_poll_interval": 30,
        }

    def get_assets(self):
        return {
            "js": ["js/rme_compatibility.js"],
            "css": ["css/rme_compatibility.css"],
        }

    def get_template_configs(self):
        return [
            {
                "type": "navbar",
                "template": "rme_compatibility_navbar.jinja2",
                "custom_bindings": True,
                "classes": ["dropdown", "rme-navbar"],
                "styles": ["display: none"],
                "data_bind": "visible: navbarVisible",
            },
            {"type": "tab", "name": "RME", "custom_bindings": True},
            {"type": "settings", "name": "RME Compatibility", "custom_bindings": True},
        ]

    def get_update_information(self):
        """Expose stable and beta release channels to OctoPrint Software Update.

        Stable accepts releases targeting ``main``. Beta additionally accepts
        GitHub prereleases targeting ``beta``, allowing development builds to
        be tested without offering them to stable installations.
        """
        return {
            "rme_compatibility": {
                "displayName": "RME Compatibility",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "Rylan-Meilutis",
                "repo": "OctoPrint-RME-Compatibility",
                "current": self._plugin_version,
                # Compare the full PEP 440 version so b1, b2, and later beta
                # builds are not collapsed to the same 0.1.0 base release.
                "release_compare": "python",
                "force_base": False,
                # The plugin registers assets, blueprints, serial hooks, and
                # background services that must be initialized by the server.
                "restart": "octoprint",
                "stable_branch": {
                    "name": "Stable",
                    "branch": "main",
                    "commitish": ["main"],
                },
                "prerelease_branches": [
                    {
                        "name": "Beta",
                        "branch": "beta",
                        "commitish": ["beta", "main"],
                    }
                ],
                "pip": (
                    "https://github.com/Rylan-Meilutis/OctoPrint-RME-Compatibility/"
                    "archive/{target_version}.zip"
                ),
            }
        }

    def is_api_protected(self):
        return True

    def is_blueprint_csrf_protected(self):
        return True

    def get_api_commands(self):
        return {
            "discover": [],
            "open_session": [],
            "query_dialog": [],
            "respond": ["action"],
            "stuck": ["action"],
            "apply_toolmap": ["mapping", "enabled"],
            "reset_toolmap": [],
            "apply_machine_profile": [],
            "query_controls": [],
            "ui_control": ["action", "value"],
            "lock_now": [],
            "lock_unlock": ["pin"],
            "set_lock": ["pin", "timeout", "serial", "enabled"],
            "set_theme": ["colors"],
            "set_temp_lights": ["screen", "chamber", "status"],
            "set_persistent_lights": ["screen", "chamber", "status"],
            "set_filament": ["slot", "name", "nozzle", "preheat", "bed", "visible"],
            "stage_firmware": ["filename"],
            "stage_and_flash_firmware": ["filename"],
            "cancel_firmware": [],
            "flash_firmware": [],
            "delete_firmware": ["filename"],
            "sync_spoolmanager": [],
            "sync_filaments_from_printer": [],
            "sync_filaments_to_printer": [],
            "confirm_provider_sync": [],
            "cancel_provider_sync": [],
            "select_spool": ["tool", "database_id"],
            "deselect_spool": ["tool"],
            "begin_new_spool": ["tool"],
            "create_spool": ["display_name", "material", "color", "total_weight"],
            "cancel_new_spool": [],
            "touch_toolmap": [],
            "storage_caps": [],
            "storage_list": ["path"],
            "storage_mkdir": ["path"],
            "storage_rename": ["path", "destination"],
            "storage_delete": ["path"],
            "storage_print": ["path"],
            "storage_flash": ["path"],
        }

    def on_api_get(self, request):
        if not Permissions.STATUS.can():
            flask.abort(403)
        return flask.jsonify(self._public_state())

    @api_errors
    def on_api_command(self, command, data):
        if not Permissions.CONTROL.can():
            flask.abort(403)
        if command == "discover":
            self._send_command("@RME MACHINE QUERY")
        elif command == "open_session":
            self._open_session()
        elif command == "query_dialog":
            self._send_command("@RME DIALOG QUERY")
        elif command == "respond":
            self._send_command(dialog_response_command(data["action"]))
            self._send_command("@RME DIALOG QUERY")
        elif command == "stuck":
            action = str(data["action"]).upper()
            if action not in ("CONTINUE", "UNLOAD", "ABORT", "QUERY"):
                flask.abort(400, description="Invalid stuck-filament action")
            self._send_command("@RME STUCK %s" % action)
        elif command == "apply_toolmap":
            self._apply_toolmap(data["mapping"], bool(data["enabled"]), release_hold=True)
        elif command == "reset_toolmap":
            self._send_commands(["@RME TOOLMAP RESET", "@RME TOOLMAP QUERY"])
            self._configure_validator_mapping({})
            self._release_toolmap_hold()
        elif command == "apply_machine_profile":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            self._apply_machine_profile()
        elif command == "query_controls":
            self._send_commands(
                [
                    "@RME LOCK QUERY",
                    "@RME THEME QUERY",
                    "@RME LIGHT QUERY",
                    "@RME FILAMENT QUERY",
                ]
            )
        elif command == "ui_control":
            action = str(data["action"]).upper()
            value = int(data.get("value", 0))
            if action == "ENCODER":
                if value == 0 or value < -100 or value > 100:
                    flask.abort(400, description="Encoder movement must be -100 through 100, excluding zero")
                frame = "@RME UI ENCODER %d" % value
            elif action in ("CLICK", "BACK", "HOME"):
                frame = "@RME UI %s" % action
            else:
                flask.abort(400, description="Invalid remote UI action")
            self._send_commands(["@RME UI ENABLE 1", frame])
        elif command == "lock_now":
            self._send_command("@RME LOCK NOW")
            self._send_command("@RME LOCK QUERY")
        elif command == "lock_unlock":
            pin = str(data["pin"])
            if not pin.isdigit() or len(pin) < 4 or len(pin) > 9:
                flask.abort(400, description="PIN must contain 4 through 9 digits")
            self._send_command("@RME LOCK UNLOCK pin=%s digits=%d" % (pin, len(pin)))
            self._send_command("@RME LOCK QUERY")
        elif command == "set_lock":
            pin = str(data["pin"])
            timeout = int(data["timeout"])
            serial = 1 if bool(data["serial"]) else 0
            enabled = 1 if bool(data["enabled"]) else 0
            if not pin.isdigit() or len(pin) < 4 or len(pin) > 9:
                flask.abort(400, description="PIN must contain 4 through 9 digits")
            if timeout < 0 or timeout > 65535:
                flask.abort(400, description="Lock timeout must be 0 through 65535 seconds")
            self._send_command(
                "@RME LOCK SET pin=%s digits=%d timeout=%d serial=%d enabled=%d"
                % (pin, len(pin), timeout, serial, enabled)
            )
            self._send_command("@RME LOCK QUERY")
        elif command == "set_theme":
            colors = data["colors"]
            keys = ("primary", "progress", "warning", "error", "image")
            if not isinstance(colors, dict) or any(
                key not in colors or not self._valid_color(colors[key]) for key in keys
            ):
                flask.abort(400, description="All theme colors must use #RRGGBB")
            self._send_command(
                "@RME THEME SET " + " ".join("%s=%s" % (key, colors[key]) for key in keys)
            )
            self._send_command("@RME THEME QUERY")
        elif command == "set_temp_lights":
            values = [int(data[key]) for key in ("screen", "chamber", "status")]
            if any(value < 0 or value > 100 for value in values):
                flask.abort(400, description="Light brightness must be 0 through 100")
            self._send_command(
                "@RME LIGHT TEMP screen=%d chamber=%d status=%d" % tuple(values)
            )
            self._send_command("@RME LIGHT QUERY")
        elif command == "set_persistent_lights":
            values = [int(data[key]) for key in ("screen", "chamber", "status")]
            if any(not self._valid_packed_brightness(value) for value in values):
                flask.abort(400, description="Each packed light-state byte must be 0 through 100")
            self._send_command(
                "@RME LIGHT SET screen=0x%08X chamber=0x%08X status=0x%08X" % tuple(values)
            )
            self._send_command("@RME LIGHT QUERY")
        elif command == "set_filament":
            slot = int(data["slot"])
            name = str(data["name"])
            temperatures = [int(data[key]) for key in ("nozzle", "preheat", "bed")]
            visible = 1 if bool(data["visible"]) else 0
            if slot < 0 or slot > 7 or not name or len(name) > 7 or " " in name:
                flask.abort(400, description="Filament needs slot 0-7 and a 1-7 character name without spaces")
            if any(value < 0 or value > 500 for value in temperatures):
                flask.abort(400, description="Filament temperatures must be 0 through 500 C")
            self._send_command(
                "@RME FILAMENT SET slot=%d name=%s nozzle=%d preheat=%d bed=%d visible=%d"
                % (slot, name, temperatures[0], temperatures[1], temperatures[2], visible)
            )
            self._send_command("@RME FILAMENT QUERY")
        elif command == "stage_firmware":
            self._start_firmware_upload(data["filename"])
        elif command == "stage_and_flash_firmware":
            self._start_firmware_upload(data["filename"], flash_after_stage=True)
        elif command == "cancel_firmware":
            if self._uploader:
                self._uploader.cancel()
            with self._state_lock:
                file_firmware_active = self._state["firmware"].get("status") in (
                    "starting", "uploading", "verifying"
                )
            if file_firmware_active and self._file_service and self._file_service.busy:
                self._file_service.cancel()
        elif command == "flash_firmware":
            self._flash_firmware()
        elif command == "delete_firmware":
            self._delete_firmware(data["filename"])
        elif command == "sync_spoolmanager":
            self._sync_filaments_to_printer()
        elif command == "sync_filaments_from_printer":
            self._sync_filaments_from_printer()
        elif command in ("sync_filaments_to_printer", "confirm_provider_sync"):
            self._sync_filaments_to_printer()
        elif command == "cancel_provider_sync":
            self._sync_filaments_from_printer()
        elif command == "select_spool":
            self._select_spool_from_octoprint(data["tool"], data["database_id"])
        elif command == "deselect_spool":
            self._deselect_spool_from_octoprint(data["tool"])
        elif command == "begin_new_spool":
            self._begin_new_spool(data["tool"])
        elif command == "create_spool":
            self._create_spool(data)
        elif command == "cancel_new_spool":
            with self._state_lock:
                self._state["spoolmanager"]["pending_new"] = None
            self._persist_and_publish()
        elif command == "touch_toolmap":
            self._pause_toolmap_timeout()
        elif command == "storage_caps":
            self._initialize_storage()
        elif command == "storage_list":
            self._refresh_storage(data["path"])
        elif command == "storage_mkdir":
            if not getattr(Permissions, "FILES_UPLOAD", Permissions.CONTROL).can():
                flask.abort(403)
            self._storage_mutation("MKDIR", data["path"])
        elif command == "storage_rename":
            if not getattr(Permissions, "FILES_DELETE", Permissions.CONTROL).can():
                flask.abort(403)
            self._storage_mutation("RENAME", data["path"], data["destination"])
        elif command == "storage_delete":
            if not getattr(Permissions, "FILES_DELETE", Permissions.CONTROL).can():
                flask.abort(403)
            self._storage_mutation("DELETE", data["path"])
        elif command == "storage_print":
            if not getattr(Permissions, "PRINT", Permissions.CONTROL).can():
                flask.abort(403)
            self._storage_mutation("PRINT", data["path"])
        elif command == "storage_flash":
            self._storage_mutation("FLASH", data["path"])
        return flask.jsonify(self._public_state())

    @octoprint.plugin.BlueprintPlugin.route("/selected-spools", methods=["GET"])
    @octoprint.plugin.BlueprintPlugin.route("/filament-report", methods=["GET"])
    def filament_report(self):
        """Expose a stable, read-only loadout document for slicer polling.

        OrcaSlicer or another authenticated OctoPrint client can poll this URL
        with its normal API key. It intentionally contains no mutation surface.
        """
        if not Permissions.STATUS.can():
            flask.abort(403)
        return flask.jsonify(self._filament_report())

    @octoprint.plugin.BlueprintPlugin.route("/storage/download", methods=["GET"])
    @api_errors
    def download_storage_file(self):
        """Stream one printer USB file through authenticated OctoPrint."""
        permission = getattr(Permissions, "FILES_DOWNLOAD", Permissions.STATUS)
        if not permission.can():
            flask.abort(403)
        path = flask.request.args.get("path", "")
        self._require_storage()
        metadata = self._file_service.stat(path)
        if metadata.get("type") != "file":
            raise FileServiceError("Only files can be downloaded")
        filename = secure_filename(os.path.basename(str(path).rstrip("/"))) or "download.bin"
        response = flask.Response(
            flask.stream_with_context(self._file_service.iter_file(path)),
            mimetype="application/octet-stream",
        )
        response.headers["Content-Length"] = str(int(metadata.get("size", 0)))
        response.headers["Content-Disposition"] = 'attachment; filename="%s"' % filename
        response.headers["Cache-Control"] = "no-store"
        return response

    @octoprint.plugin.BlueprintPlugin.route("/storage/upload", methods=["POST"])
    @api_errors
    def upload_storage_file(self):
        """Upload a browser file to USB using verified, atomic RME writes."""
        if not Permissions.CONTROL.can() or not getattr(
            Permissions, "FILES_UPLOAD", Permissions.CONTROL
        ).can():
            flask.abort(403)
        self._require_storage()
        uploaded = flask.request.files.get("file")
        path_suffix = self._settings.global_get(["server", "uploads", "pathSuffix"]) or "path"
        name_suffix = self._settings.global_get(["server", "uploads", "nameSuffix"]) or "name"
        spooled_path = flask.request.values.get("file." + path_suffix)
        original_name = uploaded.filename if uploaded is not None else flask.request.values.get("file." + name_suffix)
        directory = flask.request.values.get("path", "/")
        if not original_name or (uploaded is None and not spooled_path):
            return flask.jsonify({"error": "A file is required"}), 400
        filename = os.path.basename(str(original_name).replace("\\", "/"))
        if filename in ("", ".", ".."):
            return flask.jsonify({"error": "The upload filename is invalid"}), 400
        remote_path = self._join_storage_path(directory, filename)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".rme-storage-", suffix=".tmp", dir=self.get_plugin_data_folder()
        )
        os.close(descriptor)
        try:
            if uploaded is not None:
                uploaded.save(temporary)
            else:
                shutil.copyfile(spooled_path, temporary)
            maximum = int(self._state["storage"].get("caps", {}).get("max_size", 1024 ** 3))
            size = os.path.getsize(temporary)
            if size > maximum:
                return flask.jsonify({"error": "File exceeds the printer USB limit"}), 413
            self._set_storage_status("uploading", progress=0, error=None)
            try:
                self._file_service.write_file(temporary, remote_path, self._storage_progress)
                self._refresh_storage(directory)
            except Exception as exc:
                self._set_storage_status("error", progress=None, error=str(exc))
                raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return flask.jsonify(self._public_state())

    # A separate multipart route is needed for browser-to-Pi BBF upload.
    @octoprint.plugin.BlueprintPlugin.route("/firmware", methods=["POST"])
    def upload_firmware(self):
        """Store one browser-uploaded BBF and return machine-readable errors."""
        if not Permissions.CONTROL.can():
            flask.abort(403)
        uploaded = flask.request.files.get("file")
        upload_path_suffix = self._settings.global_get(
            ["server", "uploads", "pathSuffix"]
        ) or "path"
        upload_name_suffix = self._settings.global_get(
            ["server", "uploads", "nameSuffix"]
        ) or "name"
        spooled_path = flask.request.values.get("file." + upload_path_suffix)
        spooled_name = flask.request.values.get("file." + upload_name_suffix)

        # OctoPrint's UploadStorageFallbackHandler replaces larger multipart
        # files with trusted file.path/file.name fields and removes the entry
        # from request.files. Reserved-field protection in that handler keeps
        # clients from forging an arbitrary server-side path.
        original_name = uploaded.filename if uploaded is not None else spooled_name
        if not original_name or (uploaded is None and not spooled_path):
            return flask.jsonify({"error": "A .bbf file is required"}), 400
        filename = secure_filename(original_name)
        if not filename or not filename.lower().endswith(".bbf"):
            return flask.jsonify({"error": "Only .bbf firmware files are accepted"}), 400
        destination = self._firmware_path(filename)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".rme-upload-", suffix=".tmp", dir=self._firmware_directory
        )
        os.close(descriptor)
        try:
            if uploaded is not None:
                uploaded.save(temporary)
            else:
                try:
                    shutil.copyfile(spooled_path, temporary)
                except (OSError, TypeError) as exc:
                    self._logger.warning("Could not read OctoPrint-spooled BBF: %s", exc)
                    return flask.jsonify(
                        {"error": "OctoPrint could not read the temporary BBF upload"}
                    ), 400
            size = os.path.getsize(temporary)
            if size <= 0 or size > MAX_FIRMWARE_SIZE:
                return flask.jsonify(
                    {"error": "Firmware must be between 1 byte and 32 MiB"}
                ), 413
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return flask.jsonify({"file": firmware_metadata(destination), "state": self._public_state()})

    def bodysize_hook(self, current_max_body_sizes, *args, **kwargs):
        # OctoPrint prefixes plugin hook routes with /plugin/<identifier>/.
        # Return only the blueprint-relative path or the prefix is duplicated
        # and Tornado rejects larger uploads against its default body limit.
        return [
            ("POST", r"/firmware", 33 * 1024 * 1024),
            ("POST", r"/storage/upload", 1025 * 1024 * 1024),
        ]

    # -- Events and serial receive path -------------------------------------

    def on_event(self, event, payload):
        """Handle core lifecycle plus SpoolManager's documented event names."""
        normalized_event = str(event).lower()
        if normalized_event.startswith("plugin_spoolmanager_"):
            self._defer(self._handle_spoolmanager_event, normalized_event, payload or {})
            return
        if normalized_event.startswith("plugin_spoolman_"):
            self._defer(self._handle_spoolman_event)
            return
        if event == Events.CONNECTED:
            with self._state_lock:
                self._state["connected"] = True
                self._state["supported"] = False
                self._state["session"] = {"active": False, "legacy": True, "last_seq": 0}
                self._stats_supported = None
                self._last_stats_poll = 0
                self._priority_controls_sent.clear()
                self._firmware_completed_controls.clear()
                self._state["stats"] = {"supported": None, "updated": None, "values": {}}
                # Never display a previous printer's tool/loadout as current
                # while capability discovery for this connection is pending.
                self._state["loaded_filaments"] = []
                self._state["active_tool"] = self._empty_state()["active_tool"]
            self._publish()
            self._send_command("@RME MACHINE QUERY")
            self._defer(self._sync_spoolmanager, True)
        elif event in (Events.DISCONNECTING, Events.DISCONNECTED):
            if event == Events.DISCONNECTING and self._state.get("supported"):
                try:
                    self._send_command("@RME SESSION CLOSE")
                except Exception:
                    pass
            if self._file_service:
                self._file_service.reset("Printer disconnected")
            with self._state_lock:
                self._state["connected"] = False
                self._state["supported"] = False
                self._state["session"]["active"] = False
                self._state["storage"].update(
                    supported=False, status="printer disconnected", progress=None
                )
            self._publish()
        elif event == Events.PRINT_STARTED:
            with self._state_lock:
                self._priority_controls_sent.clear()
                self._firmware_completed_controls.clear()
            self._handle_print_started(payload)
        elif event == Events.PRINT_CANCELLING:
            # Cancellation preparation is queued as part of the job, so the
            # mapping hold must be released before OctoPrint can process it.
            with self._state_lock:
                if self._preflight_gate_started and not self._print_job_gcode_sent:
                    self._skip_cancel_script = True
            self._release_toolmap_hold()
            self._request_priority_control("cancel")
        elif event == Events.PRINT_PAUSED:
            # Fallback for pause configurations that do not enqueue a tagged
            # preparation command before transitioning into PAUSED.
            if not self._consume_firmware_completed_control("pause"):
                self._request_priority_control("pause")
        elif event == Events.PRINT_RESUMED:
            if not self._consume_firmware_completed_control("resume"):
                self._request_priority_control("resume")
        elif event in (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED):
            self._release_toolmap_hold()
            with self._state_lock:
                self._priority_controls_sent.clear()
                self._firmware_completed_controls.clear()

    def gcode_received_hook(self, comm_instance, line, *args, **kwargs):
        """Observe RME records without blocking or bypassing OctoPrint's queue."""
        record = parse_line(line)
        if self._uploader:
            self._uploader.handle_response(line, record)
        if self._file_service:
            self._file_service.handle_response(record)
        if record:
            self._handle_record(record)
            # M998 reports transaction failures with Marlin's Error prefix, but
            # they are recoverable transfer failures rather than printer safety
            # faults. The uploader has consumed the record; keep OctoPrint from
            # treating this one namespaced error as a fatal firmware condition.
            if record.get("record") == "upload_error" and line.strip().startswith("Error:"):
                return "echo:" + line.strip()[len("Error:") :].lstrip()
        return line

    def action_command_hook(self, comm_instance, line, action, *args, **kwargs):
        """Remember firmware-completed pause states to avoid echoing them back.

        Buddy reports both requests (``pause``/``resume``) and completed state
        (``paused``/``resumed``). Only requests need a service command from the
        host; completion events merely synchronize OctoPrint's state.
        """
        name = str(kwargs.get("name") or action or "").strip().lower()
        completed = {"paused": "pause", "resumed": "resume"}.get(name)
        if completed:
            with self._state_lock:
                if self._state.get("supported"):
                    self._firmware_completed_controls.add(completed)

    def atcommand_sending_hook(
        self, comm_instance, phase, command, parameters, *args, **kwargs
    ):
        """Forward OctoPrint's reserved ``@RME`` host command to firmware.

        OctoPrint consumes all at-commands locally and normally skips their
        serial write. Registering this sending-phase hook is therefore required
        for discovery itself as well as every later RME session command. The
        command still travels through OctoPrint's single serialized writer; no
        competing serial descriptor is opened.
        """
        if phase != "sending" or str(command).upper() != "RME":
            return
        parameters = str(parameters or "").strip()
        if "\r" in parameters or "\n" in parameters:
            self._logger.warning("Refusing multiline @RME command")
            return
        full_command = "@RME" + ((" " + parameters) if parameters else "")
        do_send = getattr(comm_instance, "_do_send", None)
        if not callable(do_send):
            self._logger.error(
                "OctoPrint does not expose its serialized send primitive; "
                "%s was not transmitted", full_command
            )
            return
        do_send(full_command, gcode=None)

    def _consume_firmware_completed_control(self, action):
        with self._state_lock:
            if action not in self._firmware_completed_controls:
                return False
            self._firmware_completed_controls.discard(action)
            return True

    def gcode_script_hook(self, comm_instance, script_type, script_name, *args, **kwargs):
        """Acquire the mapping hold at OctoPrint's synchronous start boundary.

        ``PrintStarted`` is dispatched asynchronously and can race the serial
        send loop. ``beforePrintStarted`` is rendered synchronously before that
        loop starts, including when another plugin calls ``start_print``.
        """
        if script_type == "gcode" and script_name == "beforePrintStarted":
            with self._state_lock:
                self._preflight_gate_started = True
                self._print_job_gcode_sent = False
                self._skip_cancel_script = False
            self._prepare_toolmap_prompt()
        elif script_type == "gcode" and script_name == "afterPrintCancelled":
            with self._state_lock:
                if self._preflight_gate_started and not self._print_job_gcode_sent:
                    self._skip_cancel_script = True
        return None

    def gcode_queuing_hook(self, comm_instance, phase, cmd, cmd_type, gcode,
                           subcode=None, tags=None, *args, **kwargs):
        """Handle the preflight cancel macro and RME out-of-band controls.

        OctoPrint tags its pause/resume/cancel preparation commands. Detecting
        those tags at the queue boundary lets the matching RME command jump
        the host queue before a blocking heater, probe, or MMU command returns.
        Explicit M601/M602/M604 commands are rerouted the same way.
        """
        tags = tags or kwargs.get("tags") or set()
        with self._state_lock:
            skip = self._skip_cancel_script and "script:afterPrintCancelled" in tags
            supported = self._state.get("supported", False)
        if skip:
            return (None,)
        if not supported:
            return None
        if "rme:priority_control" in tags:
            return self._force_send_rme_control(comm_instance, cmd, gcode)

        tagged_action = next((
            action for action in ("cancel", "pause", "resume")
            if "trigger:" + action in tags
        ), None)
        command_actions = {"M601": "pause", "M602": "resume", "M604": "cancel"}
        command_action = command_actions.get(str(gcode or "").upper())
        action = command_action or tagged_action
        if action:
            self._request_priority_control(action)
        # An explicit service command is resent with force=True below; leaving
        # its original copy queued would execute the action twice later.
        return (None,) if command_action else None

    def gcode_sent_hook(self, comm_instance, phase, cmd, cmd_type, gcode,
                        subcode=None, tags=None, *args, **kwargs):
        """Record job transmission and the latest tool selected by G-code.

        Buddy's M865 query reports filament metadata but not the currently
        selected tool. Tracking an actually transmitted ``Tn`` command is the
        same source OctoPrint uses for streamed MMU/toolchanger jobs and avoids
        claiming that a merely queued command is already active.
        """
        tags = tags or kwargs.get("tags") or set()
        is_job_command = "source:job" in tags or "source:file" in tags
        is_cancel_command = "trigger:cancel" in tags or "trigger:comm.cancel" in tags
        if is_job_command and not is_cancel_command:
            with self._state_lock:
                self._print_job_gcode_sent = True
        tool_match = re.match(r"^\s*T(\d+)(?:\s|$)", str(cmd or ""), re.IGNORECASE)
        with self._state_lock:
            supported = self._state.get("supported", False)
        if supported and tool_match:
            self._set_active_tool(int(tool_match.group(1)))

    def _handle_record(self, record):
        """Fold one parsed firmware record into the authoritative UI state."""
        kind = record["record"]
        if kind.startswith("upload_") or kind.startswith("file_"):
            return
        follow_up = []
        apply_profile = False
        with self._state_lock:
            if kind == "machine":
                self._state["supported"] = True
                self._state["machine"].update(record)
                # Build the provider alias table before M865 Q so printer-side
                # selections can be resolved without first overwriting them.
                follow_up.append("initialize_spool_sync")
                follow_up.append("initialize_storage")
                follow_up.append("@RME STATS QUERY")
                if (
                    int(record.get("logical_tools", 0)) == 1
                    and self._state["active_tool"].get("logical") is None
                ):
                    self._state["active_tool"]["logical"] = 0
                    self._refresh_active_tool_locked()
                if self._settings.get_boolean(["auto_open_session"]):
                    follow_up.append("open_session")
            elif kind in ("envelope", "limits"):
                self._state["machine"].update(record)
                if kind == "limits":
                    apply_profile = self._settings.get_boolean(["auto_machine_profile"])
                    follow_up.append("@RME TOOLMAP QUERY")
            elif kind == "session":
                self._state["session"].update(
                    active=bool(record.get("active")), legacy=bool(record.get("legacy"))
                )
                if record.get("active"):
                    follow_up.extend(
                        [
                            "@RME DIALOG QUERY",
                            "@RME TOOLMAP QUERY",
                            "@RME LOCK QUERY",
                            "@RME THEME QUERY",
                            "@RME LIGHT QUERY",
                            "@RME FILAMENT QUERY",
                        ]
                    )
            elif kind == "event":
                now = int(time.time())
                record["workflow"] = classify_workflow(record)
                previous_workflow = self._state.get("workflow") or {}
                record["received_at"] = now
                if previous_workflow.get("workflow") == record.get("workflow"):
                    record["phase_started_at"] = previous_workflow.get("phase_started_at", now)
                else:
                    record["phase_started_at"] = now
                sequence = int(record.get("seq", 0))
                previous = int(self._state["session"].get("last_seq", 0))
                if previous and sequence != previous + 1:
                    follow_up.extend(["@RME SESSION QUERY", "@RME DIALOG QUERY"])
                self._state["session"]["last_seq"] = sequence
                self._state["workflow"] = dict(record)
                if record.get("type") == "error" or record.get("state") == "waiting":
                    follow_up.append("@RME DIALOG QUERY")
                if workflow_is_terminal(record):
                    if (self._state.get("prompt") or {}).get("kind") == "firmware":
                        self._state["prompt"] = None
                    # Loading dialogs can change M865 without originating on the
                    # serial host. Query after their terminal event so an LCD-side
                    # choice immediately propagates back to SpoolManager.
                    follow_up.append("M865 Q")
            elif kind == "prompt":
                if record["actions"]:
                    workflow = self._state.get("workflow") or {}
                    self._state["prompt"] = {
                        "kind": "firmware",
                        "actions": record["actions"],
                        "workflow": workflow.get("workflow", "printer"),
                        "message": workflow.get("message", "Printer action required"),
                        "updated": int(time.time()),
                    }
                elif (self._state.get("prompt") or {}).get("kind") == "firmware":
                    self._state["prompt"] = None
            elif kind == "toolmap":
                self._state["toolmap"] = {
                    "enabled": record["enabled"],
                    "mapping": record["mapping"],
                }
                self._refresh_active_tool_locked()
            elif kind in ("lock", "theme", "light"):
                self._state[kind] = {
                    key: value for key, value in record.items() if key != "record"
                }
            elif kind == "stats":
                self._stats_supported = True
                self._last_stats_poll = time.monotonic()
                values = dict(self._state.get("stats", {}).get("values") or {})
                values.update({
                    key: value for key, value in record.items() if key != "record"
                })
                self._state["stats"] = {
                    "supported": True,
                    "updated": int(time.time()),
                    "values": values,
                }
            elif kind == "filament":
                filament = {key: value for key, value in record.items() if key != "record"}
                identity = (filament.get("user"), filament.get("slot"))
                existing = self._state["filaments"]
                existing[:] = [
                    item for item in existing
                    if (item.get("user"), item.get("slot")) != identity
                ]
                existing.append(filament)
                existing.sort(key=lambda item: (int(item.get("user", 0)), int(item.get("slot", 0))))
            elif kind == "loaded_filament":
                loadout = {
                    key: value for key, value in record.items() if key != "record"
                }
                existing = self._state["loaded_filaments"]
                existing[:] = [
                    item for item in existing
                    if int(item.get("tool", -1)) != int(loadout["tool"])
                ]
                existing.append(loadout)
                existing.sort(key=lambda item: int(item["tool"]))
                self._refresh_active_tool_locked()
                self._defer(self._accept_firmware_spool, dict(record))
            elif kind == "rme_error":
                if "stats" in record["message"].lower():
                    self._stats_supported = False
                    self._state["stats"]["supported"] = False
                errors = self._state["errors"]
                errors.append({"message": record["message"], "time": int(time.time())})
                del errors[:-10]
        # The receive hook must remain memory-only; persistence and websocket
        # publication happen after returning control to OctoPrint's RX loop.
        self._defer(self._persist_and_publish)
        for item in follow_up:
            if item == "open_session":
                self._defer(self._open_session)
            elif item == "sync_spoolmanager":
                self._defer(self._sync_spoolmanager, True)
            elif item == "sync_spoolmanager_inventory":
                self._defer(self._sync_spoolmanager, True, False)
            elif item == "initialize_spool_sync":
                self._defer(self._initialize_spool_sync)
            elif item == "initialize_storage":
                self._defer(self._initialize_storage)
            else:
                self._defer(self._send_command, item)
        if apply_profile:
            self._defer(self._apply_machine_profile)

    # -- Protocol actions ---------------------------------------------------

    def _send_command(self, command):
        if not self._printer.is_operational():
            raise RuntimeError("Printer is not connected")
        self._printer.commands(command, tags={"plugin:rme_compatibility"})

    def _send_commands(self, commands):
        if not self._printer.is_operational():
            raise RuntimeError("Printer is not connected")
        self._printer.commands(commands, tags={"plugin:rme_compatibility"})

    def _request_priority_control(self, action):
        """Queue one idempotent pause/resume/cancel on OctoPrint's fast path."""
        commands = {"pause": "M601", "resume": "M602", "cancel": "M604"}
        if action not in commands:
            raise ValueError("Unknown priority control action: %s" % action)
        with self._state_lock:
            if not (self._state.get("connected") and self._state.get("supported")):
                return False
            # Each resume permits a later pause and vice versa. Cancel remains
            # latched until the job ends so duplicate API/events are harmless.
            if action == "pause":
                self._priority_controls_sent.discard("resume")
            elif action == "resume":
                self._priority_controls_sent.discard("pause")
            if action in self._priority_controls_sent:
                return False
            self._priority_controls_sent.add(action)
        self._defer(self._send_priority_control, action, commands[action])
        return True

    def _send_priority_control(self, action, command):
        """Submit a service command to the forced, out-of-band send hook."""
        try:
            self._printer.commands(
                command,
                tags={
                    "plugin:rme_compatibility",
                    "rme:priority_control",
                    "trigger:rme.fast_%s" % action,
                },
                force=True,
            )
        except Exception:
            with self._state_lock:
                self._priority_controls_sent.discard(action)
            raise

    def _force_send_rme_control(self, comm_instance, command, gcode):
        """Write an RME service command without waiting for the prior ``ok``.

        OctoPrint's public ``force=True`` API skips its command queue but still
        enters the send queue, whose worker normally waits for an acknowledgement.
        Its bundled Action Command Prompt plugin uses these same protected comm
        primitives for an emergency M876 response. RME discovery is our separate
        capability gate for the firmware's reserved priority-command receiver.
        """
        use_up_clear = getattr(comm_instance, "_use_up_clear", None)
        do_send = getattr(comm_instance, "_do_send", None)
        continue_sending = getattr(comm_instance, "_continue_sending", None)
        if not callable(use_up_clear) or not callable(do_send):
            self._logger.warning(
                "OctoPrint does not expose its out-of-band send primitives; "
                "%s will use the normal forced send queue", command
            )
            return None
        used_up_clear = use_up_clear(gcode)
        do_send(command, gcode=gcode)
        if not used_up_clear and callable(continue_sending):
            continue_sending()
        return (None,)

    def _refresh_active_tool_locked(self):
        """Resolve active logical tool, physical mapping, and filament details.

        Callers hold ``_state_lock``. Firmware M865 metadata is authoritative;
        SpoolManager is a fallback while a fresh query is still in flight.
        """
        active = self._state["active_tool"]
        logical = active.get("logical")
        if logical is None:
            return
        logical = int(logical)
        toolmap = self._state.get("toolmap") or {}
        mapping = toolmap.get("mapping") or {}
        physical = (
            mapping.get(logical, mapping.get(str(logical), logical))
            if toolmap.get("enabled") else logical
        )
        physical = int(physical)
        loadout = next(
            (
                item for item in self._state.get("loaded_filaments", [])
                if int(item.get("tool", -1)) == physical
            ),
            None,
        )
        if loadout is None:
            loadout = next(
                (
                    item for item in self._state.get("spoolmanager", {}).get("selected", [])
                    if int(item.get("tool", -1)) == physical
                ),
                {},
            )
        color = loadout.get("color")
        active.update(
            logical=logical,
            physical=physical,
            material=loadout.get("material"),
            color_name=loadout.get("color_name") or loadout.get("display_name"),
            color=color if self._valid_color(color) else None,
        )

    def _filament_report(self):
        """Build OrcaSlicer's provider-neutral ``data.tools`` response shape."""
        with self._state_lock:
            provider = self._state["spoolmanager"].get("provider") or "internal"
            selected = copy.deepcopy(self._state["spoolmanager"].get("selected", []))
            loaded = copy.deepcopy(self._state.get("loaded_filaments", []))
            inventory = copy.deepcopy(self._state["spoolmanager"].get("inventory", []))
            machine_count = int(self._state.get("machine", {}).get("logical_tools", 0))
            active_tool = copy.deepcopy(self._state["active_tool"])
            tool_mapping = copy.deepcopy(self._state["toolmap"])
            stats = copy.deepcopy(self._state["stats"])
            updated = self._state["spoolmanager"].get("last_sync")
        selected_by_tool = {int(item["tool"]): item for item in selected}
        loaded_by_tool = {int(item["tool"]): item for item in loaded}
        highest = max(list(selected_by_tool) + list(loaded_by_tool) + [-1]) + 1
        count = max(machine_count, highest)
        tools = []
        for tool in range(count):
            item = selected_by_tool.get(tool)
            if item is not None:
                tools.append({
                    "name": item.get("display_name", ""),
                    "material": item.get("material", ""),
                    "color": item.get("color", ""),
                    "color_name": item.get("color_name", ""),
                    "vendor": item.get("vendor", ""),
                    "spool_id": str(item.get("database_id", "")),
                    "provider": provider,
                })
                continue
            firmware = loaded_by_tool.get(tool) or {}
            tools.append({
                "name": firmware.get("material", ""),
                "material": firmware.get("material", ""),
                "color": firmware.get("color", ""),
                "color_name": firmware.get("color_name", ""),
                "vendor": "",
                "spool_id": "",
                "provider": "RME firmware",
            })
        spools = []
        for item in inventory:
            cleaned = {key: value for key, value in item.items() if not key.startswith("_")}
            cleaned.update(
                spool_id=str(item.get("database_id", "")),
                name=item.get("display_name", ""),
                provider=provider,
            )
            spools.append(cleaned)
        return {
            "schema": "rme-filament-report-v1",
            "provider": provider,
            "data": {"tools": tools, "spools": spools},
            "active_tool": active_tool,
            "stats": stats,
            "tool_mapping": tool_mapping,
            "loaded_filaments": loaded,
            "updated": updated,
        }

    def _set_active_tool(self, logical):
        """Publish the tool selected by a transmitted ``Tn`` command."""
        with self._state_lock:
            self._state["active_tool"]["logical"] = int(logical)
            self._state["active_tool"]["updated"] = int(time.time())
            self._refresh_active_tool_locked()
        self._defer(self._persist_and_publish)

    def _open_session(self):
        legacy = 1 if self._settings.get_boolean(["legacy_notifications"]) else 0
        self._send_command("@RME SESSION OPEN events=15 legacy=%d" % legacy)

    @staticmethod
    def _valid_color(value):
        if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
            return False
        return all(character in "0123456789abcdefABCDEF" for character in value[1:])

    @staticmethod
    def _valid_packed_brightness(value):
        return 0 <= value <= 0xFFFFFFFF and all(((value >> shift) & 0xFF) <= 100 for shift in (0, 8, 16, 24))

    def _keepalive_loop(self):
        """Renew the RME lease and periodically reconcile optional spool state."""
        while not self._stop.wait(10):
            with self._state_lock:
                active = self._state["session"].get("active")
                connected = self._state["connected"]
            # Acknowledged M998 and FILE transactions must not be interleaved
            # with periodic session, statistics, or filament requests.
            transfer_busy = bool(
                (self._uploader and self._uploader.busy)
                or (self._file_service and self._file_service.busy)
            )
            if active and connected and not transfer_busy:
                try:
                    self._send_command("@RME SESSION KEEPALIVE")
                except Exception:
                    self._logger.debug("RME keepalive could not be queued", exc_info=True)
            interval = max(10, int(self._settings.get_int(["spoolmanager_sync_interval"]) or 30))
            if not transfer_busy and time.monotonic() - self._last_spool_sync >= interval:
                self._defer(self._periodic_filament_sync)
            if not transfer_busy:
                self._poll_stats_if_due(connected)

    def _poll_stats_if_due(self, connected, now=None):
        """Queue telemetry only after firmware positively answered the probe."""
        now = time.monotonic() if now is None else float(now)
        stats_interval = max(10, int(self._settings.get_int(["stats_poll_interval"]) or 30))
        if (
            self._stats_supported is True
            and connected
            and now - self._last_stats_poll >= stats_interval
        ):
            self._last_stats_poll = now
            self._defer(self._send_command, "@RME STATS QUERY")

    def _apply_toolmap(self, mapping, enabled, release_hold=False):
        machine = self._state.get("machine", {})
        count = int(machine.get("logical_tools", 0))
        normalized = {int(key): int(value) for key, value in mapping.items()}
        if count and (
            set(normalized.keys()) != set(range(count))
            or any(value >= count for value in normalized.values())
        ):
            raise ValueError("A mapping for every discovered logical tool is required")
        commands = toolmap_commands(normalized, enabled)
        # NFV's logical slicer slots must be checked against the selected
        # physical tool before its first-command validation gate is released.
        self._configure_validator_mapping(normalized if enabled else {})
        self._send_commands(commands)
        self._settings.set(["default_toolmap"], {str(k): v for k, v in normalized.items()})
        self._settings.set_boolean(["default_toolmap_enabled"], enabled)
        self._settings.save()
        if release_hold:
            self._release_toolmap_hold()

    def _handle_print_started(self, payload):
        """Backstop older connectors and enrich the pre-start prompt payload."""
        self._prepare_toolmap_prompt(payload)

    def _prepare_toolmap_prompt(self, payload=None):
        """Hold a multi-tool local job before any of its queued G-code is sent."""
        with self._state_lock:
            supported = self._state["supported"]
            count = int(self._state.get("machine", {}).get("logical_tools", 0))
            current_toolmap = copy.deepcopy(self._state.get("toolmap") or {})
        if not supported or count <= 1:
            return
        configured = current_toolmap.get("mapping") or self._settings.get(
            ["default_toolmap"], merged=True
        ) or {}
        mapping = {int(key): int(value) for key, value in configured.items()}
        if set(mapping.keys()) != set(range(count)):
            mapping = {index: index for index in range(count)}
        if self._settings.get_boolean(["prompt_toolmap_on_print"]):
            with self._state_lock:
                existing = self._state.get("prompt") or {}
                already_held = self._toolmap_hold_active and existing.get("kind") == "toolmap"
            if already_held:
                if payload and payload.get("name"):
                    with self._state_lock:
                        self._state["prompt"]["filename"] = payload["name"]
                    self._defer(self._persist_and_publish)
                return
            held = self._printer.set_job_on_hold(True, blocking=True)
            if not held:
                self._logger.error("Could not hold print for RME tool mapping")
                return
            timeout = max(0, min(86400, int(
                self._settings.get_int(["toolmap_timeout_seconds"]) or 0
            )))
            now = int(time.time())
            with self._state_lock:
                self._toolmap_hold_active = True
                self._state["prompt"] = {
                    "kind": "toolmap",
                    "message": "Choose the physical tool for each logical tool before printing",
                    "mapping": mapping,
                    "enabled": bool(current_toolmap.get("enabled")),
                    "count": count,
                    "filename": (payload or {}).get("name"),
                    "updated": now,
                    "timeout_seconds": timeout,
                    "deadline": now + timeout if timeout else None,
                    "timer_paused": False,
                    "remaining_seconds": timeout if timeout else None,
                }
            self._start_toolmap_timeout()
            self._defer(self._persist_and_publish)
        else:
            self._apply_toolmap(
                mapping, self._settings.get_boolean(["default_toolmap_enabled"])
            )

    def _release_toolmap_hold(self):
        with self._state_lock:
            prompt = self._state.get("prompt")
            was_toolmap = prompt and prompt.get("kind") == "toolmap"
            if was_toolmap:
                self._state["prompt"] = None
            held = self._toolmap_hold_active
            self._toolmap_hold_active = False
            self._toolmap_timer_generation += 1
        if held:
            try:
                self._printer.set_job_on_hold(False)
            finally:
                self._persist_and_publish()
        elif was_toolmap:
            self._persist_and_publish()

    def _configure_validator_mapping(self, mapping):
        """Give Nozzle Filament Validator the confirmed physical-tool mapping.

        The integration is optional and feature-detected. Current NFV builds
        expose ``set_tool_mapping``; older builds continue to validate without
        remap awareness and generate a clear warning in OctoPrint's log.
        """
        plugins = getattr(self._plugin_manager, "plugins", {})
        info = plugins.get("Nozzle_Filament_Validator") or plugins.get(
            "nozzle_filament_validator"
        )
        implementation = getattr(info, "implementation", None) if info else None
        setter = getattr(implementation, "set_tool_mapping", None)
        if callable(setter):
            setter({int(logical): int(physical) for logical, physical in mapping.items()})
        elif implementation is not None:
            self._logger.warning(
                "Nozzle Filament Validator does not expose remapped-tool validation support"
            )

    def _pause_toolmap_timeout(self):
        """Permanently pause this prompt's expiry after the first interaction."""
        with self._state_lock:
            prompt = self._state.get("prompt") or {}
            if prompt.get("kind") != "toolmap" or prompt.get("timer_paused"):
                return
            deadline = prompt.get("deadline")
            if deadline is not None:
                prompt["remaining_seconds"] = max(0, int(deadline - time.time()))
            prompt["deadline"] = None
            prompt["timer_paused"] = True
            prompt["updated"] = int(time.time())
            self._toolmap_timer_generation += 1
        self._persist_and_publish()

    def _start_toolmap_timeout(self):
        """Cancel an untouched print safely when its configured deadline expires."""
        with self._state_lock:
            self._toolmap_timer_generation += 1
            generation = self._toolmap_timer_generation
            prompt = self._state.get("prompt") or {}
            deadline = prompt.get("deadline")
        if deadline is None:
            return

        def wait_for_deadline():
            while not self._stop.wait(0.5):
                with self._state_lock:
                    current = self._state.get("prompt") or {}
                    valid = (
                        generation == self._toolmap_timer_generation
                        and current.get("kind") == "toolmap"
                        and not current.get("timer_paused")
                    )
                    current_deadline = current.get("deadline")
                if not valid or current_deadline is None:
                    return
                if time.time() < current_deadline:
                    continue
                self._expire_toolmap_prompt()
                return

        threading.Thread(
            target=wait_for_deadline, name="rme-toolmap-timeout", daemon=True
        ).start()

    def _expire_toolmap_prompt(self):
        """Keep the firmware's current mapping and continue an untouched job."""
        with self._state_lock:
            prompt = self._state.get("prompt") or {}
            if prompt.get("kind") != "toolmap" or prompt.get("timer_paused"):
                return
            current = copy.deepcopy(self._state.get("toolmap") or {})
        mapping = current.get("mapping") if current.get("enabled") else {}
        self._configure_validator_mapping(mapping or {})
        self._logger.info("Tool mapping prompt timed out; continuing with current firmware mapping")
        # Deliberately do not send TOOLMAP commands: timeout means leave the
        # printer exactly as it was, then allow NFV and the print to proceed.
        self._release_toolmap_hold()

    # -- Filament inventory synchronization -------------------------------

    @staticmethod
    def _tool_index(value):
        """Accept provider tool IDs in either integer or ``toolN`` form."""
        text = str(value)
        digits = "".join(character for character in text if character.isdigit())
        if not digits:
            raise ValueError("Filament provider did not contain a valid tool ID")
        return int(digits)

    @staticmethod
    def _public_spool_record(record):
        """Remove adapter-only fields before persisting or publishing a spool."""
        return {key: value for key, value in record.items() if not key.startswith("_")}

    def _active_spool_provider(self):
        """Resolve the current setting immediately for interactive UI actions."""
        self._spoolmanager, provider_name = self._resolve_spool_provider()
        return self._spoolmanager, provider_name

    def _sync_spoolmanager(self, force=False, push_to_firmware=True):
        """Reconcile the active inventory provider with eight firmware presets.

        The firmware limits user material names to seven characters and exposes
        eight slots. Seven slots receive stable database-ID aliases; slot seven
        is reserved for ``NEW``. Full metadata remains visible in OctoPrint.
        """
        if not self._settings.get_boolean(["spoolmanager_enabled"]):
            with self._state_lock:
                self._state["spoolmanager"].update(
                    available=False, status="disabled", error=None
                )
            self._persist_and_publish()
            return
        if not self._spool_sync_lock.acquire(False):
            return
        try:
            self._spoolmanager, provider_name = self._active_spool_provider()
            if not self._spoolmanager or not self._spoolmanager.available():
                raise SpoolManagerUnavailable("No filament inventory provider is available")

            inventory = [
                self._public_spool_record(record)
                for record in self._spoolmanager.inventory()
            ]
            selected_models = self._spoolmanager.selected()
            selected = [
                dict(self._public_spool_record(record), tool=tool)
                for tool, record in enumerate(selected_models) if record is not None
            ]
            records = {record["database_id"]: record for record in inventory}
            for record in selected:
                records[record["database_id"]] = {
                    key: value for key, value in record.items() if key != "tool"
                }

            with self._state_lock:
                old_published = copy.deepcopy(self._state["spoolmanager"].get("published", []))
                if self._state["spoolmanager"].get("provider") != provider_name:
                    old_published = []
                can_send = self._state["connected"] and self._state["supported"]
                logical_tools = int(self._state.get("machine", {}).get("logical_tools", 0))

            # Selected spools have priority, then prior slots, then the remaining
            # inventory. This minimizes menu churn while keeping all active tools.
            ordered_ids = []
            for record in selected + old_published + inventory:
                database_id = record.get("database_id")
                if database_id in records and database_id not in ordered_ids:
                    ordered_ids.append(database_id)
            ordered_ids = ordered_ids[:7]
            prior_slots = {
                item["database_id"]: item["slot"] for item in old_published
                if item.get("database_id") in ordered_ids and 0 <= int(item.get("slot", -1)) < 7
            }
            unused_slots = [slot for slot in range(7) if slot not in prior_slots.values()]
            used_aliases = set()
            published = []
            for database_id in ordered_ids:
                record = copy.deepcopy(records[database_id])
                slot = prior_slots.get(database_id)
                if slot is None:
                    slot = unused_slots.pop(0)
                alias = spool_alias(record["material"], database_id, used_aliases)
                used_aliases.add(alias)
                record.update(slot=slot, alias=alias)
                published.append(record)
            published.sort(key=lambda item: item["slot"])

            old_signature = [(item.get("slot"), item.get("alias"), item.get("database_id")) for item in old_published]
            new_signature = [(item["slot"], item["alias"], item["database_id"]) for item in published]
            # Publish the alias map before queueing M865 Q. OctoPrint's serial
            # worker may return the loadout while this reconciliation thread is
            # still running, and the receive side must recognize every alias.
            with self._state_lock:
                pending = self._state["spoolmanager"].get("pending_new")
                pending_provider_sync = self._state["spoolmanager"].get(
                    "pending_provider_sync"
                )
                self._state["spoolmanager"] = {
                    "available": True,
                    "provider": provider_name,
                    "status": "synchronizing" if can_send else "ready; printer disconnected",
                    "inventory": inventory,
                    "published": published,
                    "selected": selected,
                    "pending_new": pending,
                    "pending_provider_sync": pending_provider_sync,
                    "last_sync": int(time.time()),
                    "error": None,
                }
                self._refresh_active_tool_locked()
            should_push = can_send and push_to_firmware and not pending_provider_sync
            if should_push and (force or old_signature != new_signature):
                by_slot = {item["slot"]: item for item in published}
                commands = []
                for slot in range(7):
                    item = by_slot.get(slot)
                    if item:
                        nozzle = max(0, min(500, int(item["nozzle_temperature"])))
                        bed = max(0, min(500, int(item["bed_temperature"])))
                        commands.append(
                            "@RME FILAMENT SET slot=%d name=%s nozzle=%d preheat=%d bed=%d visible=1"
                            % (slot, item["alias"], nozzle, max(0, nozzle - 40), bed)
                        )
                    else:
                        commands.append(
                            "@RME FILAMENT SET slot=%d name=EMPTY nozzle=215 preheat=170 bed=60 visible=0"
                            % slot
                        )
                commands.extend([
                    "@RME FILAMENT SET slot=7 name=NEW nozzle=215 preheat=170 bed=60 visible=1",
                    "@RME FILAMENT QUERY",
                ])
                self._send_commands(commands)
            if should_push:
                # Reassert selected tool assignments after reconnects and after
                # inventory edits; publishing a preset alone does not mark it as
                # physically loaded in Buddy's M865 metadata.
                slot_by_id = {item["database_id"]: item for item in published}
                assignments = []
                selected_tools = {int(item["tool"]) for item in selected}
                tool_count = max(logical_tools, max(selected_tools, default=-1) + 1)
                for tool in range(tool_count):
                    if tool not in selected_tools:
                        assignments.append('M865 S"---" L%d' % tool)
                for selected_item in selected:
                    item = slot_by_id.get(selected_item["database_id"])
                    if item:
                        assignments.append(
                            'M865 U%d L%d O"%s"'
                            % (item["slot"], selected_item["tool"], item["color"])
                        )
                assignments.append("M865 Q")
                self._send_commands(assignments)

            with self._state_lock:
                if should_push:
                    self._state["spoolmanager"]["status"] = "synchronized"
                elif can_send and pending_provider_sync:
                    self._state["spoolmanager"]["status"] = "provider change awaiting confirmation"
            self._persist_and_publish()
        except SpoolManagerUnavailable as exc:
            with self._state_lock:
                self._state["spoolmanager"].update(
                    available=False, status="unavailable", error=str(exc)
                )
            self._persist_and_publish()
        except Exception as exc:
            self._logger.exception("SpoolManager synchronization failed")
            with self._state_lock:
                self._state["spoolmanager"].update(status="error", error=str(exc))
            self._persist_and_publish()
        finally:
            self._last_spool_sync = time.monotonic()
            self._spool_sync_lock.release()

    def _resolve_spool_provider(self):
        """Choose exactly one inventory backend.

        An explicitly selected external provider never falls back silently to
        the built-in database. In Automatic mode the built-in backend is used
        only when neither external plugin is available.
        """
        preference = str(self._settings.get(["spool_provider"], merged=True) or "auto").lower()
        providers = {
            "spoolmanager": getattr(self, "_spoolmanager_bridge", None),
            "spoolman": getattr(self, "_spoolman_bridge", None),
            "internal": getattr(self, "_internal_spool_bridge", None),
        }
        if preference in ("spoolmanager", "spoolman"):
            return providers[preference], preference
        if preference == "internal":
            return providers["internal"], "internal"
        for name in ("spoolmanager", "spoolman"):
            candidate = providers[name]
            if candidate is not None and candidate.available():
                return candidate, name
        return providers["internal"], "internal"

    def _handle_spoolmanager_event(self, event, payload):
        """Prompt before applying external SpoolManager selection changes."""
        _, provider_name = self._active_spool_provider()
        if provider_name != "spoolmanager":
            return
        if event.endswith("_spool_selected"):
            tool = self._tool_index(payload.get("toolId", payload.get("tool", 0)))
            database_id = int(payload.get("databaseId", payload.get("database_id")))
            with self._state_lock:
                previous = next((
                    item for item in self._state["spoolmanager"].get("selected", [])
                    if int(item.get("tool", -1)) == tool
                ), None)
            # SpoolManager emits spool_selected while merely reading its saved
            # selections. An identical event is not a user configuration edit.
            if previous and int(previous.get("database_id", -1)) == database_id:
                return
            if self._consume_expected_provider_event(tool, database_id):
                self._sync_spoolmanager(True, False)
                return
            self._sync_spoolmanager(True, False)
            self._queue_provider_sync_prompt(tool, database_id)
        elif event.endswith("_spool_deselected"):
            tool = self._tool_index(payload.get("toolId", payload.get("tool", 0)))
            with self._state_lock:
                previously_selected = any(
                    int(item.get("tool", -1)) == tool
                    for item in self._state["spoolmanager"].get("selected", [])
                )
            if not previously_selected:
                return
            if self._consume_expected_provider_event(tool, None):
                self._sync_spoolmanager(True, False)
                return
            self._sync_spoolmanager(True, False)
            self._queue_provider_sync_prompt(tool, None)
        else:
            self._sync_spoolmanager(True)

    def _handle_spoolman_event(self):
        """Prompt for Spoolman selection/configuration changes when active."""
        _, provider_name = self._active_spool_provider()
        if provider_name == "spoolman":
            self._sync_spoolmanager(True, False)
            with self._state_lock:
                expected = self._expected_spoolman_event_until > time.monotonic()
                self._expected_spoolman_event_until = 0
            if expected:
                return
            self._queue_provider_sync_prompt(None, None)

    def _mark_expected_provider_event(self, tool, database_id):
        """Suppress the echo of a provider event initiated from firmware/UI."""
        with self._state_lock:
            now = time.monotonic()
            self._expected_provider_events = {
                key: expiry for key, expiry in self._expected_provider_events.items()
                if expiry > now
            }
            self._expected_provider_events[(int(tool), database_id)] = now + 5
            self._expected_spoolman_event_until = now + 5

    def _consume_expected_provider_event(self, tool, database_id):
        with self._state_lock:
            key = (int(tool), database_id)
            expiry = self._expected_provider_events.pop(key, 0)
        return expiry > time.monotonic()

    def _queue_provider_sync_prompt(self, tool, database_id):
        """Persist a provider-to-printer confirmation across browser refreshes."""
        with self._state_lock:
            selected = copy.deepcopy(self._state["spoolmanager"].get("selected", []))
            item = next(
                (entry for entry in selected if tool is not None and int(entry.get("tool", -1)) == int(tool)),
                None,
            )
            if tool is None:
                message = "The filament provider configuration changed. Apply it to the printer?"
            elif database_id is None:
                message = "SpoolManager cleared tool T%d. Clear it on the printer?" % int(tool)
            else:
                name = (item or {}).get("display_name", "spool %s" % database_id)
                message = "SpoolManager selected %s for T%d. Apply it to the printer?" % (
                    name, int(tool)
                )
            self._state["spoolmanager"]["pending_provider_sync"] = {
                "tool": tool,
                "database_id": database_id,
                "message": message,
                "updated": int(time.time()),
            }
            self._state["spoolmanager"]["status"] = "provider change awaiting confirmation"
        self._persist_and_publish()

    def _sync_filaments_from_printer(self):
        """Make firmware M865 assignments authoritative for one reconciliation."""
        with self._state_lock:
            self._state["spoolmanager"]["pending_provider_sync"] = None
            can_send = self._state["connected"] and self._state["supported"]
            self._state["spoolmanager"]["status"] = "reading selections from printer"
        if not can_send:
            raise RuntimeError("RME printer is not connected")
        self._send_command("M865 Q")
        self._persist_and_publish()

    def _initialize_spool_sync(self):
        """Load aliases first, then import printer assignments on connection."""
        self._sync_spoolmanager(True, False)
        with self._state_lock:
            pending = self._state["spoolmanager"].get("pending_provider_sync")
            can_send = self._state["connected"] and self._state["supported"]
        if can_send and not pending:
            self._send_command("M865 Q")

    def _periodic_filament_sync(self):
        """Refresh inventory and poll printer changes without overwriting it."""
        self._sync_spoolmanager(False, False)
        with self._state_lock:
            pending = self._state["spoolmanager"].get("pending_provider_sync")
            can_send = self._state["connected"] and self._state["supported"]
        if can_send and not pending:
            self._send_command("M865 Q")

    def _sync_filaments_to_printer(self):
        """Publish provider presets and assignments after explicit acceptance."""
        with self._state_lock:
            self._state["spoolmanager"]["pending_provider_sync"] = None
        self._sync_spoolmanager(True, True)

    def _assign_published_spool(self, tool, database_id):
        """Apply a selected provider record as firmware loadout metadata."""
        with self._state_lock:
            can_send = self._state["connected"] and self._state["supported"]
            item = next((entry for entry in self._state["spoolmanager"].get("published", [])
                         if entry["database_id"] == int(database_id)), None)
        if not can_send:
            return
        if item is None:
            raise RuntimeError("Selected spool could not fit in the seven firmware slots")
        self._send_commands([
            'M865 U%d L%d O"%s"' % (item["slot"], int(tool), item["color"]),
            "M865 Q",
        ])

    def _select_spool_from_octoprint(self, tool, database_id):
        """Select from the RME tab and update both provider and firmware."""
        tool = self._tool_index(tool)
        database_id = int(database_id)
        provider, _ = self._active_spool_provider()
        self._mark_expected_provider_event(tool, database_id)
        provider.select(tool, database_id)
        self._sync_spoolmanager(True)
        self._assign_published_spool(tool, database_id)

    def _deselect_spool_from_octoprint(self, tool):
        """Clear one tool in the active provider and on the printer."""
        tool = self._tool_index(tool)
        provider, _ = self._active_spool_provider()
        self._mark_expected_provider_event(tool, None)
        provider.deselect(tool)
        with self._state_lock:
            can_send = self._state["connected"] and self._state["supported"]
        if can_send:
            self._send_commands(['M865 S"---" L%d' % tool, "M865 Q"])
        self._sync_spoolmanager(True)

    def _begin_new_spool(self, tool):
        """Open the persistent creation form without requiring an LCD request."""
        tool = self._tool_index(tool)
        defaults = self.get_settings_defaults()
        with self._state_lock:
            self._state["spoolmanager"]["pending_new"] = {
                "tool": tool,
                "display_name": "New spool on tool %d" % tool,
                "vendor": "",
                "material": "PLA",
                "color": "#808080",
                "color_name": "",
                "total_weight": self._settings.get_int(["spoolmanager_default_weight"])
                or defaults["spoolmanager_default_weight"],
                "nozzle_temperature": 215,
                "bed_temperature": 60,
            }
        self._persist_and_publish()

    def _accept_firmware_spool(self, record):
        """Apply an LCD-side material choice to the active provider.

        A known short alias selects an existing spool. ``NEW`` or a normal
        firmware material opens a persistent creation form in OctoPrint using
        the material and color that the operator chose locally.
        """
        material = record.get("material", "")
        tool = int(record["tool"])
        provider, _ = self._active_spool_provider()
        with self._state_lock:
            published = copy.deepcopy(self._state["spoolmanager"].get("published", []))
            selected = copy.deepcopy(self._state["spoolmanager"].get("selected", []))
        match = next((item for item in published if item["alias"] == material), None)
        current = next((item for item in selected if int(item.get("tool", -1)) == tool), None)
        if match:
            if not current or current["database_id"] != match["database_id"]:
                self._mark_expected_provider_event(tool, match["database_id"])
                provider.select(tool, match["database_id"])
                self._sync_spoolmanager(False, False)
            return
        if material == "---":
            if current:
                self._mark_expected_provider_event(tool, None)
                provider.deselect(tool)
                self._sync_spoolmanager(False, False)
            return

        # This state is intentionally persisted independently of firmware
        # dialogs, so a refresh cannot lose partially entered spool details.
        defaults = self.get_settings_defaults()
        with self._state_lock:
            self._state["spoolmanager"]["pending_new"] = {
                "tool": tool,
                "display_name": "New spool on tool %d" % tool,
                "vendor": "",
                "material": "PLA" if material == "NEW" else material,
                "color": record.get("color") if self._valid_color(record.get("color")) else "#808080",
                "color_name": record.get("color_name") if record.get("color_name") != "None" else "",
                "total_weight": self._settings.get_int(["spoolmanager_default_weight"]) or defaults["spoolmanager_default_weight"],
                "nozzle_temperature": 215,
                "bed_temperature": 60,
            }
        if current:
            self._mark_expected_provider_event(tool, None)
            provider.deselect(tool)
        self._persist_and_publish()

    def _create_spool(self, data):
        """Validate the remote form, create/select the spool, then assign Buddy."""
        with self._state_lock:
            pending = copy.deepcopy(self._state["spoolmanager"].get("pending_new") or {})
        if not pending:
            raise ValueError("There is no pending printer-side spool selection")
        values = dict(pending)
        values.update(data)
        if not str(values.get("display_name", "")).strip() or not str(values.get("material", "")).strip():
            raise ValueError("Spool name and material are required")
        weight = float(values["total_weight"])
        if weight <= 0:
            raise ValueError("Spool weight must be greater than zero")
        values.update(
            total_weight=weight,
            diameter=self._settings.get_float(["spoolmanager_default_diameter"]) or 1.75,
            density=self._settings.get_float(["spoolmanager_default_density"]) or 1.24,
            nozzle_temperature=int(values.get("nozzle_temperature", 215)),
            bed_temperature=int(values.get("bed_temperature", 60)),
        )
        provider, _ = self._active_spool_provider()
        created = provider.create(values)
        provider.select(int(pending["tool"]), created["database_id"])
        with self._state_lock:
            self._state["spoolmanager"]["pending_new"] = None
        self._sync_spoolmanager(True)
        self._assign_published_spool(int(pending["tool"]), created["database_id"])

    # -- Machine profile ----------------------------------------------------

    def _apply_machine_profile(self):
        with self._state_lock:
            machine = copy.deepcopy(self._state.get("machine", {}))
        required = (
            "x_min", "x_max", "y_min", "y_max", "z_min", "z_max",
            "feed_x", "feed_y", "feed_z", "logical_tools", "single_nozzle",
        )
        if not all(key in machine for key in required):
            raise RuntimeError("Machine discovery has not completed")
        manager = self._printer_profile_manager
        profile = copy.deepcopy(manager.get_current_or_default())
        profile["volume"].update(
            width=float(machine["x_max"]) - float(machine["x_min"]),
            depth=float(machine["y_max"]) - float(machine["y_min"]),
            height=float(machine["z_max"]) - float(machine["z_min"]),
            formFactor="rectangular",
            origin="lowerleft",
            custom_box={
                "x_min": float(machine["x_min"]), "x_max": float(machine["x_max"]),
                "y_min": float(machine["y_min"]), "y_max": float(machine["y_max"]),
                "z_min": float(machine["z_min"]), "z_max": float(machine["z_max"]),
            },
        )
        count = int(machine["logical_tools"])
        profile["extruder"].update(
            count=count,
            sharedNozzle=bool(int(machine["single_nozzle"])),
            offsets=[(0, 0)] * count,
        )
        for axis in ("x", "y", "z"):
            profile["axes"][axis]["speed"] = float(machine["feed_" + axis]) * 60.0
        manager.save(profile, allow_overwrite=True)
        self._logger.info("Updated OctoPrint printer profile from RME machine discovery")

    # -- RME USB storage ---------------------------------------------------

    def _require_storage(self):
        with self._state_lock:
            connected = self._state["connected"] and self._state["supported"]
        if not connected or not self._file_service:
            raise FileServiceError("An RME printer is not connected")
        if self._uploader and self._uploader.busy:
            raise FileServiceError("Firmware staging is already using the serial transfer channel")

    @staticmethod
    def _join_storage_path(directory, name):
        base = str(directory or "/").rstrip("/")
        return (base + "/" + str(name).lstrip("/")) if base else "/" + str(name).lstrip("/")

    @staticmethod
    def _parent_storage_path(path):
        parts = [part for part in str(path or "/").split("/") if part]
        return "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"

    def _set_storage_status(self, status, **changes):
        with self._state_lock:
            self._state["storage"].update(status=status, **changes)
        self._persist_and_publish()

    def _storage_progress(self, offset, size):
        now = time.monotonic()
        with self._state_lock:
            self._state["storage"].update(
                status="uploading", progress=round(offset * 100.0 / max(1, size), 2),
                error=None,
            )
        if now - self._last_storage_publish >= 0.25:
            self._last_storage_publish = now
            self._publish()

    def _initialize_storage(self):
        """Probe the exact FILE capability record exposed by current RME."""
        self._require_storage()
        self._set_storage_status("detecting", progress=None, error=None)
        try:
            caps = self._file_service.capabilities()
            caps = {key: value for key, value in caps.items() if key != "record"}
            with self._state_lock:
                self._state["storage"].update(supported=True, caps=caps)
            self._refresh_storage("/")
        except Exception as exc:
            with self._state_lock:
                self._state["storage"].update(
                    supported=False, status="unsupported", progress=None, error=str(exc)
                )
            self._persist_and_publish()

    def _refresh_storage(self, path=None):
        """List one USB directory and publish browser-ready full paths."""
        self._require_storage()
        with self._state_lock:
            path = str(path if path is not None else self._state["storage"].get("path", "/"))
            self._state["storage"].update(status="listing", progress=None, error=None)
        self._publish()
        try:
            entries = self._file_service.list_directory(path)
            public = []
            for entry in entries:
                item = {key: value for key, value in entry.items() if key != "record"}
                item["path"] = self._join_storage_path(path, item["name"])
                public.append(item)
            public.sort(key=lambda item: (item.get("type") != "dir", str(item.get("name", "")).lower()))
            with self._state_lock:
                self._state["storage"].update(
                    supported=True, path=path or "/", entries=public, status="ready",
                    progress=None, error=None, updated=int(time.time()),
                )
            self._persist_and_publish()
        except Exception as exc:
            self._set_storage_status("error", progress=None, error=str(exc))
            raise

    def _storage_mutation(self, action, path, destination=None):
        """Apply a guarded USB action and refresh the affected directory."""
        self._require_storage()
        self._set_storage_status(action.lower(), progress=None, error=None)
        try:
            self._file_service.mutate(action, path, destination)
            if action in ("PRINT", "FLASH"):
                self._set_storage_status(action.lower() + " queued", progress=None, error=None)
            else:
                refresh = self._parent_storage_path(path)
                self._refresh_storage(refresh)
        except Exception as exc:
            self._set_storage_status("error", progress=None, error=str(exc))
            raise

    # -- Firmware files stored on the Pi ----------------------------------

    def _firmware_path(self, filename):
        safe = secure_filename(str(filename))
        if safe != filename or not safe.lower().endswith(".bbf"):
            raise ValueError("Invalid firmware filename")
        return os.path.join(self._firmware_directory, safe)

    def _list_firmware(self):
        result = []
        if not self._firmware_directory:
            return result
        for filename in sorted(os.listdir(self._firmware_directory), key=str.lower):
            if filename.lower().endswith(".bbf"):
                path = self._firmware_path(filename)
                try:
                    stat = os.stat(path)
                    result.append(
                        {
                            "name": filename,
                            "size": stat.st_size,
                            "modified": int(stat.st_mtime),
                        }
                    )
                except OSError:
                    continue
        return result

    def _start_firmware_upload(self, filename, flash_after_stage=False):
        """Stage a BBF through FILE on current firmware or legacy M998."""
        if self._printer.is_printing() or self._printer.is_paused():
            raise UploadError("Firmware transfer is only allowed while the printer is idle")
        if not self._state.get("supported"):
            raise UploadError("The connected printer did not complete the RME handshake")
        path = self._firmware_path(filename)
        if not os.path.isfile(path):
            raise UploadError("Firmware file was not found on the Pi")
        metadata = firmware_metadata(path)
        with self._firmware_action_lock:
            if self._uploader.busy or (
                self._firmware_file_thread and self._firmware_file_thread.is_alive()
            ):
                raise UploadError("A firmware transfer is already active")
            with self._state_lock:
                caps = dict(self._state["storage"].get("caps") or {})
                file_supported = bool(
                    self._state["storage"].get("supported") and int(caps.get("write", 0))
                )
            # Current RME firmware advertises FILE WRITE and should use it. Its
            # M998 handler relies on Marlin string_arg and can reject otherwise
            # valid numeric P phases as FW_UPLOAD PHASE.
            if self._file_service and not file_supported:
                try:
                    probed = self._file_service.capabilities()
                    caps = {key: value for key, value in probed.items() if key != "record"}
                    file_supported = bool(int(caps.get("write", 0)))
                    with self._state_lock:
                        self._state["storage"].update(
                            supported=file_supported, caps=caps,
                            status="ready" if file_supported else "unsupported", error=None,
                        )
                except Exception:
                    file_supported = False
            with self._state_lock:
                self._state["firmware"]["flash_after_stage"] = bool(flash_after_stage)
            if file_supported:
                # ``write_file`` owns the same serialized operation lock as
                # directory listing and capability probes. Starting the worker
                # here lets a short UI refresh finish first instead of exposing
                # a transient HTTP 409 to the user.
                self._firmware_file_thread = threading.Thread(
                    target=self._run_file_firmware_upload,
                    args=(path, metadata),
                    name="rme-file-firmware-upload",
                    daemon=True,
                )
                self._firmware_file_thread.start()
                return
            try:
                self._uploader.start(path, metadata)
            except Exception:
                with self._state_lock:
                    self._state["firmware"]["flash_after_stage"] = False
                raise

    def _run_file_firmware_upload(self, path, metadata):
        """Stage and verify ``FWUPD.BBF`` through the current FILE service."""
        try:
            self._firmware_state_changed(
                status="starting", filename=metadata["name"], size=metadata["size"],
                sha256=metadata["sha256"], offset=0, progress=0, error=None,
                staged_path=None,
            )
            self._file_service.write_file(
                path,
                "FWUPD.BBF",
                progress=self._firmware_file_progress,
                finalizing=lambda: self._firmware_state_changed(
                    status="verifying", offset=metadata["size"], progress=100
                ),
            )
            self._firmware_state_changed(
                status="staged", offset=metadata["size"], progress=100,
                staged_path="/usb/FWUPD.BBF",
            )
        except Exception as exc:
            self._logger.exception("RME FILE firmware transfer failed")
            self._firmware_state_changed(status="error", error=str(exc))

    def _firmware_file_progress(self, offset, size):
        self._firmware_state_changed(
            status="uploading", offset=offset,
            progress=round(offset * 100.0 / max(1, size), 2),
        )

    def _firmware_state_changed(self, **changes):
        status = changes.get("status")
        with self._state_lock:
            self._state["firmware"].update(changes)
            flash_after_stage = bool(
                self._state["firmware"].get("flash_after_stage")
            )
            if status == "error":
                self._state["firmware"]["flash_after_stage"] = False
        now = time.monotonic()
        if status != "uploading" or now - self._last_fw_publish >= 0.25:
            self._last_fw_publish = now
            self._persist_and_publish()
        if status == "staged" and flash_after_stage:
            self._defer(self._flash_after_verified_stage)
        elif status in ("staged", "error"):
            self._defer(self._restore_session_after_upload)

    def _flash_after_verified_stage(self):
        """Wait for the transfer worker to exit, then perform one-click flash."""
        deadline = time.monotonic() + 5
        while (
            (self._uploader and self._uploader.busy)
            or (self._firmware_file_thread and self._firmware_file_thread.is_alive())
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        if (self._uploader and self._uploader.busy) or (
            self._firmware_file_thread and self._firmware_file_thread.is_alive()
        ):
            self._firmware_state_changed(
                status="error",
                error="Verified transfer did not finish cleanly; automatic flash was stopped",
            )
            return
        with self._state_lock:
            requested = bool(self._state["firmware"].get("flash_after_stage"))
            staged = self._state["firmware"].get("status") == "staged"
            self._state["firmware"]["flash_after_stage"] = False
        if requested and staged:
            self._flash_firmware()

    def _restore_session_after_upload(self):
        # The event lease normally expires during a long M998 transfer because
        # generic keepalive acknowledgements must not be interleaved with its
        # strict request/response exchange.
        deadline = time.monotonic() + 5
        while (
            (self._uploader and self._uploader.busy)
            or (self._firmware_file_thread and self._firmware_file_thread.is_alive())
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        with self._state_lock:
            should_open = self._state["connected"] and self._state["supported"]
        if should_open:
            self._open_session()

    def _flash_firmware(self):
        if self._printer.is_printing() or self._printer.is_paused():
            raise UploadError("Firmware flashing is only allowed while the printer is idle")
        with self._state_lock:
            if self._state["firmware"].get("status") != "staged":
                raise UploadError("Stage and verify firmware on the printer before flashing")
            use_file_service = bool(
                self._state["storage"].get("supported")
                and int(self._state["storage"].get("caps", {}).get("flash", 0))
            )
        if use_file_service:
            self._file_service.mutate("FLASH", "FWUPD.BBF")
        else:
            self._send_command("M997 /usb/FWUPD.BBF")
        with self._state_lock:
            self._state["firmware"].update(
                status="flashing", error=None, flash_after_stage=False
            )
        self._persist_and_publish()

    def _delete_firmware(self, filename):
        if (self._uploader and self._uploader.busy) or (
            self._firmware_file_thread and self._firmware_file_thread.is_alive()
        ):
            raise UploadError("Cannot delete firmware during a transfer")
        path = self._firmware_path(filename)
        if not os.path.isfile(path):
            flask.abort(404)
        os.unlink(path)

    # -- State publication --------------------------------------------------

    def _persistent_snapshot(self):
        """Return refresh/restart-critical state, excluding ephemeral connection data."""
        with self._state_lock:
            return copy.deepcopy(
                {key: self._state[key] for key in (
                    "machine", "toolmap", "workflow", "prompt", "firmware",
                    "spoolmanager", "loaded_filaments", "active_tool", "internal_spools",
                    "stats",
                )}
            )

    def _public_state(self):
        with self._state_lock:
            result = copy.deepcopy(self._state)
        # The built-in database is implementation state, not a second live
        # provider. Keep it private so an external provider cannot appear to be
        # running alongside SpoolManager or Spoolman in API/UI snapshots.
        result.pop("internal_spools", None)
        result["firmware_files"] = self._list_firmware()
        return result

    def _persist_and_publish(self):
        if self._store:
            self._store.request_save()
        self._publish()

    def _publish(self):
        """Send one authoritative snapshot to every currently open OctoPrint UI."""
        if hasattr(self, "_plugin_manager"):
            self._plugin_manager.send_plugin_message(self._identifier, self._public_state())

    def _defer(self, callback, *args):
        """Keep serial/event callbacks non-blocking while retaining error logging."""
        def run():
            try:
                callback(*args)
            except Exception:
                self._logger.exception("Deferred RME task failed")

        threading.Thread(target=run, daemon=True).start()


__plugin_name__ = "RME Compatibility"
__plugin_pythoncompat__ = ">=3.8,<4"
__plugin_implementation__ = RmeCompatibilityPlugin()
__plugin_hooks__ = {
    "octoprint.comm.protocol.action": __plugin_implementation__.action_command_hook,
    "octoprint.comm.protocol.atcommand.sending": __plugin_implementation__.atcommand_sending_hook,
    "octoprint.comm.protocol.gcode.received": __plugin_implementation__.gcode_received_hook,
    "octoprint.comm.protocol.gcode.queuing": (__plugin_implementation__.gcode_queuing_hook, 1),
    "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.gcode_sent_hook,
    "octoprint.comm.protocol.scripts": (__plugin_implementation__.gcode_script_hook, 1),
    "octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook,
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
}
