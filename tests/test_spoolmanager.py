import unittest

from octoprint_rme_compatibility.spoolmanager import (
    SpoolManagerBridge,
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


if __name__ == "__main__":
    unittest.main()
