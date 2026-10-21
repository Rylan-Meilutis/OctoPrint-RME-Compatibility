import logging
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility import baud_policy
from octoprint_rme_compatibility.baud_policy import install


class Settings:
    def __init__(self, modern=False):
        self.values = {"auto_highest_baud": True, "baud_auto_migrated": False}
        self.global_values = {
            ("serial", "baudrate"): 115200,
            ("printerConnection", "preferred", "connector"): "serial" if modern else None,
            ("printerConnection", "preferred", "parameters"): {"port": "/dev/ttyACM0", "baudrate": 115200},
        }
        self.save = Mock()

    def get_boolean(self, path):
        return self.values.get(path[0], False)

    def set_boolean(self, path, value):
        self.values[path[0]] = value

    def global_get(self, path):
        return self.global_values.get(tuple(path))

    def global_set(self, path, value):
        self.global_values[tuple(path)] = value


class BaudPolicyTests(unittest.TestCase):
    def module(self, name):
        module = types.ModuleType(name)
        module.STANDARD_BAUDRATES = [115200, 250000, 230400, 9600]
        module.blocked = []
        module.additional = []
        module.preferred = 115200
        module.settings = lambda: types.SimpleNamespace(get=lambda path: module.blocked if path[-1] in ("blocklistedBaudrates", "blacklistedBaudrates") else None)
        # OctoPrint's candidate utility mutates its argument, adds configured
        # rates, removes blocked rates, then promotes the saved preferred rate.
        exec('''def baudrateList(candidates=None):
    candidates = STANDARD_BAUDRATES[:] if candidates is None else candidates
    for rate in sorted(additional, reverse=True):
        candidates.insert(0, rate)
    for rate in blocked:
        if rate in candidates:
            candidates.remove(rate)
    if preferred in candidates:
        candidates.remove(preferred)
        candidates.insert(0, preferred)
    return candidates
''', module.__dict__)
        return module

    def test_auto_and_dropdown_fastest_first_on_both_host_layouts(self):
        for name in ("octoprint.util.comm", "octoprint.plugins.serial_connector.serial_comm"):
            with self.subTest(name=name):
                module = self.module(name)
                alias = types.ModuleType("octoprint.plugins.serial_connector.connector")
                alias.baudrateList = module.baudrateList
                with patch.dict(sys.modules, {name: module, alias.__name__: alias}):
                    with patch("octoprint_rme_compatibility.baud_policy.importlib.import_module",
                               side_effect=lambda target: sys.modules[target] if target == name else (_ for _ in ()).throw(ImportError())):
                        self.assertTrue(install(lambda: True))
                        self.assertEqual([1000000, 250000, 230400, 115200, 9600], module.baudrateList())
                        self.assertEqual(module.baudrateList(), alias.baudrateList())
                        # Multi-port AUTO uses a restricted candidate seed.
                        seed = [115200, 250000]
                        self.assertEqual([1000000, 250000, 230400, 115200], module.baudrateList(seed))
                        self.assertEqual([115200, 250000], seed)

    def test_blocklist_dedup_user_rates_disable_and_reload(self):
        module = self.module("octoprint.util.comm")
        module.blocked = [1000000, 250000]
        module.additional = [2000000, 230400, 1000000, 1000000]
        with patch.dict(sys.modules, {module.__name__: module}):
            with patch("octoprint_rme_compatibility.baud_policy.importlib.import_module", return_value=module):
                original = module.baudrateList
                install(lambda: True)
                wrapper = module.baudrateList
                self.assertEqual([2000000, 230400, 115200, 9600], wrapper())
                # Failed handshakes can consume each next lower candidate.
                attempts = []
                for rate in wrapper():
                    attempts.append(rate)
                    if rate == 115200:
                        break
                self.assertEqual([2000000, 230400, 115200], attempts)
                install(lambda: False)
                self.assertIs(wrapper, module.baudrateList)
                self.assertEqual(115200, wrapper()[0])
                self.assertEqual(original(), wrapper())

    def test_legacy_host_without_exported_baud_constant(self):
        module = self.module("octoprint.util.comm")
        exec('''def baudrateList(candidates=None):
    return [115200, 250000, 230400, 57600, 38400, 19200, 9600] if candidates is None else candidates
''', module.__dict__)
        del module.STANDARD_BAUDRATES
        with patch.dict(sys.modules, {module.__name__: module}):
            with patch("octoprint_rme_compatibility.baud_policy.importlib.import_module", return_value=module):
                install(lambda: True)
                self.assertEqual(1000000, module.baudrateList()[0])
                self.assertIn(115200, module.baudrateList())

    def test_policy_failure_leaves_host_settings_api_usable(self):
        module = self.module("octoprint.util.comm")
        original = module.baudrateList
        with patch.dict(sys.modules, {module.__name__: module}):
            with patch("octoprint_rme_compatibility.baud_policy.importlib.import_module", return_value=module):
                install(lambda: True)
                with patch.object(baud_policy, "ordered_candidates", side_effect=RuntimeError("injected adapter failure")) as policy:
                    self.assertEqual(original(), module.baudrateList())
                    self.assertEqual(original(), module.baudrateList())
                    policy.assert_called_once()

    def plugin(self, modern=False):
        plugin = RmeCompatibilityPlugin()
        plugin._settings = Settings(modern)
        plugin._logger = logging.getLogger(__name__)
        plugin._printer = types.SimpleNamespace(
            connect=Mock(), disconnect=Mock(),
            get_current_connection=lambda: ("Operational", "/dev/ttyACM0", 115200, {}))
        return plugin

    def test_migrate_only_next_connection_once_on_both_hosts(self):
        for modern in (False, True):
            plugin = self.plugin(modern)
            plugin._configure_auto_baud()
            path = (("printerConnection", "preferred", "parameters") if modern else ("serial", "baudrate"))
            saved = plugin._settings.global_values[path]
            self.assertEqual(0, saved["baudrate"] if modern else saved)
            if modern:
                self.assertEqual("/dev/ttyACM0", saved["port"])
            plugin._printer.disconnect.assert_not_called()
            plugin._printer.connect.assert_not_called()
            # User explicitly saves a fixed speed later: do not override it.
            plugin._settings.global_values[path] = {"baudrate": 230400} if modern else 230400
            plugin._configure_auto_baud()
            self.assertEqual({"baudrate": 230400} if modern else 230400, plugin._settings.global_values[path])
            plugin._settings.save.assert_called_once()

    def test_initialize_prepares_known_rme_before_startup_autoconnect(self):
        for modern in (False, True):
            plugin = self.plugin(modern)
            plugin._settings.values["baud_auto_migrated"] = True
            with patch.object(baud_policy, "install", return_value=True) as installer:
                plugin.initialize()
                installer.assert_called_once()
                self.assertTrue(installer.call_args.args[0]())
                path = (("printerConnection", "preferred", "parameters") if modern else ("serial", "baudrate"))
                preferred = plugin._settings.global_values[path]
                self.assertEqual(0, preferred["baudrate"] if modern else preferred)
                # The later startup callback does not rewrite an operator's
                # manual choice or touch the connection which already started.
                plugin._settings.global_values[path] = {"baudrate": 230400} if modern else 230400
                plugin.on_startup("localhost", 5000)
                self.assertEqual({"baudrate": 230400} if modern else 230400,
                                 plugin._settings.global_values[path])
            plugin._printer.disconnect.assert_not_called()
            plugin._printer.connect.assert_not_called()

    def test_startup_does_not_override_unknown_opted_out_or_network_printer(self):
        for known, enabled, connector in ((False, True, None), (True, False, None),
                                           (True, True, "network")):
            plugin = self.plugin()
            plugin._settings.values.update(baud_auto_migrated=known, auto_highest_baud=enabled)
            plugin._settings.global_values[("printerConnection", "preferred", "connector")] = connector
            with patch.object(baud_policy, "install", return_value=True):
                plugin.initialize()
            self.assertEqual(115200, plugin._settings.global_values[("serial", "baudrate")])
            plugin._settings.save.assert_not_called()

    def test_opt_out_and_nonserial_are_untouched(self):
        plugin = self.plugin()
        plugin._settings.values["auto_highest_baud"] = False
        plugin._configure_auto_baud()
        plugin._settings.save.assert_not_called()
        plugin = self.plugin(True)
        plugin._settings.global_values[("printerConnection", "preferred", "connector")] = "network"
        plugin._configure_auto_baud()
        plugin._settings.save.assert_not_called()

    def test_update_reconnect_uses_auto_but_preserves_explicit_fixed_rate(self):
        for modern in (False, True):
            plugin = self.plugin(modern)
            plugin._configure_auto_baud()
            plugin._arm_firmware_reconnect()
            self.assertEqual({"baudrate": 0}, plugin._firmware_reconnect_options)
            if modern:
                plugin._settings.global_values[("printerConnection", "preferred", "parameters")]["baudrate"] = 115200
            else:
                plugin._settings.global_values[("serial", "baudrate")] = 115200
            plugin._arm_firmware_reconnect()
            self.assertEqual({"baudrate": 115200}, plugin._firmware_reconnect_options)
