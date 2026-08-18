"""Inventory-provider adapters for SpoolManager, Spoolman, and local storage.

SpoolManager currently exposes selection events and Python implementation
methods, but no registered plugin helpers. This adapter keeps all use of that
optional API isolated and feature-detected so the RME plugin still works when
SpoolManager is absent or changes.
"""

import re

import requests


class SpoolManagerUnavailable(RuntimeError):
    pass


def normalize_color(value, default="#808080"):
    """Normalize SpoolManager's optional CSS color to firmware ``#rrggbb``."""
    if value is None:
        return default
    text = str(value).strip()
    if re.match(r"^#[0-9a-fA-F]{6}$", text):
        return text.lower()
    if re.match(r"^#[0-9a-fA-F]{3}$", text):
        return ("#" + "".join(character * 2 for character in text[1:])).lower()
    return default


def spool_alias(material, database_id, used=None):
    """Create a unique, firmware-safe seven-character user-preset name."""
    prefix = "".join(character for character in str(material or "SP").upper() if character.isalnum())[:3]
    prefix = prefix or "SP"
    suffix = _base36(int(database_id))[-3:].rjust(3, "0")
    candidate = (prefix + "-" + suffix)[:7]
    if used is not None and candidate in used:
        candidate = ("S" + _base36(int(database_id)).rjust(6, "0"))[-7:]
    return candidate


def _base36(value):
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if value == 0:
        return "0"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result


