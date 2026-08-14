"""OctoPrint integration for the Prusa Buddy RME serial protocol.

This module owns lifecycle, API, UI-state, and optional-plugin coordination.
Wire parsing and firmware transport live in smaller independently testable
modules so the serial receive hook stays fast and easy to audit.
"""

from __future__ import absolute_import

import copy
import functools
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import quote

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
from .storage import StateStore, TransferManifestStore
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
        self._manifest_store = None
        self._transfer_directory = None
        self._partial_thread = None
        self._uploader = None
        self._file_service = None
        self._firmware_file_thread = None
        self._firmware_file_cancel = threading.Event()
        self._stop = threading.Event()
        self._keepalive_thread = None
        self._firmware_directory = None
        self._last_fw_publish = 0
        self._spoolmanager = None
        self._spool_sync_lock = threading.Lock()
        self._expected_provider_events = {}
        self._expected_spoolman_event_until = 0
        self._toolmap_hold_active = False
        self._toolmap_timer_generation = 0
        self._preflight_gate_started = False
        self._print_job_gcode_sent = False
        self._skip_cancel_script = False
        self._stats_supported = None
        self._stats_probe_sent = False
        self._priority_controls_sent = set()
        self._firmware_completed_controls = set()
        self._firmware_action_lock = threading.Lock()
        self._last_storage_publish = 0
        self._native_refresh_lock = threading.Lock()
        self._publish_timer = None
        self._publish_timer_lock = threading.Lock()
        self._configuration_timer = None
        self._configuration_timer_lock = threading.Lock()
        self._configuration_domains = set()
        self._binary_frame_lock = threading.Lock()
        self._binary_session = None
        self._manufacturer_sync_timer = None
        self._manufacturer_sync_timer_lock = threading.Lock()
        self._manufacturer_sync_pending = False
        self._transaction = int(time.time() * 1000) & 0xFFFFFFFF or 1
        self._suppressed_refresh_transactions = {}
        self._provider_firmware_signature = None
        self._transfer_conflict_cancel = False
        self._firmware_handoff_started_at = 0

    @staticmethod
    def _empty_state():
        """Build a complete JSON-safe snapshot for the UI and persistence layer."""
        return {
            "connected": False,
            "supported": False,
            "session": {
                "active": False, "legacy": True, "last_seq": 0,
                "configuration_revision": 0,
            },
            "machine": {},
            "toolmap": {"enabled": False, "mapping": {}},
            "lock": {},
            "theme": {},
            "light": {},
            "filaments": [],
            "manufacturers": {"profiles": [], "loaded": []},
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
                "reconnect_expected": False,
                "recovery_required": False,
                "candidate": False,
                "armed": False,
                "printer_stage_state": "idle",
            },
            "storage": {
                "supported": False, "caps": {}, "path": "/", "entries": [],
                "status": "not checked", "progress": None, "error": None,
                "updated": None, "native_files": [], "download": None,
                "downloads": [], "partial": None,
            },
            "errors": [],
        }

    # -- OctoPrint lifecycle -------------------------------------------------

    def on_after_startup(self):
        """Restore durable UI state and start non-serial background workers."""
        data_folder = self.get_plugin_data_folder()
        self._firmware_directory = os.path.join(data_folder, "firmware")
        os.makedirs(self._firmware_directory, exist_ok=True)
        self._download_directory = os.path.join(data_folder, "downloads")
        os.makedirs(self._download_directory, exist_ok=True)
        self._transfer_directory = os.path.join(data_folder, "transfer_sources")
        os.makedirs(self._transfer_directory, exist_ok=True)
        restored_downloads = []
        for stored_name in sorted(os.listdir(self._download_directory)):
            match = re.match(r"^([0-9a-f]{32})--(.+)$", stored_name)
            path = os.path.join(self._download_directory, stored_name)
            if not match or not os.path.isfile(path) or stored_name.endswith(".part"):
                continue
            restored_downloads.append({
                "id": match.group(1), "path": None, "name": match.group(2),
                "stored_name": stored_name, "target": "pi", "status": "ready",
                "size": os.path.getsize(path), "offset": os.path.getsize(path),
                "progress": 100, "error": None,
                "updated": int(os.path.getmtime(path)),
            })
        restored_downloads.sort(key=lambda item: item["updated"])
        with self._state_lock:
            self._state["storage"]["downloads"] = restored_downloads[-20:]
        self._store = StateStore(
            os.path.join(data_folder, "state.json"), self._persistent_snapshot, self._logger
        )
        persisted = self._store.load()
        self._manifest_store = TransferManifestStore(
            os.path.join(data_folder, "transfer-manifest.json"), self._logger
        )
        interrupted_transfer = self._manifest_store.load()
        retained_source = (
            os.path.realpath(interrupted_transfer.get("source_path"))
            if interrupted_transfer and interrupted_transfer.get("source_path") else None
        )
        for stored_name in os.listdir(self._transfer_directory):
            stored_path = os.path.realpath(
                os.path.join(self._transfer_directory, stored_name)
            )
            if stored_path != retained_source and os.path.isfile(stored_path):
                try:
                    os.unlink(stored_path)
                except OSError:
                    self._logger.warning(
                        "Could not remove stale RME transfer source %s", stored_path
                    )
        with self._state_lock:
            for key in (
                "machine", "toolmap",
                "spoolmanager", "loaded_filaments", "active_tool", "internal_spools",
                "stats",
            ):
                if key in persisted:
                    self._state[key] = persisted[key]
            # An uncertain raw receiver is a durable safety condition. An
            # OctoPrint restart is not evidence that the printer left binary
            # mode, so retain the lock and warning until printer reboot proof.
            persisted_firmware = persisted.get("firmware") or {}
            if persisted_firmware.get("recovery_required"):
                self._state["firmware"].update(persisted_firmware)
                self._state["firmware"].update(
                    status="error", recovery_required=True,
                    error=(
                        "Printer communication is locked after an unconfirmed "
                        "binary transfer. Power-cycle the printer, then confirm "
                        "the reboot in RME settings. No RME commands will be sent."
                    ),
                )
            elif persisted_firmware.get("status") in ("ready", "staged"):
                self._state["firmware"].update(persisted_firmware)
                self._state["firmware"]["status"] = "ready"
                self._state["firmware"]["error"] = None
            # A one-click flash request is deliberately process-local. Never
            # carry a bootloader handoff intent across an OctoPrint restart.
            self._state["firmware"]["flash_after_stage"] = False
            if interrupted_transfer:
                self._state["storage"]["partial"] = self._public_manifest(
                    interrupted_transfer, status="interrupted"
                )
        self._store.start()
        self._uploader = FirmwareUploader(
            self._send_command, self._firmware_state_changed, self._logger
        )
        self._file_service = RmeFileService(
            self._send_command, self._logger,
            send_binary=self._send_binary_frame,
            begin_binary=self._begin_binary_transport,
            end_binary=self._end_binary_transport,
        )
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
        with self._publish_timer_lock:
            if self._publish_timer:
                self._publish_timer.cancel()
                self._publish_timer = None
        with self._configuration_timer_lock:
            if self._configuration_timer:
                self._configuration_timer.cancel()
                self._configuration_timer = None
        with self._manufacturer_sync_timer_lock:
            if self._manufacturer_sync_timer:
                self._manufacturer_sync_timer.cancel()
                self._manufacturer_sync_timer = None
        if self._uploader and self._uploader.busy:
            self._uploader.cancel()
        self._firmware_file_cancel.set()
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
            "spoolmanager_default_weight": 1000,
            "spoolmanager_default_diameter": 1.75,
            "spoolmanager_default_density": 1.24,
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
            "stage_octoprint_firmware": ["path"],
            "stage_and_flash_octoprint_firmware": ["path"],
            "unstage_firmware": [],
            "cancel_firmware": [],
            "flash_firmware": [],
            "confirm_printer_reboot": [],
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
            "storage_download": ["path", "target"],
            "partial_resume": [],
            "partial_discard": [],
            "partial_cleanup": ["path"],
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
                    "@RME MANUFACTURER QUERY",
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
            self._send_command(self._with_transaction("@RME LOCK NOW"))
        elif command == "lock_unlock":
            pin = str(data["pin"])
            if not pin.isdigit() or len(pin) < 4 or len(pin) > 9:
                flask.abort(400, description="PIN must contain 4 through 9 digits")
            self._send_command(self._with_transaction(
                "@RME LOCK UNLOCK pin=%s digits=%d" % (pin, len(pin))
            ))
        elif command == "set_lock":
            pin = str(data["pin"])
            timeout = int(data["timeout"])
            serial = 1 if bool(data["serial"]) else 0
            enabled = 1 if bool(data["enabled"]) else 0
            if not pin.isdigit() or len(pin) < 4 or len(pin) > 9:
                flask.abort(400, description="PIN must contain 4 through 9 digits")
            if timeout < 0 or timeout > 65535:
                flask.abort(400, description="Lock timeout must be 0 through 65535 seconds")
            self._send_command(self._with_transaction(
                "@RME LOCK SET pin=%s digits=%d timeout=%d serial=%d enabled=%d"
                % (pin, len(pin), timeout, serial, enabled)
            ))
        elif command == "set_theme":
            colors = data["colors"]
            keys = ("primary", "progress", "warning", "error", "image")
            if not isinstance(colors, dict) or any(
                key not in colors or not self._valid_color(colors[key]) for key in keys
            ):
                flask.abort(400, description="All theme colors must use #RRGGBB")
            self._send_command(self._with_transaction(
                "@RME THEME SET " + " ".join("%s=%s" % (key, colors[key]) for key in keys)
            ))
        elif command == "set_temp_lights":
            values = [int(data[key]) for key in ("screen", "chamber", "status")]
            if any(value < 0 or value > 100 for value in values):
                flask.abort(400, description="Light brightness must be 0 through 100")
            self._send_command(self._with_transaction(
                "@RME LIGHT TEMP screen=%d chamber=%d status=%d" % tuple(values)
            ))
        elif command == "set_persistent_lights":
            values = [int(data[key]) for key in ("screen", "chamber", "status")]
            if any(not self._valid_packed_brightness(value) for value in values):
                flask.abort(400, description="Each packed light-state byte must be 0 through 100")
            self._send_command(self._with_transaction(
                "@RME LIGHT SET screen=0x%08X chamber=0x%08X status=0x%08X" % tuple(values)
            ))
        elif command == "set_filament":
            _, provider_name = self._active_spool_provider()
            if provider_name in ("spoolmanager", "spoolman"):
                flask.abort(
                    409,
                    description="Firmware filament slots are managed by %s"
                    % ("SpoolManager" if provider_name == "spoolmanager" else "Spoolman"),
                )
            slot = int(data["slot"])
            name = str(data["name"])
            temperatures = [int(data[key]) for key in ("nozzle", "preheat", "bed")]
            visible = 1 if bool(data["visible"]) else 0
            if slot < 0 or slot > 7 or not name or len(name) > 7 or " " in name:
                flask.abort(400, description="Filament needs slot 0-7 and a 1-7 character name without spaces")
            if any(value < 0 or value > 500 for value in temperatures):
                flask.abort(400, description="Filament temperatures must be 0 through 500 C")
            self._send_command(self._with_transaction(
                "@RME FILAMENT SET slot=%d name=%s nozzle=%d preheat=%d bed=%d visible=%d"
                % (slot, name, temperatures[0], temperatures[1], temperatures[2], visible)
            ))
        elif command == "stage_firmware":
            self._start_firmware_upload(data["filename"])
        elif command == "stage_and_flash_firmware":
            self._start_firmware_upload(data["filename"], flash_after_stage=True)
        elif command == "stage_octoprint_firmware":
            self._start_octoprint_firmware_upload(data["path"])
        elif command == "stage_and_flash_octoprint_firmware":
            self._start_octoprint_firmware_upload(
                data["path"], flash_after_stage=True
            )
        elif command == "unstage_firmware":
            self._unstage_firmware()
        elif command == "cancel_firmware":
            if self._uploader:
                self._uploader.cancel()
            with self._state_lock:
                firmware_status = self._state["firmware"].get("status")
                file_firmware_active = firmware_status in (
                    "starting", "uploading", "verifying"
                )
            if firmware_status == "queued":
                # Do not abort the unrelated USB operation currently ahead of
                # this transfer. The queued worker will observe this before its
                # first WRITE_BEGIN command.
                self._firmware_file_cancel.set()
                self._firmware_state_changed(status="canceling")
            if file_firmware_active and self._file_service and self._file_service.busy:
                self._file_service.cancel()
        elif command == "flash_firmware":
            self._flash_firmware()
        elif command == "confirm_printer_reboot":
            if self._clear_transport_recovery("user confirmed printer reboot"):
                try:
                    operational = bool(self._printer and self._printer.is_operational())
                except Exception:
                    operational = False
                if operational:
                    self._defer(self._send_command, "@RME MACHINE QUERY")
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
        elif command == "storage_download":
            permission = getattr(Permissions, "FILES_DOWNLOAD", Permissions.STATUS)
            if not permission.can():
                flask.abort(403)
            self._start_storage_download(data["path"], data["target"])
        elif command == "partial_resume":
            self._resume_partial_transfer()
        elif command == "partial_discard":
            self._discard_partial_transfer()
        elif command == "partial_cleanup":
            if not getattr(Permissions, "FILES_DELETE", Permissions.CONTROL).can():
                flask.abort(403)
            self._cleanup_named_partial(data["path"])
        return flask.jsonify(self._public_state())

    @staticmethod
    def file_extension_hook(*args, **kwargs):
        """Expose RME firmware and Buddy dumps in OctoPrint's Files UI."""
        return {"model": {"rme_artifact": ["bbf", "bin"]}}

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

    @octoprint.plugin.BlueprintPlugin.route("/storage/download", methods=["POST"])
    @api_errors
    def download_storage_file(self):
        """Start a progress-reporting printer-to-Pi download job."""
        permission = getattr(Permissions, "FILES_DOWNLOAD", Permissions.STATUS)
        if not permission.can():
            flask.abort(403)
        data = flask.request.get_json(silent=True) or {}
        job = self._start_storage_download(
            data.get("path", ""), data.get("target", "pi")
        )
        response = flask.jsonify({"job": job, "state": self._public_state()})
        response.status_code = 202
        return response

    @octoprint.plugin.BlueprintPlugin.route(
        "/storage/downloads/<job_id>", methods=["GET"]
    )
    @api_errors
    def serve_storage_download(self, job_id):
        """Serve a completed Pi copy without touching the serial connection."""
        permission = getattr(Permissions, "FILES_DOWNLOAD", Permissions.STATUS)
        if not permission.can():
            flask.abort(403)
        with self._state_lock:
            item = next(
                (
                    entry
                    for entry in self._state["storage"].get("downloads", [])
                    if entry.get("id") == job_id
                ),
                None,
            )
        if item is None:
            flask.abort(404)
        path = os.path.join(self._download_directory, item["stored_name"])
        if not os.path.isfile(path):
            flask.abort(404)
        return flask.send_file(
            path, mimetype="application/octet-stream", as_attachment=True,
            download_name=item["name"], conditional=True,
        )

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
                self._write_file_with_manifest(
                    temporary, remote_path, kind="file", progress=self._storage_progress
                )
                self._set_storage_status("ready", progress=100, error=None)
                self._defer(self._refresh_storage_after_change, directory)
            except Exception as exc:
                self._set_storage_status("error", progress=None, error=str(exc))
                raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return flask.jsonify(self._public_state())

    # A separate multipart route is needed for browser-to-Pi BBF upload.
    @octoprint.plugin.BlueprintPlugin.route("/firmware", methods=["POST"])
    @api_errors
    def upload_firmware(self):
        """Store one browser-uploaded BBF and return machine-readable errors."""
        if not Permissions.CONTROL.can():
            flask.abort(403)
        self._require_print_idle("Firmware uploads")
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
                recovery_required = bool(
                    self._state["firmware"].get("recovery_required")
                )
                self._state["connected"] = True
                self._state["supported"] = False
                self._state["session"] = {
                    "active": False, "legacy": True, "last_seq": 0,
                    "configuration_revision": 0,
                }
                self._stats_supported = None
                self._stats_probe_sent = False
                self._priority_controls_sent.clear()
                self._firmware_completed_controls.clear()
                self._state["stats"] = {"supported": None, "updated": None, "values": {}}
                # Never display a previous printer's tool/loadout as current
                # while capability discovery for this connection is pending.
                self._state["loaded_filaments"] = []
                self._state["manufacturers"] = {"profiles": [], "loaded": []}
                self._state["active_tool"] = self._empty_state()["active_tool"]
                # Workflow records are connection-local. Replaying a persisted
                # firmware handoff after reconnect can claim that USB is about
                # to disappear even though no current M997 is running.
                self._state["workflow"] = None
                self._state["prompt"] = None
                self._suppressed_refresh_transactions.clear()
                self._provider_firmware_signature = None
                self._transfer_conflict_cancel = False
            self._publish()
            if recovery_required:
                self._logger.warning(
                    "RME transport remains locked pending a confirmed printer reboot"
                )
                self._defer(self._disconnect_for_transport_recovery)
            else:
                self._send_command("@RME MACHINE QUERY")
                self._defer(self._sync_spoolmanager, True)
        elif event in (Events.DISCONNECTING, Events.DISCONNECTED):
            if (
                event == Events.DISCONNECTING
                and self._state.get("supported")
                and not self._transport_recovery_required()
            ):
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
                    supported=False, status="printer disconnected", progress=None,
                    entries=[], native_files=[],
                )
                self._suppressed_refresh_transactions.clear()
                self._provider_firmware_signature = None
                self._transfer_conflict_cancel = False
            self._publish()
        elif event == Events.PRINT_STARTED:
            if self._printer_transfer_active():
                self._stop_print_started_during_transfer()
                return
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
            with self._state_lock:
                transfer_conflict = self._transfer_conflict_cancel
            if transfer_conflict:
                # The transfer interlock uses a direct OctoPrint job hold,
                # separate from the tool-map hold tracked above. Release it so
                # OctoPrint can drain its cancel transition and leave
                # Cancelling instead of wedging the send loop indefinitely.
                self._release_transfer_conflict_hold()
                return
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
                self._transfer_conflict_cancel = False
            self._defer(self._resume_background_queries)

    def gcode_received_hook(self, comm_instance, line, *args, **kwargs):
        """Observe RME records without blocking or bypassing OctoPrint's queue."""
        if (
            str(line or "").strip().lower() == "start"
            and self._transport_recovery_required()
        ):
            self._clear_transport_recovery("printer startup banner observed")
            self._defer(self._send_command, "@RME MACHINE QUERY")
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
        if self._transport_recovery_required():
            self._logger.warning(
                "Blocked @RME %s while printer reboot recovery is required",
                parameters,
            )
            return
        raw_match = re.match(r"^FILE RAW_SESSION token=([0-9a-f]{32})$", parameters)
        if raw_match:
            token = raw_match.group(1)
            with self._binary_frame_lock:
                session = self._binary_session
            if session is None or session["token"] != token:
                self._logger.error("RME binary session marker expired before transmission")
                return
            try:
                serial_port = getattr(comm_instance, "_serial", None)
                if serial_port is None or not callable(getattr(serial_port, "write", None)):
                    raise RuntimeError("OctoPrint raw serial transport is unavailable")
                while True:
                    # Hold OctoPrint's writer until COMPLETE/ABORTED queues the
                    # sentinel. Releasing it on a timeout lets ordinary line
                    # traffic corrupt firmware's active raw-frame decoder.
                    pending = session["queue"].get()
                    if pending is None:
                        return
                    frame = pending["frame"]
                    try:
                        written = 0
                        while written < len(frame):
                            count = serial_port.write(frame[written:])
                            if count is None:
                                written = len(frame)
                                continue
                            count = int(count)
                            if count <= 0:
                                raise RuntimeError(
                                    "OctoPrint raw serial transport made no progress"
                                )
                            written += count
                        # Drain each complete frame to the CDC endpoint. Current
                        # Buddy bounds and snapshots its parser, so no host-side
                        # delay is needed between its negotiated window frames.
                        flush = getattr(serial_port, "flush", None)
                        if callable(flush):
                            flush()
                    except Exception as exc:
                        pending["error"] = exc
                    finally:
                        pending["event"].set()
                    # Zero-payload frames only request finalize/abort. Keep the
                    # writer reserved until the matching receive record causes
                    # the file service to release this session explicitly.
            except Exception as exc:
                self._logger.error("RME binary send session failed: %s", exc)
            finally:
                with self._binary_frame_lock:
                    if self._binary_session is session:
                        self._binary_session = None
                done = session.get("done")
                if done is not None:
                    done.set()
            return
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

    def _begin_binary_transport(self):
        """Reserve OctoPrint's writer thread until raw mode is finalized."""
        token = uuid.uuid4().hex
        with self._binary_frame_lock:
            if self._binary_session is not None:
                raise FileServiceError("A raw printer transfer is already active")
            self._binary_session = {
                "token": token,
                "queue": queue.Queue(),
                "done": threading.Event(),
            }
        return "@RME FILE RAW_SESSION token=%s" % token

    def _send_binary_frame(self, frame):
        """Pass one raw frame to the reserved OctoPrint writer thread."""
        pending = {
            "frame": bytes(frame), "event": threading.Event(), "error": None,
        }
        with self._binary_frame_lock:
            session = self._binary_session
        if session is None:
            raise FileServiceError("Raw printer transport is not active")
        session["queue"].put(pending)
        if not pending["event"].wait(20):
            raise FileServiceError("Timed out writing raw printer data")
        if pending["error"]:
            raise FileServiceError(
                "Could not write raw printer data: %s" % pending["error"]
            )

    def _end_binary_transport(self):
        """Release an armed raw writer when binary negotiation fails."""
        with self._binary_frame_lock:
            session = self._binary_session
        if session is not None:
            session["queue"].put(None)
            done = session.get("done")
            if done is not None and not done.wait(10):
                raise FileServiceError(
                    "Timed out releasing OctoPrint's raw serial writer"
                )

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
            if self._printer_transfer_active():
                self._stop_print_started_during_transfer()
                return None
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

        # OctoPrint has no public hook for replacing its SD file-list backend.
        # Suppress only the native refresh command after positive RME FILE
        # discovery, then repopulate the Files view through the plugin state.
        if str(gcode or "").upper().startswith("M20"):
            with self._state_lock:
                use_rme_files = bool(self._state["storage"].get("supported"))
            if use_rme_files:
                if not self._print_job_active():
                    self._defer(self._refresh_native_storage_files)
                return (None,)

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
                follow_up.append("probe_stats")
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
                previous_lease = bool(self._state["session"].get("active"))
                self._state["session"].update({
                    key: value for key, value in record.items() if key != "record"
                })
                # Build 8 renamed this field to distinguish the RME lease from
                # actual printer activity. Older firmware remains supported.
                lease = record.get("lease", record.get("active"))
                self._state["session"]["active"] = bool(lease)
                self._state["session"]["legacy"] = bool(record.get("legacy"))
                # A fresh session record is authoritative printer state. If
                # the printer remains connected and IDLE after a claimed
                # firmware handoff, no bootloader restart is in progress.
                # Clear that stale handoff before it can cancel a new print.
                printer_state = str(record.get("printer_state", "")).upper()
                firmware_status = self._state["firmware"].get("status")
                workflow = self._state.get("workflow") or {}
                if (
                    printer_state == "IDLE"
                    and not int(
                        self._state.get("storage", {}).get("caps", {}).get(
                            "firmware_status", 0
                        )
                    )
                    and (
                        firmware_status in ("flashing", "restarting")
                        or (
                            firmware_status == "flash_queued"
                            and self._firmware_handoff_started_at
                            and time.monotonic() - self._firmware_handoff_started_at >= 5
                        )
                    )
                    and not self._state["firmware"].get("recovery_required")
                ):
                    self._state["firmware"] = self._empty_state()["firmware"]
                    if workflow.get("workflow") == "firmware_update":
                        self._state["workflow"] = None
                    self._transfer_conflict_cancel = False
                    self._firmware_handoff_started_at = 0
                    follow_up.append("release_transfer_conflict")
                # KEEPALIVE returns the same session record every ten seconds.
                # Only the inactive -> active transition needs the discovery
                # snapshot; treating every acknowledgement as a new session
                # creates an endless full configuration poll loop.
                if lease and not previous_lease:
                    follow_up.append("@RME DIALOG QUERY")
                    follow_up.append("refresh_configuration:all")
                    follow_up.append("reconcile_firmware_stage")
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
                    follow_up.append("refresh_configuration:all")
                self._state["session"]["last_seq"] = sequence
                self._state["workflow"] = dict(record)
                if record.get("type") == "error" or record.get("state") == "waiting":
                    follow_up.append("@RME DIALOG QUERY")
                if workflow_is_terminal(record):
                    if (self._state.get("prompt") or {}).get("kind") == "firmware":
                        self._state["prompt"] = None
            elif kind == "change":
                sequence = int(record.get("seq", 0))
                previous_sequence = int(self._state["session"].get("last_seq", 0))
                revision = int(record.get("revision", 0))
                previous_revision = int(
                    self._state["session"].get("configuration_revision", 0)
                )
                sequence_gap = bool(previous_sequence and sequence != previous_sequence + 1)
                revision_gap = bool(previous_revision and revision != previous_revision + 1)
                transaction = int(record.get("tx", 0) or 0)
                now = time.monotonic()
                self._suppressed_refresh_transactions = {
                    tx: expiry for tx, expiry in self._suppressed_refresh_transactions.items()
                    if expiry > now
                }
                suppress_refresh = bool(
                    record.get("origin") == "host"
                    and self._suppressed_refresh_transactions.pop(transaction, 0) > now
                )
                self._state["session"]["last_seq"] = sequence
                self._state["session"]["configuration_revision"] = revision
                if (
                    not suppress_refresh
                    and record.get("domain") == "manufacturer"
                    and record.get("key") == "custom"
                ):
                    self._state["manufacturers"]["profiles"][:] = [
                        item for item in self._state["manufacturers"]["profiles"]
                        if int(item.get("builtin", 0))
                    ]
                if sequence_gap or revision_gap:
                    follow_up.extend(["@RME SESSION QUERY", "@RME DIALOG QUERY"])
                    follow_up.append("refresh_configuration:all")
                elif not suppress_refresh:
                    follow_up.append(
                        "refresh_configuration:%s" % str(record.get("domain", ""))
                    )
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
            elif kind in ("lock", "theme"):
                self._state[kind] = {
                    key: value for key, value in record.items() if key != "record"
                }
            elif kind == "light":
                light = {
                    key: value for key, value in record.items() if key != "record"
                }
                if int(light.get("schema", 1)) >= 2:
                    light.update(states={}, policy={}, live={})
                self._state["light"] = light
            elif kind == "light_state":
                state_name = str(record.get("state", ""))
                if state_name in ("deep_idle", "idle", "active", "printing"):
                    states = self._state.setdefault("light", {}).setdefault("states", {})
                    states[state_name] = {
                        key: value for key, value in record.items()
                        if key not in ("record", "state")
                    }
            elif kind == "light_policy":
                self._state.setdefault("light", {})["policy"] = {
                    key: value for key, value in record.items() if key != "record"
                }
            elif kind == "light_live":
                self._state.setdefault("light", {})["live"] = {
                    key: value for key, value in record.items() if key != "record"
                }
            elif kind == "stats":
                self._stats_supported = True
                values = dict(self._state.get("stats", {}).get("values") or {})
                values.update({
                    key: value for key, value in record.items() if key != "record"
                })
                self._state["stats"] = {
                    "supported": True,
                    "updated": int(time.time()),
                    "values": values,
                }
            elif kind == "firmware_restart":
                self._firmware_handoff_started_at = time.monotonic()
                self._state["firmware"].update(
                    status="restarting", error=None,
                    reconnect_expected=bool(record.get("reconnect", 0)),
                    candidate=True, armed=True,
                    printer_stage_state="restarting",
                )
            elif kind == "firmware_status":
                self._apply_authoritative_firmware_locked(record)
            elif kind == "firmware_unstaged":
                self._apply_authoritative_firmware_locked({
                    "candidate": 0, "armed": 0, "state": "idle",
                })
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
            elif kind == "manufacturer":
                profile = {key: value for key, value in record.items() if key != "record"}
                profiles = self._state["manufacturers"]["profiles"]
                identity = (int(profile.get("builtin", 0)), int(profile.get("slot", -1)))
                profiles[:] = [
                    item for item in profiles
                    if (int(item.get("builtin", 0)), int(item.get("slot", -1))) != identity
                ]
                profiles.append(profile)
                profiles.sort(key=lambda item: (-int(item.get("builtin", 0)), int(item.get("slot", 0))))
                # The current firmware emits all 50 built-ins before custom
                # profiles and loaded assignments. Its query has no explicit
                # terminator, so the final built-in is the stable completion
                # signal for a short, coalesced provider reconciliation.
                if int(profile.get("builtin", 0)) and int(profile.get("slot", -1)) == 49:
                    follow_up.append("sync_manufacturer_profiles")
            elif kind == "manufacturer_loaded":
                loaded_manufacturer = {
                    "tool": int(record.get("tool", 0)),
                    "name": "" if str(record.get("name", "")).lower() == "none" else str(record.get("name", "")),
                }
                loaded = self._state["manufacturers"]["loaded"]
                loaded[:] = [
                    item for item in loaded
                    if int(item.get("tool", -1)) != loaded_manufacturer["tool"]
                ]
                loaded.append(loaded_manufacturer)
                for item in self._state["loaded_filaments"]:
                    if int(item.get("tool", -1)) == loaded_manufacturer["tool"]:
                        item["vendor"] = loaded_manufacturer["name"]
            elif kind == "loaded_filament":
                loadout = {
                    key: value for key, value in record.items() if key != "record"
                }
                if str(loadout.get("vendor", "")).lower() == "none":
                    loadout["vendor"] = ""
                # Provider metadata remains authoritative when the firmware's
                # seven-character alias identifies a published spool.
                provider_match = next(
                    (
                        item for item in self._state["spoolmanager"].get("published", [])
                        if item.get("alias") == loadout.get("material")
                    ),
                    None,
                )
                if provider_match:
                    loadout.update(
                        firmware_alias=loadout.get("material", ""),
                        material=provider_match.get("material", loadout.get("material", "")),
                        vendor=provider_match.get("vendor", ""),
                        display_name=provider_match.get("display_name", ""),
                        database_id=provider_match.get("database_id"),
                        provider=self._state["spoolmanager"].get("provider"),
                    )
                else:
                    loadout.update(
                        vendor=loadout.get("vendor", ""),
                        display_name="", provider="RME firmware",
                    )
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
        self._schedule_publish()
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
            elif item == "sync_manufacturer_profiles":
                self._schedule_manufacturer_profile_sync()
            elif item == "probe_stats":
                self._defer(self._probe_stats)
            elif item == "reconcile_firmware_stage":
                self._defer(self._reconcile_firmware_stage)
            elif item == "release_transfer_conflict":
                self._defer(self._release_transfer_conflict_hold)
            elif item.startswith("refresh_configuration:"):
                self._schedule_configuration_refresh(item.split(":", 1)[1])
            else:
                self._defer(self._send_command, item)
        if apply_profile:
            self._defer(self._apply_machine_profile)

    # -- Protocol actions ---------------------------------------------------

    def _transport_recovery_required(self):
        with self._state_lock:
            return bool(self._state["firmware"].get("recovery_required"))

    def _clear_transport_recovery(self, evidence):
        """Unlock RME traffic only after observed or user-confirmed reboot."""
        with self._state_lock:
            if not self._state["firmware"].get("recovery_required"):
                return False
            self._state["firmware"].update(
                status="idle", error=None, recovery_required=False,
                progress=0, offset=0, staged_path=None,
                flash_after_stage=False, reconnect_expected=False,
            )
        if self._file_service:
            self._file_service.reset("Printer reboot confirmed")
        self._logger.warning("Released RME transport lock: %s", evidence)
        self._persist_and_publish()
        return True

    def _disconnect_for_transport_recovery(self):
        """Keep OctoPrint offline while firmware raw mode is uncertain."""
        disconnect = getattr(self._printer, "disconnect", None)
        if not callable(disconnect):
            self._logger.error(
                "OctoPrint cannot disconnect the uncertain RME transport"
            )
            return
        try:
            disconnect()
        except Exception:
            self._logger.exception(
                "Could not disconnect the uncertain RME transport"
            )

    def _send_command(self, command):
        if self._transport_recovery_required():
            raise RuntimeError(
                "Printer reboot required; RME command transmission is locked"
            )
        if not self._printer.is_operational():
            raise RuntimeError("Printer is not connected")
        self._printer.commands(command, tags={"plugin:rme_compatibility"})

    def _send_commands(self, commands):
        if self._transport_recovery_required():
            raise RuntimeError(
                "Printer reboot required; RME command transmission is locked"
            )
        if not self._printer.is_operational():
            raise RuntimeError("Printer is not connected")
        self._printer.commands(commands, tags={"plugin:rme_compatibility"})

    def _with_transaction(self, command, suppress_refresh=False):
        """Attach the firmware's nonzero mutation correlation identifier."""
        with self._state_lock:
            self._transaction = (self._transaction + 1) & 0xFFFFFFFF
            if not self._transaction:
                self._transaction = 1
            transaction = self._transaction
            if suppress_refresh:
                now = time.monotonic()
                self._suppressed_refresh_transactions = {
                    tx: expiry for tx, expiry in self._suppressed_refresh_transactions.items()
                    if expiry > now
                }
                self._suppressed_refresh_transactions[transaction] = now + 120
        return "%s tx=%d" % (command, transaction)

    @staticmethod
    def _configuration_queries(domain=None):
        """Return the minimum snapshot queries for one RME change domain."""
        queries = {
            "lock": ["@RME LOCK QUERY"],
            "theme": ["@RME THEME QUERY"],
            "light": ["@RME LIGHT QUERY"],
            "filament": ["@RME FILAMENT QUERY", "M865 Q"],
            "color": ["M865 Q"],
            "manufacturer": ["@RME MANUFACTURER QUERY", "M865 Q"],
            "toolmap": ["@RME TOOLMAP QUERY"],
        }
        if domain in queries:
            return list(queries[domain])
        result = []
        for name in ("lock", "theme", "light", "filament", "manufacturer", "toolmap"):
            for command in queries[name]:
                if command not in result:
                    result.append(command)
        return result

    def _schedule_configuration_refresh(self, domain):
        """Collapse a burst of revision events into minimal domain queries."""
        with self._configuration_timer_lock:
            self._configuration_domains.add(domain or "all")
            if self._configuration_timer is not None:
                return

            def refresh():
                if self._print_job_active():
                    # Preserve the accumulated domains and retry later without
                    # placing snapshot traffic ahead of streamed print G-code.
                    with self._configuration_timer_lock:
                        if self._stop.is_set():
                            self._configuration_timer = None
                            return
                        self._configuration_timer = threading.Timer(2.0, refresh)
                        self._configuration_timer.daemon = True
                        self._configuration_timer.start()
                    return
                with self._configuration_timer_lock:
                    domains = set(self._configuration_domains)
                    self._configuration_domains.clear()
                    self._configuration_timer = None
                commands = self._configuration_queries() if "all" in domains else []
                if not commands:
                    for changed_domain in sorted(domains):
                        for command in self._configuration_queries(changed_domain):
                            if command not in commands:
                                commands.append(command)
                if commands:
                    try:
                        self._send_commands(commands)
                    except Exception:
                        self._logger.exception("RME configuration refresh failed")

            self._configuration_timer = threading.Timer(0.1, refresh)
            self._configuration_timer.daemon = True
            self._configuration_timer.start()

    def _request_priority_control(self, action):
        """Queue one idempotent pause/resume/cancel on OctoPrint's fast path."""
        commands = {"pause": "M601", "resume": "M602", "cancel": "M604"}
        if action not in commands:
            raise ValueError("Unknown priority control action: %s" % action)
        with self._state_lock:
            if not (self._state.get("connected") and self._state.get("supported")):
                return False
            if self._state["firmware"].get("recovery_required"):
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
                "name": firmware.get("display_name") or firmware.get("material", ""),
                "material": firmware.get("material", ""),
                "color": firmware.get("color", ""),
                "color_name": firmware.get("color_name", ""),
                "vendor": firmware.get("vendor", ""),
                "spool_id": str(firmware.get("database_id") or ""),
                "provider": firmware.get("provider") or "RME firmware",
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
        self._schedule_publish()

    def _open_session(self):
        legacy = 1 if self._settings.get_boolean(["legacy_notifications"]) else 0
        self._send_command("@RME SESSION OPEN events=31 legacy=%d" % legacy)

    @staticmethod
    def _valid_color(value):
        if not isinstance(value, str) or len(value) != 7 or value[0] != "#":
            return False
        return all(character in "0123456789abcdefABCDEF" for character in value[1:])

    @staticmethod
    def _valid_packed_brightness(value):
        return 0 <= value <= 0xFFFFFFFF and all(((value >> shift) & 0xFF) <= 100 for shift in (0, 8, 16, 24))

    def _keepalive_loop(self):
        """Renew the required RME lease without polling configuration state."""
        while not self._stop.wait(10):
            with self._state_lock:
                active = self._state["session"].get("active")
                connected = self._state["connected"]
                recovery_required = bool(
                    self._state["firmware"].get("recovery_required")
                )
            # Acknowledged M998 and FILE transactions must not be interleaved
            # with periodic session, statistics, or filament requests.
            transfer_busy = bool(
                (self._uploader and self._uploader.busy)
                or (self._file_service and self._file_service.busy)
            )
            if active and connected and not transfer_busy and not recovery_required:
                try:
                    self._send_command("@RME SESSION KEEPALIVE")
                except Exception:
                    self._logger.debug("RME keepalive could not be queued", exc_info=True)
            if not transfer_busy and not recovery_required:
                if self._stats_supported is None:
                    self._defer(self._probe_stats)

    def _print_job_active(self):
        """Return whether background serial reads must yield to job G-code."""
        try:
            return bool(self._printer.is_printing() or self._printer.is_paused())
        except Exception:
            return False

    def _require_print_idle(self, operation="This operation"):
        """Reject printer-storage and firmware work during an active print."""
        if self._print_job_active():
            raise UploadError("%s are unavailable while a print is active" % operation)

    def _printer_transfer_active(self):
        """Return whether a new print must yield to transfer/flash ownership."""
        with self._state_lock:
            firmware_status = self._state["firmware"].get("status")
            recovery_required = bool(
                self._state["firmware"].get("recovery_required")
            )
        return bool(
            (self._uploader and self._uploader.busy)
            or (self._file_service and self._file_service.busy)
            or firmware_status in (
                "queued", "canceling", "starting", "uploading", "verifying",
                "flash_queued", "flashing", "restarting",
            )
            or recovery_required
        )

    def _stop_print_started_during_transfer(self):
        """Hold and cancel a print that raced an existing transfer or flash."""
        with self._state_lock:
            if self._transfer_conflict_cancel:
                return
            self._transfer_conflict_cancel = True
        try:
            self._printer.set_job_on_hold(True, blocking=False)
        except Exception:
            self._logger.debug("Could not hold conflicting print before cancel", exc_info=True)
        self._logger.warning(
            "Canceling print start because an RME file/firmware operation is active"
        )
        try:
            self._printer.cancel_print(tags={"plugin:rme_compatibility", "rme:transfer_conflict"})
        except TypeError:
            self._printer.cancel_print()
        except Exception:
            self._logger.exception("Could not cancel print conflicting with active transfer")

    def _release_transfer_conflict_hold(self):
        """Release a pre-start hold after firmware proves no handoff exists."""
        try:
            self._printer.set_job_on_hold(False, blocking=False)
        except Exception:
            self._logger.debug("Could not release stale transfer-conflict hold", exc_info=True)

    def _probe_stats(self):
        """Probe statistics support only while the serial job queue is idle."""
        if self._print_job_active():
            return
        with self._state_lock:
            connected = self._state.get("connected")
            supported = self._state.get("supported")
            should_probe = bool(
                connected and supported and self._stats_supported is None
                and not self._stats_probe_sent
            )
            if should_probe:
                self._stats_probe_sent = True
        if should_probe:
            self._send_command("@RME STATS QUERY")

    def _refresh_stats_snapshot(self):
        """Refresh supported statistics at a lifecycle boundary, never on a timer."""
        if self._print_job_active():
            return
        with self._state_lock:
            connected = self._state.get("connected")
            supported = self._state.get("supported")
        if connected and supported and self._stats_supported is True:
            self._send_command("@RME STATS QUERY")
        elif self._stats_supported is None:
            self._probe_stats()

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

    @staticmethod
    def _gcode_text(value, maximum):
        """Return bounded text that cannot escape an M865 quoted argument."""
        return (
            str(value or "")
            .replace('"', "'")
            .replace("\r", " ")
            .replace("\n", " ")[:maximum]
        )

    def _provider_profile_commands(self, published, provider_name):
        """Mirror external color and manufacturer profiles into firmware.

        The external provider remains authoritative. Firmware custom color and
        manufacturer slots are merely a presentation/cache layer that lets its
        local load picker show the provider's names and report them back.
        """
        if provider_name not in ("spoolmanager", "spoolman"):
            return [], {}

        commands = []
        seen_colors = set()
        color_slot = 0
        for item in published:
            color = str(item.get("color") or "#808080").lower()
            if color in seen_colors or not self._valid_color(color) or color_slot >= 8:
                continue
            seen_colors.add(color)
            name = self._gcode_text(
                item.get("color_name") or item.get("display_name") or "Color %d" % color_slot,
                15,
            )
            commands.append('M865 V%d O"%s" N"%s"' % (color_slot, color, name))
            color_slot += 1

        with self._state_lock:
            profiles = copy.deepcopy(self._state.get("manufacturers", {}).get("profiles", []))
        builtin = {
            str(item.get("name", "")).casefold()
            for item in profiles if int(item.get("builtin", 0))
        }
        custom = {
            int(item.get("slot", -1)): str(item.get("name", ""))
            for item in profiles if not int(item.get("builtin", 0))
        }
        desired_custom = []
        manufacturer_for_spool = {}
        for item in published:
            vendor = self._gcode_text(item.get("vendor"), 23)
            if not vendor:
                manufacturer_for_spool[item["database_id"]] = "none"
                continue
            if vendor.casefold() in builtin:
                manufacturer_for_spool[item["database_id"]] = vendor
                continue
            existing = next(
                (name for name in desired_custom if name.casefold() == vendor.casefold()),
                None,
            )
            if existing is None and len(desired_custom) < 8:
                desired_custom.append(vendor)
                existing = vendor
            manufacturer_for_spool[item["database_id"]] = existing or "none"

        # An empty list means the initial MANUFACTURER QUERY has not returned
        # yet. Avoid guessing that built-ins are custom; assignments to known
        # built-ins still work and the next explicit/provider sync fills custom
        # profiles after discovery.
        if profiles:
            for slot in range(8):
                wanted = desired_custom[slot] if slot < len(desired_custom) else None
                current = custom.get(slot)
                if current and (wanted is None or current.casefold() != wanted.casefold()):
                    commands.append(self._with_transaction(
                        "@RME MANUFACTURER DELETE slot=%d" % slot,
                        suppress_refresh=True,
                    ))
                if wanted and (not current or current.casefold() != wanted.casefold()):
                    commands.append(self._with_transaction(
                        "@RME MANUFACTURER CREATE slot=%d name=%s"
                        % (slot, quote(wanted, safe="-_.~")),
                        suppress_refresh=True,
                    ))
        return commands, manufacturer_for_spool

    def _schedule_manufacturer_profile_sync(self):
        """Reconcile once after the firmware's manufacturer query burst."""
        if self._print_job_active():
            with self._state_lock:
                self._manufacturer_sync_pending = True
            return
        with self._manufacturer_sync_timer_lock:
            if self._manufacturer_sync_timer is not None:
                self._manufacturer_sync_timer.cancel()

            def synchronize():
                with self._manufacturer_sync_timer_lock:
                    self._manufacturer_sync_timer = None
                if self._stop.is_set():
                    return
                if self._print_job_active():
                    with self._state_lock:
                        self._manufacturer_sync_pending = True
                    return
                # Query completion is reconciliation, not a provider edit.
                # Only publish when this connection has not received the
                # current provider snapshot yet.
                self._sync_spoolmanager(False, True)

            self._manufacturer_sync_timer = threading.Timer(0.15, synchronize)
            self._manufacturer_sync_timer.daemon = True
            self._manufacturer_sync_timer.start()

    def _resume_background_queries(self):
        """Catch up deferred telemetry and provider metadata after a job."""
        self._refresh_stats_snapshot()
        with self._state_lock:
            synchronize_manufacturers = self._manufacturer_sync_pending
            self._manufacturer_sync_pending = False
        if synchronize_manufacturers:
            self._schedule_manufacturer_profile_sync()

    def _sync_spoolmanager(self, force=False, push_to_firmware=True):
        """Reconcile the active inventory provider with eight firmware presets.

        The firmware limits user material names to seven characters and exposes
        eight slots. Seven slots receive stable database-ID aliases; slot seven
        is reserved for ``NEW``. Full metadata remains visible in OctoPrint.
        """
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

            firmware_signature = (
                tuple(
                    (
                        item.get("slot"), item.get("alias"), item.get("database_id"),
                        item.get("material"), item.get("vendor"), item.get("color"),
                        item.get("color_name"), item.get("display_name"),
                        int(item.get("nozzle_temperature", 0)),
                        int(item.get("bed_temperature", 0)),
                    )
                    for item in published
                ),
                tuple(
                    (int(item.get("tool", 0)), item.get("database_id"))
                    for item in selected
                ),
            )
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
            profile_commands, manufacturer_by_spool = self._provider_profile_commands(
                published, provider_name
            )
            metadata_changed = (
                force or self._provider_firmware_signature != firmware_signature
            )
            if should_push and metadata_changed and profile_commands:
                self._send_commands(profile_commands)
            if should_push and metadata_changed:
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
                commands.append(
                    "@RME FILAMENT SET slot=7 name=NEW nozzle=215 preheat=170 bed=60 visible=1"
                )
                commands = [
                    self._with_transaction(command, suppress_refresh=True)
                    for command in commands
                ]
                self._send_commands(commands)
            if should_push and metadata_changed:
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
                        assignments.append(self._with_transaction(
                            "@RME MANUFACTURER ASSIGN tool=%d name=none" % tool,
                            suppress_refresh=True,
                        ))
                for selected_item in selected:
                    item = slot_by_id.get(selected_item["database_id"])
                    if item:
                        assignments.append(
                            'M865 U%d L%d O"%s"'
                            % (item["slot"], selected_item["tool"], item["color"])
                        )
                        manufacturer = manufacturer_by_spool.get(
                            item["database_id"], "none"
                        )
                        assignments.append(self._with_transaction(
                            "@RME MANUFACTURER ASSIGN tool=%d name=%s"
                            % (selected_item["tool"], quote(manufacturer, safe="-_.~")),
                            suppress_refresh=True,
                        ))
                assignments.append("M865 Q")
                self._send_commands(assignments)
                self._provider_firmware_signature = firmware_signature

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
        # Older settings may still contain ``internal``. Once an external
        # provider is installed it owns the inventory unconditionally; the
        # local backend is only a no-provider fallback.
        if preference == "internal":
            for name in ("spoolmanager", "spoolman"):
                candidate = providers[name]
                if candidate is not None and candidate.available():
                    return candidate, name
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

    def _sync_filaments_to_printer(self):
        """Publish provider presets and assignments after explicit acceptance."""
        with self._state_lock:
            self._state["spoolmanager"]["pending_provider_sync"] = None
        self._sync_spoolmanager(True, True)

    def _select_spool_from_octoprint(self, tool, database_id):
        """Select from the RME tab and update both provider and firmware."""
        tool = self._tool_index(tool)
        database_id = int(database_id)
        provider, _ = self._active_spool_provider()
        self._mark_expected_provider_event(tool, database_id)
        provider.select(tool, database_id)
        self._sync_spoolmanager(True)

    def _deselect_spool_from_octoprint(self, tool):
        """Clear one tool in the active provider and on the printer."""
        tool = self._tool_index(tool)
        provider, _ = self._active_spool_provider()
        self._mark_expected_provider_event(tool, None)
        provider.deselect(tool)
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
                self._sync_spoolmanager(True, True)
            else:
                expected_vendor = self._gcode_text(match.get("vendor"), 23)
                expected_color_name = self._gcode_text(
                    match.get("color_name") or match.get("display_name"), 15
                )
                mismatch = (
                    str(record.get("color", "")).lower()
                    != str(match.get("color", "")).lower()
                    or (
                        record.get("vendor") not in (None, "", "None")
                        and str(record.get("vendor", "")).casefold()
                        != expected_vendor.casefold()
                    )
                    or (
                        record.get("color_name") not in (None, "", "None", "Custom")
                        and str(record.get("color_name", "")).casefold()
                        != expected_color_name.casefold()
                    )
                )
                if mismatch:
                    # External metadata owns the slot. A front-panel edit to a
                    # linked alias is corrected back to the provider's color
                    # and manufacturer rather than forking a local RME record.
                    self._sync_spoolmanager(True, True)
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
                "vendor": "" if str(record.get("vendor", "")).lower() == "none" else record.get("vendor", ""),
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

    @staticmethod
    def _public_manifest(manifest, status=None, error=None):
        """Return provenance and recovery state without exposing a Pi path."""
        if not manifest:
            return None
        value = {
            key: manifest.get(key) for key in (
                "remote_path", "source_name", "size", "sha256", "transport",
                "kind", "offset", "resumed", "created",
            )
        }
        value["status"] = status or manifest.get("status") or "interrupted"
        value["error"] = error
        value["source_available"] = bool(
            manifest.get("source_path") and os.path.isfile(manifest["source_path"])
        )
        return value

    def _stage_transfer_source(self, local_path):
        """Retain upload bytes across OctoPrint and printer reconnects."""
        source_name = secure_filename(os.path.basename(str(local_path))) or "upload.bin"
        destination = os.path.join(
            self._transfer_directory, uuid.uuid4().hex + "--" + source_name
        )
        shutil.copyfile(local_path, destination)
        with open(destination, "rb") as retained:
            os.fsync(retained.fileno())
        return destination, source_name

    def _write_file_with_manifest(
        self, local_path, remote_path, kind="file", resume_manifest=None, **kwargs
    ):
        """Write through FILE while durably preserving current-firmware provenance."""
        # Lightweight unit/plugin harnesses may exercise transfer routing
        # without running OctoPrint's startup lifecycle.
        if self._manifest_store is None:
            return self._file_service.write_file(local_path, remote_path, **kwargs)
        manifest = dict(resume_manifest or {})
        created_source = False
        if resume_manifest:
            source_path = manifest.get("source_path")
            if not source_path or not os.path.isfile(source_path):
                raise FileServiceError(
                    "The saved source file is unavailable; discard the printer partial"
                )
        else:
            if self._manifest_store.get():
                raise FileServiceError(
                    "An interrupted printer upload must be resumed or discarded first"
                )
            source_path, source_name = self._stage_transfer_source(local_path)
            created_source = True
            manifest = {
                "version": 1,
                "source_path": source_path,
                "source_name": source_name,
                "remote_path": str(remote_path).replace("\\", "/").lstrip("/"),
                "kind": str(kind),
                "created": int(time.time()),
                "offset": 0,
            }

        def update_manifest(transport, size, digest):
            if resume_manifest and (
                int(manifest.get("size", -1)) != int(size)
                or str(manifest.get("sha256", "")).lower() != str(digest).lower()
            ):
                raise FileServiceError(
                    "The retained source no longer matches the interrupted upload"
                )
            manifest.update(
                transport=transport, size=int(size), sha256=str(digest).lower(),
                status="transferring",
            )
            self._manifest_store.save(manifest)
            with self._state_lock:
                self._state["storage"]["partial"] = self._public_manifest(
                    manifest, status="transferring"
                )
            self._persist_and_publish()

        def complete_manifest():
            self._manifest_store.clear()
            with self._state_lock:
                self._state["storage"]["partial"] = None
            try:
                os.unlink(source_path)
            except FileNotFoundError:
                pass
            self._persist_and_publish()

        try:
            return self._file_service.write_file(
                source_path, remote_path,
                manifest_update=update_manifest,
                manifest_complete=complete_manifest,
                **kwargs
            )
        except Exception as exc:
            saved = self._manifest_store.get()
            if not saved and created_source:
                try:
                    os.unlink(source_path)
                except FileNotFoundError:
                    pass
            elif saved:
                with self._state_lock:
                    self._state["storage"]["partial"] = self._public_manifest(
                        saved, status="interrupted", error=str(exc)
                    )
                self._persist_and_publish()
            if getattr(
                self._file_service, "transport_mode_uncertain",
                getattr(self._file_service, "binary_mode_uncertain", False),
            ):
                self._firmware_state_changed(
                    status="error", recovery_required=True,
                    error=(
                        "Printer communication is locked because transfer teardown "
                        "was not confirmed. Power-cycle the printer, then confirm "
                        "the reboot in RME settings. No further RME commands will be sent."
                    ),
                )
                self._disconnect_for_transport_recovery()
            raise

    def _set_partial_status(self, status, error=None):
        manifest = self._manifest_store.get()
        with self._state_lock:
            self._state["storage"]["partial"] = self._public_manifest(
                manifest, status=status, error=error
            )
        self._persist_and_publish()

    def _resume_partial_transfer(self):
        self._require_storage()
        manifest = self._manifest_store.get()
        if not manifest:
            raise FileServiceError("There is no interrupted RME upload to resume")
        if self._partial_thread and self._partial_thread.is_alive():
            raise FileServiceError("Partial-file recovery is already active")
        self._set_partial_status("queued")

        def resume():
            try:
                progress = (
                    self._firmware_file_progress
                    if manifest.get("kind") == "firmware" else self._storage_progress
                )
                if manifest.get("kind") == "firmware":
                    self._firmware_state_changed(
                        status="starting", filename=manifest.get("source_name"),
                        size=int(manifest.get("size", 0)),
                        sha256=manifest.get("sha256"), error=None,
                    )
                self._write_file_with_manifest(
                    manifest["source_path"], manifest["remote_path"],
                    kind=manifest.get("kind", "file"), resume_manifest=manifest,
                    progress=progress,
                    finalizing=(
                        lambda: self._firmware_state_changed(
                            status="verifying", offset=int(manifest["size"]), progress=100
                        )
                    ) if manifest.get("kind") == "firmware" else None,
                )
                if manifest.get("kind") == "firmware":
                    self._confirm_completed_firmware_manifest(manifest)
                else:
                    self._set_storage_status("ready", progress=100, error=None)
                    self._defer(
                        self._refresh_storage_after_change,
                        self._parent_storage_path(manifest["remote_path"]),
                    )
            except Exception as exc:
                self._logger.exception("RME partial upload resume failed")
                if manifest.get("kind") == "firmware":
                    self._firmware_state_changed(status="error", error=str(exc))
                self._set_partial_status("interrupted", str(exc))

        self._partial_thread = threading.Thread(
            target=resume, name="rme-partial-resume", daemon=True
        )
        self._partial_thread.start()

    def _discard_partial_transfer(self):
        self._require_storage()
        manifest = self._manifest_store.get()
        if not manifest:
            raise FileServiceError("There is no interrupted RME upload to discard")
        if self._partial_thread and self._partial_thread.is_alive():
            raise FileServiceError("Partial-file recovery is already active")
        self._set_partial_status("discarding")

        def discard():
            try:
                self._file_service.discard_partial(
                    manifest["remote_path"], manifest["size"], manifest["sha256"]
                )
                self._manifest_store.clear()
                try:
                    os.unlink(manifest["source_path"])
                except FileNotFoundError:
                    pass
                with self._state_lock:
                    self._state["storage"]["partial"] = None
                    if manifest.get("kind") == "firmware":
                        self._state["firmware"] = self._empty_state()["firmware"]
                self._persist_and_publish()
            except Exception as exc:
                self._logger.exception("RME partial upload discard failed")
                self._set_partial_status("interrupted", str(exc))

        self._partial_thread = threading.Thread(
            target=discard, name="rme-partial-discard", daemon=True
        )
        self._partial_thread.start()

    def _cleanup_named_partial(self, path):
        self._require_storage()
        if self._manifest_store.get():
            raise FileServiceError(
                "Resolve the known interrupted upload before orphan cleanup"
            )
        self._file_service.cleanup_orphan(path)
        self._set_storage_status("ready", progress=None, error=None)

    def _require_storage(self):
        with self._state_lock:
            connected = self._state["connected"] and self._state["supported"]
        if not connected or not self._file_service:
            raise FileServiceError("An RME printer is not connected")
        if self._uploader and self._uploader.busy:
            raise FileServiceError("Firmware staging is already using the serial transfer channel")
        if self._print_job_active():
            raise FileServiceError(
                "Printer USB transfers are unavailable while a print is active"
            )

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
            self._probe_interrupted_transfer()
            if getattr(
                self._file_service, "transport_mode_uncertain",
                getattr(self._file_service, "binary_mode_uncertain", False),
            ):
                return
            self._reconcile_firmware_stage()
            self._refresh_storage("/")
            self._refresh_native_storage_files()
        except Exception as exc:
            with self._state_lock:
                self._state["storage"].update(
                    supported=False, status="unsupported", progress=None, error=str(exc)
                )
            self._persist_and_publish()

    def _probe_interrupted_transfer(self):
        """Reopen and suspend a hidden partial to recover its committed offset."""
        manifest = self._manifest_store.get() if self._manifest_store else None
        if not manifest or (self._partial_thread and self._partial_thread.is_alive()):
            return
        try:
            ready = self._file_service.probe_partial(
                manifest["remote_path"], manifest["size"], manifest["sha256"]
            )
            manifest["offset"] = int(ready.get("offset", 0))
            manifest["resumed"] = bool(int(ready.get("resumed", 0)))
            manifest["status"] = "interrupted"
            self._manifest_store.save(manifest)
            with self._state_lock:
                self._state["storage"]["partial"] = self._public_manifest(
                    manifest, status="interrupted"
                )
            self._persist_and_publish()
        except Exception as exc:
            self._logger.warning(
                "Could not inspect interrupted RME upload: %s", exc
            )
            self._set_partial_status("interrupted", str(exc))
            if getattr(
                self._file_service, "transport_mode_uncertain",
                getattr(self._file_service, "binary_mode_uncertain", False),
            ):
                self._firmware_state_changed(
                    status="error", recovery_required=True,
                    error=(
                        "Printer communication is locked because transfer teardown "
                        "was not confirmed. Power-cycle the printer, then confirm "
                        "the reboot in RME settings. No further RME commands will be sent."
                    ),
                )
                self._disconnect_for_transport_recovery()

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
                self._refresh_native_storage_files()
        except Exception as exc:
            self._set_storage_status("error", progress=None, error=str(exc))
            raise

    def _refresh_storage_after_change(self, directory):
        """Refresh both storage views without changing a completed transfer result."""
        try:
            self._refresh_storage(directory)
            self._refresh_native_storage_files()
        except Exception:
            self._logger.warning(
                "RME file transfer completed, but the follow-up USB listing failed",
                exc_info=True,
            )

    @staticmethod
    def _native_storage_extension(path):
        """Return the supported native Files-view category for one USB file."""
        extension = os.path.splitext(str(path))[1].lower()
        if extension in (".gcode", ".gco", ".bgcode"):
            return "machinecode"
        if extension in (".bbf", ".bin"):
            return "model"
        return None

    def _refresh_native_storage_files(self):
        """Coalesce native Files-view refreshes from UI and M20 polling."""
        if not self._native_refresh_lock.acquire(False):
            return
        try:
            return self._refresh_native_storage_files_locked()
        finally:
            self._native_refresh_lock.release()

    def _refresh_native_storage_files_locked(self):
        """Build the recursive RME USB index consumed by OctoPrint's Files UI."""
        self._require_storage()
        files = []
        visited = set()

        def walk(directory, depth=0):
            if depth > 24 or directory in visited:
                return
            visited.add(directory)
            for entry in self._file_service.list_directory(directory):
                full_path = self._join_storage_path(directory, entry.get("name", ""))
                if entry.get("type") == "dir":
                    walk(full_path, depth + 1)
                    continue
                category = self._native_storage_extension(full_path)
                if not category:
                    continue
                files.append({
                    "path": full_path.lstrip("/"),
                    "name": os.path.basename(full_path),
                    "size": max(0, int(entry.get("size", 0))),
                    # FILE LIST has no mtime, but OctoPrint's core Files
                    # templates require the date property to exist.
                    "date": None,
                    "category": category,
                })

        self._set_storage_status("indexing", progress=None, error=None)
        try:
            walk("/")
            files.sort(key=lambda item: item["path"].casefold())
            with self._state_lock:
                self._state["storage"].update(
                    native_files=files, status="ready", progress=None,
                    error=None, updated=int(time.time()),
                )
            self._persist_and_publish()
            updated_files = getattr(Events, "UPDATED_FILES", None)
            if updated_files and hasattr(self, "_event_bus"):
                self._event_bus.fire(updated_files, {"type": "printables"})
        except Exception as exc:
            self._set_storage_status("error", progress=None, error=str(exc))
            raise

    def _start_storage_download(self, remote_path, target):
        """Queue one printer-to-Pi transfer and optionally hand it to a browser."""
        self._require_storage()
        target = str(target or "pi").lower()
        if target not in ("pi", "device"):
            raise ValueError("Download target must be pi or device")
        filename = secure_filename(
            os.path.basename(str(remote_path).replace("\\", "/").rstrip("/"))
        ) or "download.bin"
        job_id = uuid.uuid4().hex
        stored_name = job_id + "--" + filename
        job = {
            "id": job_id, "path": str(remote_path), "name": filename,
            "stored_name": stored_name, "target": target, "status": "queued",
            "size": 0, "offset": 0, "progress": 0, "error": None,
            "updated": int(time.time()),
        }
        with self._state_lock:
            self._state["storage"]["download"] = dict(job)
            self._state["storage"].update(
                status="download queued", progress=0, error=None
            )
        self._persist_and_publish()

        def transfer():
            destination = os.path.join(self._download_directory, stored_name)

            def progress(offset, size):
                percentage = round(offset * 100.0 / max(1, size), 2)
                with self._state_lock:
                    current = self._state["storage"].get("download") or {}
                    if current.get("id") == job_id:
                        current.update(
                            status="downloading", offset=offset, size=size,
                            progress=percentage, updated=int(time.time()),
                        )
                        self._state["storage"].update(
                            status="downloading", progress=percentage, error=None
                        )
                now = time.monotonic()
                if now - self._last_storage_publish >= 0.25:
                    self._last_storage_publish = now
                    self._publish()

            try:
                metadata = self._file_service.download_file(
                    remote_path, destination, progress=progress
                )
                complete = dict(
                    job, status="ready", size=int(metadata.get("size", 0)),
                    offset=int(metadata.get("size", 0)), progress=100,
                    updated=int(time.time()),
                )
                with self._state_lock:
                    downloads = self._state["storage"].setdefault("downloads", [])
                    downloads[:] = [item for item in downloads if item.get("id") != job_id]
                    downloads.append(complete)
                    del downloads[:-20]
                    self._state["storage"]["download"] = complete
                    self._state["storage"].update(
                        status="ready", progress=100, error=None
                    )
                self._persist_and_publish()
            except Exception as exc:
                self._logger.exception("RME printer file download failed")
                with self._state_lock:
                    failed = dict(
                        job, status="error", error=str(exc),
                        updated=int(time.time()),
                    )
                    self._state["storage"]["download"] = failed
                    self._state["storage"].update(
                        status="error", progress=None, error=str(exc)
                    )
                self._persist_and_publish()

        threading.Thread(
            target=transfer, name="rme-storage-download", daemon=True
        ).start()
        return job

    def sd_card_upload_hook(
        self, printer, filename, path, start_callback, success_callback,
        failure_callback, *args, **kwargs
    ):
        """Replace OctoPrint's line-based SD upload with verified RME FILE I/O.

        Returning a remote name tells OctoPrint that this plugin owns the
        transfer. Older RME builds without positively advertised FILE WRITE
        support return ``None`` and retain OctoPrint's normal M28/M29 path.
        """
        with self._state_lock:
            use_rme_file = bool(
                self._state.get("connected")
                and self._state.get("supported")
                and self._state["storage"].get("supported")
                and int(self._state["storage"].get("caps", {}).get("write", 0))
            )
        if not use_rme_file or not self._file_service:
            return None
        remote_name_factory = getattr(printer, "_get_free_remote_name", None)
        if not callable(remote_name_factory):
            self._logger.warning("Cannot replace SD upload: OctoPrint remote-name helper is unavailable")
            return None
        remote_name = remote_name_factory(filename)
        if not remote_name:
            return None
        start_callback(filename, remote_name)
        if self._print_job_active():
            self._logger.warning(
                "Rejecting printer USB upload because a print is active"
            )
            failure_callback(filename, remote_name, 0)
            return remote_name

        def transfer():
            started = time.monotonic()
            try:
                self._set_storage_status("uploading", progress=0, error=None)
                self._write_file_with_manifest(
                    path, remote_name, kind="file", progress=self._storage_progress
                )
                self._set_storage_status("ready", progress=100, error=None)
                success_callback(filename, remote_name, time.monotonic() - started)
                self._defer(
                    self._refresh_storage_after_change,
                    self._parent_storage_path(remote_name),
                )
            except Exception as exc:
                self._logger.exception("RME SD-card upload failed")
                self._set_storage_status("error", progress=None, error=str(exc))
                failure_callback(filename, remote_name, time.monotonic() - started)

        threading.Thread(
            target=transfer, name="rme-sdcard-upload", daemon=True
        ).start()
        return remote_name

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
        path = self._firmware_path(filename)
        if not os.path.isfile(path):
            raise UploadError("Firmware file was not found on the Pi")
        self._start_firmware_path(path, flash_after_stage=flash_after_stage)

    def _start_octoprint_firmware_upload(self, path, flash_after_stage=False):
        """Stage a BBF selected in OctoPrint's standard local Files list."""
        logical_path = str(path or "").replace("\\", "/").lstrip("/")
        if (
            not logical_path
            or not logical_path.lower().endswith(".bbf")
            or any(part in ("", ".", "..") for part in logical_path.split("/"))
        ):
            raise UploadError("Select a valid local .BBF firmware file")
        try:
            disk_path = self._file_manager.path_on_disk("local", logical_path)
        except Exception as exc:
            raise UploadError("OctoPrint firmware file was not found") from exc
        if not disk_path or not os.path.isfile(disk_path):
            raise UploadError("OctoPrint firmware file was not found")
        self._start_firmware_path(
            disk_path, flash_after_stage=flash_after_stage
        )

    def _start_firmware_path(self, path, flash_after_stage=False):
        """Start the guarded firmware workflow for one trusted local BBF."""
        self._require_print_idle("Firmware transfers")
        if not self._state.get("supported"):
            raise UploadError("The connected printer did not complete the RME handshake")
        if not str(path).lower().endswith(".bbf") or not os.path.isfile(path):
            raise UploadError("Firmware file was not found on the Pi")
        metadata = firmware_metadata(path)
        with self._firmware_action_lock:
            if self._manifest_store and self._manifest_store.get():
                raise UploadError(
                    "Resume or discard the interrupted printer upload first"
                )
            if self._uploader.busy or (
                self._firmware_file_thread and self._firmware_file_thread.is_alive()
            ):
                raise UploadError("A firmware transfer is already active")
            with self._state_lock:
                self._state["firmware"]["error"] = None
            with self._state_lock:
                caps = dict(self._state["storage"].get("caps") or {})
                file_supported = bool(
                    self._state["storage"].get("supported") and int(caps.get("write", 0))
                )
            # The current firmware contract requires FILE WRITE. Probe once if
            # the UI has not yet cached CAPS, then fail clearly instead of
            # silently dropping to the obsolete M998 upload protocol.
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
                except Exception as exc:
                    raise UploadError(
                        "The printer did not expose the current RME FILE protocol"
                    ) from exc
            if not file_supported:
                raise UploadError(
                    "The printer does not expose the current RME FILE write protocol"
                )
            with self._state_lock:
                self._state["firmware"]["flash_after_stage"] = bool(flash_after_stage)
            # ``write_file`` owns the same serialized operation lock as
            # directory listing and capability probes. Starting the worker
            # here lets a short UI refresh finish first instead of exposing
            # a transient HTTP 409 to the user.
            self._firmware_file_cancel.clear()
            self._firmware_state_changed(
                status="queued", filename=metadata["name"], size=metadata["size"],
                sha256=metadata["sha256"], offset=0, progress=0, error=None,
                staged_path=None,
            )
            self._firmware_file_thread = threading.Thread(
                target=self._run_file_firmware_upload,
                args=(path, metadata),
                name="rme-file-firmware-upload",
                daemon=True,
            )
            self._firmware_file_thread.start()

    def _run_file_firmware_upload(self, path, metadata):
        """Stage and verify ``FWUPD.BBF`` through the current FILE service."""
        try:
            self._write_file_with_manifest(
                path,
                "FWUPD.BBF",
                kind="firmware",
                progress=self._firmware_file_progress,
                starting=lambda: self._firmware_state_changed(status="starting"),
                cancel_check=self._firmware_file_cancel.is_set,
                finalizing=lambda: self._firmware_state_changed(
                    status="verifying", offset=metadata["size"], progress=100
                ),
            )
            self._confirm_completed_firmware_manifest(metadata)
        except Exception as exc:
            self._logger.exception("RME FILE firmware transfer failed")
            recovery_required = bool(
                self._file_service
                and getattr(
                    self._file_service, "transport_mode_uncertain",
                    getattr(self._file_service, "binary_mode_uncertain", False),
                )
            )
            error = str(exc)
            if recovery_required:
                error = (
                    "Printer communication is locked because transfer teardown "
                    "was not confirmed. Power-cycle the printer, then confirm "
                    "the reboot in RME settings. No further RME commands will be sent."
                )
            self._firmware_state_changed(
                status="error", error=error,
                recovery_required=recovery_required,
            )
            if (
                recovery_required
                and self._printer
            ):
                # No ASCII command can repair a firmware receiver that did not
                # acknowledge the raw abort frame. Close OctoPrint's descriptor
                # so it cannot keep feeding line commands into raw mode. The
                # operator-facing error retains the required reconnect/power
                # cycle instruction.
                self._logger.error(
                    "Disconnecting after unconfirmed RME transfer teardown"
                )
                self._disconnect_for_transport_recovery()

    def _confirm_completed_firmware_manifest(self, metadata):
        """Require the authoritative protected candidate after FILE completion."""
        staged = self._file_service.firmware_status()
        if (
            not int(staged.get("candidate", 0))
            or int(staged.get("armed", 0))
            or str(staged.get("state", "")).lower() != "ready"
            or int(staged.get("size", -1)) != int(metadata["size"])
            or str(staged.get("sha256", "")).lower()
            != str(metadata["sha256"]).lower()
        ):
            raise FileServiceError(
                "Printer did not confirm the verified firmware candidate"
            )
        self._firmware_state_changed(
            status="ready", offset=metadata["size"], progress=100,
            staged_path="/usb/" + str(staged.get("path", "FWUPD.RME")).lstrip("/"),
        )

    def _clear_staged_firmware_state(self):
        """Forget only candidate/handoff state and stale firmware workflow."""
        with self._state_lock:
            if self._state["firmware"].get("status") in (
                "ready", "staged", "flash_queued", "flashing", "restarting"
            ):
                self._state["firmware"] = self._empty_state()["firmware"]
            workflow = self._state.get("workflow") or {}
            if workflow.get("workflow") == "firmware_update":
                self._state["workflow"] = None
                if (self._state.get("prompt") or {}).get("kind") == "firmware":
                    self._state["prompt"] = None

    def _apply_authoritative_firmware_locked(self, record):
        """Apply one current-firmware stage record while holding state lock."""
        candidate = bool(int(record.get("candidate", 0)))
        armed = bool(int(record.get("armed", 0)))
        printer_state = str(record.get("state", "idle")).lower()
        if not candidate:
            recovery_required = bool(
                self._state["firmware"].get("recovery_required")
            )
            if not recovery_required:
                self._state["firmware"] = self._empty_state()["firmware"]
                workflow = self._state.get("workflow") or {}
                if workflow.get("workflow") == "firmware_update":
                    self._state["workflow"] = None
                    if (self._state.get("prompt") or {}).get("kind") == "firmware":
                        self._state["prompt"] = None
            return False

        size = max(0, int(record.get("size", self._state["firmware"].get("size", 0))))
        path = str(record.get("path", "FWUPD.RME"))
        status = "restarting" if armed or printer_state == "restarting" else "ready"
        self._state["firmware"].update(
            status=status,
            size=size,
            offset=size,
            progress=100,
            sha256=record.get("sha256", self._state["firmware"].get("sha256")),
            error=None,
            staged_path="/usb/" + path.lstrip("/"),
            reconnect_expected=(status == "restarting"),
            candidate=True,
            armed=armed,
            printer_stage_state=printer_state,
        )
        if status == "restarting":
            self._firmware_handoff_started_at = time.monotonic()
        return True

    def _reconcile_firmware_stage(self):
        """Refresh stage truth, using the authoritative current protocol."""
        # SESSION can arrive before deferred FILE service initialization. The
        # storage initializer performs this reconciliation again once ready.
        if self._file_service is None:
            return False
        status = self._file_service.firmware_status()
        with self._state_lock:
            staged = self._apply_authoritative_firmware_locked(status)
        self._persist_and_publish()
        return staged

    def _unstage_firmware(self):
        """Delete the protected candidate and clear its durable UI state."""
        self._require_print_idle("Firmware actions")
        self._require_storage()
        with self._firmware_action_lock:
            if (self._uploader and self._uploader.busy) or (
                self._firmware_file_thread and self._firmware_file_thread.is_alive()
            ):
                raise UploadError("Cannot unstage firmware during a transfer")
            self._file_service.unstage_firmware()
            self._clear_staged_firmware_state()
            self._persist_and_publish()
            self._defer(self._refresh_storage_after_change, "/")

    def _firmware_file_progress(self, offset, size):
        self._firmware_state_changed(
            status="uploading", offset=offset,
            progress=round(offset * 100.0 / max(1, size), 2),
        )

    def _firmware_state_changed(self, **changes):
        status = changes.get("status")
        with self._state_lock:
            self._state["firmware"].update(changes)
            recovery_required = bool(
                self._state["firmware"].get("recovery_required")
            )
            flash_after_stage = bool(
                self._state["firmware"].get("flash_after_stage")
            )
            if status == "error":
                self._state["firmware"]["flash_after_stage"] = False
                error_to_clear = self._state["firmware"].get("error")
            else:
                error_to_clear = None
        now = time.monotonic()
        if status != "uploading" or now - self._last_fw_publish >= 0.25:
            self._last_fw_publish = now
            self._persist_and_publish()
        if status == "ready" and flash_after_stage:
            self._defer(self._flash_after_verified_stage)
        elif status == "ready" or (status == "error" and not recovery_required):
            self._defer(self._restore_session_after_upload)
        if error_to_clear and not recovery_required:
            timer = threading.Timer(
                30, self._clear_firmware_error, args=(error_to_clear,)
            )
            timer.daemon = True
            timer.start()

    def _clear_firmware_error(self, expected_error):
        """Dismiss only the unchanged terminal error from the latest transfer."""
        with self._state_lock:
            firmware = self._state["firmware"]
            if firmware.get("status") != "error" or firmware.get("error") != expected_error:
                return
            firmware.update(status="idle", error=None, progress=0, offset=0)
        self._persist_and_publish()

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
            staged = self._state["firmware"].get("status") == "ready"
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
        self._require_print_idle("Firmware flashing")
        with self._state_lock:
            if self._state["firmware"].get("status") not in ("ready", "staged"):
                raise UploadError("Upload and verify firmware on the printer before flashing")
        self._file_service.mutate("FLASH", "FWUPD.RME")
        with self._state_lock:
            self._state["firmware"].update(
                status="flash_queued", error=None, flash_after_stage=False,
                reconnect_expected=False,
            )
            self._firmware_handoff_started_at = time.monotonic()
        self._persist_and_publish()

    def _delete_firmware(self, filename):
        """Remove one explicitly selected BBF from the plugin's Pi storage."""
        self._require_print_idle("Firmware actions")
        if (self._uploader and self._uploader.busy) or (
            self._firmware_file_thread and self._firmware_file_thread.is_alive()
        ):
            raise UploadError("Cannot delete firmware during a transfer")
        path = self._firmware_path(filename)
        if not os.path.isfile(path):
            flask.abort(404)
        os.unlink(path)
        self._logger.info("Deleted staged Pi firmware file %s", filename)

    # -- State publication --------------------------------------------------

    def _persistent_snapshot(self):
        """Return refresh/restart-critical state, excluding ephemeral connection data."""
        with self._state_lock:
            snapshot = copy.deepcopy(
                {key: self._state[key] for key in (
                    "machine", "toolmap", "spoolmanager",
                    "loaded_filaments", "active_tool", "internal_spools", "stats",
                )}
            )
            firmware = copy.deepcopy(self._state["firmware"])
            if (
                firmware.get("status") != "ready"
                and not firmware.get("recovery_required")
            ):
                firmware = self._empty_state()["firmware"]
            if not firmware.get("recovery_required"):
                firmware["error"] = None
            firmware["flash_after_stage"] = False
            snapshot["firmware"] = firmware
            return snapshot

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

    def _schedule_publish(self, delay=0.075):
        """Coalesce serial bursts into one persisted WebSocket snapshot.

        A stats response or five-tool filament report arrives as several lines.
        Publishing each line used to create one thread, JSON copy, state write,
        and browser redraw per record. A short timer keeps the receive hook
        non-blocking while publishing the complete burst as one snapshot.
        """
        with self._publish_timer_lock:
            if self._publish_timer is not None:
                return

            def publish_once():
                with self._publish_timer_lock:
                    self._publish_timer = None
                if self._stop.is_set():
                    return
                try:
                    self._persist_and_publish()
                except Exception:
                    self._logger.exception("Deferred RME state publication failed")

            self._publish_timer = threading.Timer(delay, publish_once)
            self._publish_timer.daemon = True
            self._publish_timer.start()

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
    "octoprint.filemanager.extension_tree": __plugin_implementation__.file_extension_hook,
    "octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook,
    "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    "octoprint.printer.sdcardupload": __plugin_implementation__.sd_card_upload_hook,
}
