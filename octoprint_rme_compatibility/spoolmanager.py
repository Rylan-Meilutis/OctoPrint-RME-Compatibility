"""Compatibility adapter for OctoPrint-SpoolManager.

SpoolManager currently exposes selection events and Python implementation
methods, but no registered plugin helpers. This adapter keeps all use of that
optional API isolated and feature-detected so the RME plugin still works when
SpoolManager is absent or changes.
"""

import re


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

    def inventory(self):
        """Return active, non-template spools that have filament remaining."""
        implementation = self._implementation()
        manager = getattr(implementation, "_databaseManager", None)
        if manager is None or not hasattr(manager, "loadAllSpoolsByQuery"):
            raise SpoolManagerUnavailable("Installed SpoolManager has no compatible inventory API")
        models = manager.loadAllSpoolsByQuery(None)
        records = [self._record(model) for model in models]
        return [record for record in records if (
            not record["is_template"]
            and record["is_active"]
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