class SpoolManagerBridge(object):
    """Small, feature-detected facade around SpoolManager's Python API.

    SpoolManager publishes events but currently registers no public OctoPrint
    helpers.  Keeping private method access in this class gives the rest of the
    plugin a stable contract and makes an upstream API migration localized.
    """
    PLUGIN_KEYS = ("SpoolManager", "spoolmanager")

    def __init__(self, plugin_manager, logger=None):
        self.plugin_manager = plugin_manager
        self.logger = logger

    def available(self):
        return self._implementation(required=False) is not None

    def inventory(self, include_unavailable=False):
        """Return concrete spools, optionally including empty/inactive ones.

        Firmware publication only needs usable spools, but an operator mapping
        page must be able to display every real SpoolManager spool. Templates
        are definitions rather than physical spools and are never selectable.
        """
        implementation = self._implementation()
        manager = getattr(implementation, "_databaseManager", None)
        if manager is None or not hasattr(manager, "loadAllSpoolsByQuery"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no compatible inventory API")
        models = manager.loadAllSpoolsByQuery(None)
        records = [self._record(model) for model in models]
        concrete = [record for record in records if not record["is_template"]]
        if include_unavailable:
            return concrete
        return [record for record in concrete if (
            record["is_active"]
            and (record["remaining_weight"] is None or record["remaining_weight"] > 0)
        )]

    def selected(self):
        """Return the tool-indexed selection without emitting read-side events.

        SpoolManager's public-ish ``loadSelectedSpools`` fires ``spool_selected``
        for every item it reads. A polling integration must instead resolve its
        stored database IDs directly or it would feed its own event handler.
        """
        implementation = self._implementation()
        settings = getattr(implementation, "_settings", None)
        manager = getattr(implementation, "_databaseManager", None)
        if settings is None or manager is None or not hasattr(manager, "loadSpool"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no event-safe selection API")
        database_ids = settings.get(["selectedSpoolsDatabaseIds"]) or []
        result = []
        for database_id in database_ids:
            model = None if database_id is None else manager.loadSpool(int(database_id))
            result.append(None if model is None else self._record(model))
        return result

    def select(self, tool, database_id):
        """Select a spool through SpoolManager so its normal events still fire."""
        implementation = self._implementation()
        if not hasattr(implementation, "_selectSpool"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no compatible selection API")
        model = implementation._selectSpool(int(tool), int(database_id))
        return None if model is None else self._record(model)

    def deselect(self, tool):
        implementation = self._implementation()
        if not hasattr(implementation, "_selectSpool"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no compatible selection API")
        implementation._selectSpool(int(tool), -1)

    def create(self, values):
        """Create a concrete (non-template) spool from the persistent RME form."""
        implementation = self._implementation()
        manager = getattr(implementation, "_databaseManager", None)
        if manager is None or not hasattr(manager, "saveSpool"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no compatible create API")
        try:
            from octoprint_SpoolManager.models.SpoolModel import SpoolModel
        except ImportError as exc:
            raise SpoolManagerUnavailable("Could not import SpoolManager's spool model") from exc

        model = SpoolModel()
        model.isActive = True
        model.isTemplate = False
        model.displayName = values["display_name"]
        model.vendor = values.get("vendor") or ""
        model.material = values["material"]
        model.colorName = values.get("color_name") or values["color"]
        model.color = normalize_color(values["color"])
        model.temperature = int(values["nozzle_temperature"])
        model.bedTemperature = int(values["bed_temperature"])
        model.totalWeight = float(values["total_weight"])
        model.usedWeight = 0.0
        model.diameter = float(values.get("diameter", 1.75))
        model.density = float(values.get("density", 1.24))
        database_id = manager.saveSpool(model)
        if database_id is None:
            raise RuntimeError("SpoolManager did not create the spool")
        model = manager.loadSpool(database_id)
        if hasattr(implementation, "_sendPayload2EventBus"):
            implementation._sendPayload2EventBus(
                "spool_added",
                {
                    "databaseId": model.databaseId,
                    "spoolName": model.displayName,
                    "material": model.material,
                    "colorName": model.colorName,
                    "remainingWeight": model.remainingWeight,
                },
            )
        return self._record(model)

    def get(self, database_id):
        implementation = self._implementation()
        manager = getattr(implementation, "_databaseManager", None)
        if manager is None or not hasattr(manager, "loadSpool"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no compatible spool lookup API")
        model = manager.loadSpool(int(database_id))
        return None if model is None else self._record(model)

    def _implementation(self, required=True):
        """Resolve the optional plugin without making it a hard dependency."""
        plugins = getattr(self.plugin_manager, "plugins", {})
        for key in self.PLUGIN_KEYS:
            plugin = plugins.get(key)
            if plugin is not None and getattr(plugin, "enabled", False):
                implementation = getattr(plugin, "implementation", None)
                if implementation is not None:
                    return implementation
        if required:
            raise SpoolManagerUnavailable("SpoolManager is not installed and enabled")
        return None

    @staticmethod
    def _record(model):
        """Convert mutable SpoolManager ORM models into JSON-safe dictionaries."""
        remaining = getattr(model, "remainingWeight", None)
        if remaining is None:
            total = getattr(model, "totalWeight", None)
            used = getattr(model, "usedWeight", None)
            if total is not None:
                remaining = float(total) - float(used or 0)
        return {
            "database_id": int(model.databaseId),
            "display_name": str(getattr(model, "displayName", None) or "Spool %s" % model.databaseId),
            "vendor": str(getattr(model, "vendor", None) or ""),
            "material": str(getattr(model, "material", None) or "PLA"),
            "color_name": str(getattr(model, "colorName", None) or ""),
            "color": normalize_color(getattr(model, "color", None)),
            "nozzle_temperature": int(getattr(model, "temperature", None) or 215),
            "bed_temperature": int(getattr(model, "bedTemperature", None) or 60),
            "remaining_weight": None if remaining is None else float(remaining),
            "is_active": getattr(model, "isActive", None) is not False,
            "is_template": getattr(model, "isTemplate", None) is True,
        }


class SpoolmanBridge(object):
    """Feature-detected adapter for mdziekon's OctoPrint-Spoolman plugin.

    Its connector already owns the configured URL, TLS policy, and optional API
    key. Reusing that connector prevents the RME plugin from storing duplicate
    credentials while still speaking Spoolman's documented REST API.
    """
    PLUGIN_KEYS = ("Spoolman", "spoolman")

    def __init__(self, plugin_manager, logger=None):
        self.plugin_manager = plugin_manager
        self.logger = logger

    def available(self):
        return self._implementation(required=False) is not None

    def inventory(self, include_unavailable=False):
        connector = self._connector()
        if include_unavailable:
            try:
                raw = self._request(connector, "get", "/spool", None)
                if isinstance(raw, dict):
                    raw = raw.get("spools", raw.get("data", []))
                records = [self._record(item) for item in (raw or [])]
                return records
            except (
                AttributeError,
                requests.RequestException,
                SpoolManagerUnavailable,
                ValueError,
            ) as exc:
                if self.logger:
                    self.logger.warning(
                        "Spoolman full inventory unavailable; using available spools: %s",
                        exc,
                    )
        result = connector.handleGetSpoolsAvailable()
        if result.get("error"):
            raise SpoolManagerUnavailable("Spoolman inventory request failed: %s" % result["error"])
        records = [self._record(item) for item in result.get("data", {}).get("spools", [])]
        return [item for item in records if item["is_active"] and (
            item["remaining_weight"] is None or item["remaining_weight"] > 0
        )]

    def selected(self):
        implementation = self._implementation()
        selected = implementation._settings.get(["selectedSpoolIds"]) or {}
        inventory = {
            item["database_id"]: item
            for item in self.inventory(include_unavailable=True)
        }
        indices = [int(key) for key in selected.keys()] if selected else []
        result = [None] * (max(indices) + 1 if indices else 0)
        for key, value in selected.items():
            spool_id = (value or {}).get("spoolId")
            if spool_id is not None:
                result[int(key)] = inventory.get(int(spool_id))
        return result

    def select(self, tool, database_id):
        record = self.get(database_id)
        if record is None:
            raise SpoolManagerUnavailable("Spoolman spool %s does not exist" % database_id)
        self._set_selection(tool, str(database_id))
        return record

    def deselect(self, tool):
        self._set_selection(tool, None)

    def create(self, values):
        """Create or reuse matching filament metadata, then create its spool."""
        connector = self._connector()
        inventory = self.inventory()
        wanted_color = normalize_color(values.get("color")).lstrip("#")
        filament = next((
            item["_filament"] for item in inventory
            if item["material"].lower() == str(values["material"]).lower()
            and item["color"].lstrip("#").lower() == wanted_color.lower()
            and item["display_name"].split(" · #", 1)[0].lower()
            == str(values["display_name"]).lower()
        ), None)
        if filament is None:
            filament_payload = {
                "name": str(values["display_name"]),
                "material": str(values["material"]),
                "density": float(values.get("density", 1.24)),
                "diameter": float(values.get("diameter", 1.75)),
                "weight": float(values["total_weight"]),
                "settings_extruder_temp": int(values.get("nozzle_temperature", 215)),
                "settings_bed_temp": int(values.get("bed_temperature", 60)),
                "color_hex": wanted_color,
            }
            filament = self._request(connector, "post", "/filament", filament_payload)
        spool = self._request(connector, "post", "/spool", {
            "filament_id": int(filament["id"]),
            "initial_weight": float(values["total_weight"]),
            "remaining_weight": float(values["total_weight"]),
        })
        return self._record(spool)

    def get(self, database_id):
        return next((item for item in self.inventory(include_unavailable=True)
                     if item["database_id"] == int(database_id)), None)

    def _set_selection(self, tool, spool_id):
        implementation = self._implementation()
        selected = implementation._settings.get(["selectedSpoolIds"]) or {}
        selected[str(int(tool))] = {"spoolId": spool_id}
        implementation._settings.set(["selectedSpoolIds"], selected)
        implementation._settings.save()
        try:
            from octoprint.events import Events
            event = getattr(Events, "PLUGIN_SPOOLMAN_SPOOL_SELECTED")
            implementation.triggerPluginEvent(event, {
                "toolIdx": int(tool), "spoolId": spool_id,
            })
        except (AttributeError, ImportError):
            # Selection is already durable; the next periodic sync updates UIs
            # on older plugin versions that do not register the custom event.
            pass

    def _connector(self):
        implementation = self._implementation()
        if not hasattr(implementation, "getSpoolmanConnector"):
            raise SpoolManagerUnavailable("Installed Spoolman plugin has no compatible connector API")
        return implementation.getSpoolmanConnector()

    def _implementation(self, required=True):
        plugins = getattr(self.plugin_manager, "plugins", {})
        for key in self.PLUGIN_KEYS:
            plugin = plugins.get(key)
            if plugin is not None and getattr(plugin, "enabled", False):
                implementation = getattr(plugin, "implementation", None)
                if implementation is not None:
                    return implementation
        if required:
            raise SpoolManagerUnavailable("Spoolman is not installed and enabled")
        return None

    @staticmethod
    def _request(connector, method, endpoint, payload):
        url = connector._createSpoolmanEndpointUrl(endpoint)
        request_options = {
            "headers": connector._buildRequestHeaders(),
            "verify": connector.verifyConfig,
            "timeout": (3.05, 30),
        }
        if payload is not None:
            request_options["json"] = payload
        response = getattr(requests, method)(url, **request_options)
        if response.status_code < 200 or response.status_code >= 300:
            raise SpoolManagerUnavailable(
                "Spoolman %s failed with HTTP %d" % (endpoint, response.status_code)
            )
        return response.json()

    @staticmethod
    def _record(spool):
        filament = spool.get("filament") or {}
        vendor = filament.get("vendor") or {}
        name = filament.get("name") or filament.get("material") or "Spool"
        color = normalize_color("#" + str(filament.get("color_hex") or "808080").lstrip("#"))
        return {
            "database_id": int(spool["id"]),
            "display_name": "%s · #%s" % (name, spool["id"]),
            "vendor": str(vendor.get("name") or ""),
            "material": str(filament.get("material") or "PLA"),
            "color_name": str(filament.get("name") or ""),
            "color": color,
            "nozzle_temperature": int(filament.get("settings_extruder_temp") or 215),
            "bed_temperature": int(filament.get("settings_bed_temp") or 60),
            "remaining_weight": spool.get("remaining_weight"),
            "is_active": not bool(spool.get("archived", False)),
            "is_template": False,
            "_filament": filament,
        }


class InternalSpoolBridge(object):
    """Persistent zero-dependency inventory used when no provider is installed."""

    def __init__(self, state, lock, logger=None):
        self.state = state
        self.lock = lock
        self.logger = logger

    def available(self):
        return True

    def inventory(self, include_unavailable=False):
        with self.lock:
            return [dict(item) for item in self.state["internal_spools"]["inventory"]]

    def selected(self):
        with self.lock:
            selected = dict(self.state["internal_spools"]["selected"])
            inventory = {
                item["database_id"]: dict(item)
                for item in self.state["internal_spools"]["inventory"]
            }
        indices = [int(key) for key in selected] if selected else []
        result = [None] * (max(indices) + 1 if indices else 0)
        for key, database_id in selected.items():
            result[int(key)] = inventory.get(int(database_id))
        return result

    def select(self, tool, database_id):
        record = self.get(database_id)
        if record is None:
            raise SpoolManagerUnavailable("Built-in spool %s does not exist" % database_id)
        with self.lock:
            self.state["internal_spools"]["selected"][str(int(tool))] = int(database_id)
        return record

    def deselect(self, tool):
        with self.lock:
            self.state["internal_spools"]["selected"].pop(str(int(tool)), None)

    def create(self, values):
        with self.lock:
            storage = self.state["internal_spools"]
            database_id = int(storage["next_id"])
            storage["next_id"] = database_id + 1
            record = {
                "database_id": database_id,
                "display_name": str(values["display_name"]),
                "vendor": str(values.get("vendor") or ""),
                "material": str(values["material"]),
                "color_name": str(values.get("color_name") or ""),
                "color": normalize_color(values.get("color")),
                "nozzle_temperature": int(values.get("nozzle_temperature", 215)),
                "bed_temperature": int(values.get("bed_temperature", 60)),
                "remaining_weight": float(values["total_weight"]),
                "is_active": True,
                "is_template": False,
            }
            storage["inventory"].append(record)
            return dict(record)

    def get(self, database_id):
        with self.lock:
            record = next((item for item in self.state["internal_spools"]["inventory"]
                           if item["database_id"] == int(database_id)), None)
            return None if record is None else dict(record)
