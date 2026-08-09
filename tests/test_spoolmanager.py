import unittest
import threading

from octoprint_rme_compatibility.spoolmanager import (
    InternalSpoolBridge,
    SpoolManagerBridge,
    SpoolmanBridge,
    normalize_color,
    spool_alias,
)


class _Model(object):
    """Minimal stand-in for the mutable SpoolManager database model."""

    def __init__(self, database_id, name="Galaxy Blue", remaining=750):
        self.databaseId = database_id
        self.displayName = name
        self.vendor = "Example"
        self.material = "PLA"
        self.colorName = "Blue"
        self.color = "#193A8A"
        self.temperature = 220
        self.bedTemperature = 60
        self.remainingWeight = remaining
        self.isActive = True
        self.isTemplate = False


class _Database(object):
    def __init__(self, models):
        self.models = models

    def loadAllSpoolsByQuery(self, query):
        return self.models

    def loadSpool(self, database_id):
        return next((model for model in self.models if model.databaseId == database_id), None)


class _Settings(object):
    def __init__(self, database_ids):
        self.database_ids = database_ids

    def get(self, path):
        return self.database_ids


class _Implementation(object):
    def __init__(self, models):
        self._databaseManager = _Database(models)
        self._settings = _Settings([models[0].databaseId])
        self.selection = [models[0]]

    def loadSelectedSpools(self):
        return self.selection

    def _selectSpool(self, tool, database_id):
        self.selection[tool] = next(model for model in self._databaseManager.models
                                    if model.databaseId == database_id)
        return self.selection[tool]


class _PluginInfo(object):
    enabled = True

    def __init__(self, implementation):
        self.implementation = implementation


class _PluginManager(object):
    def __init__(self, implementation):
        self.plugins = {"SpoolManager": _PluginInfo(implementation)}


class SpoolManagerTests(unittest.TestCase):
    def test_normalizes_colors_and_builds_firmware_safe_aliases(self):
        self.assertEqual(normalize_color("#abc"), "#aabbcc")
        self.assertEqual(normalize_color("invalid"), "#808080")
        self.assertEqual(spool_alias("PETG-CF", 42), "PET-016")
        self.assertLessEqual(len(spool_alias("Very Long Material", 99999)), 7)

    def test_bridge_filters_empty_spools_and_selects_by_tool(self):
        models = [_Model(1), _Model(2, remaining=0)]
        implementation = _Implementation(models)
        bridge = SpoolManagerBridge(_PluginManager(implementation))
        self.assertEqual([record["database_id"] for record in bridge.inventory()], [1])
        self.assertEqual(bridge.selected()[0]["color"], "#193a8a")
        self.assertEqual(bridge.select(0, 1)["display_name"], "Galaxy Blue")

    def test_spoolman_bridge_normalizes_inventory_and_selected_tools(self):
        raw = [{
            "id": 7,
            "remaining_weight": 640,
            "archived": False,
            "filament": {
                "id": 3, "name": "Galaxy Blue", "material": "PETG",
                "color_hex": "193A8A", "settings_extruder_temp": 245,
                "settings_bed_temp": 85, "vendor": {"name": "Example"},
            },
        }]

        class Settings(object):
            def get(self, path):
                return {"0": {"spoolId": "7"}}

        class Connector(object):
            def handleGetSpoolsAvailable(self):
                return {"data": {"spools": raw}}

        implementation = type("SpoolmanImplementation", (), {
            "_settings": Settings(),
            "getSpoolmanConnector": lambda self: Connector(),
        })()
        manager = type("Manager", (), {
            "plugins": {"Spoolman": _PluginInfo(implementation)},
        })()
        bridge = SpoolmanBridge(manager)

        self.assertEqual("PETG", bridge.inventory()[0]["material"])
        self.assertEqual("#193a8a", bridge.selected()[0]["color"])

    def test_internal_provider_persists_inventory_and_selection(self):
        state = {"internal_spools": {"next_id": 1, "inventory": [], "selected": {}}}
        bridge = InternalSpoolBridge(state, threading.RLock())
        created = bridge.create({
            "display_name": "Fallback PLA", "vendor": "", "material": "PLA",
            "color_name": "Orange", "color": "#ff8000", "total_weight": 1000,
            "nozzle_temperature": 215, "bed_temperature": 60,
        })
        bridge.select(2, created["database_id"])

        self.assertEqual("Fallback PLA", bridge.inventory()[0]["display_name"])
        self.assertEqual("#ff8000", bridge.selected()[2]["color"])


if __name__ == "__main__":
    unittest.main()
