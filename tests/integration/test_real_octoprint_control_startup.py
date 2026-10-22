"""Run separately in the real OctoPrint environment, without serial hardware."""
import logging
import tempfile
import types
import unittest
from pathlib import Path
from octoprint.util.comm import PrintingGcodeFileInformation
from octoprint.filemanager.storage import LocalFileStorage
from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


class RealControlStartup(unittest.TestCase):
    def test_real_connector_start_token_changes_for_repeated_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'part.gcode'
            path.write_text('G28\nT1\n')
            selected = PrintingGcodeFileInformation(str(path))
            comm = types.SimpleNamespace(_currentFile=selected)
            plugin = RmeCompatibilityPlugin()
            self.assertIsNone(plugin._print_start_token(comm))
            selected.start()
            first = plugin._print_start_token(comm)
            self.assertIsNotNone(first)
            self.assertEqual(first, plugin._print_start_token(comm))
            selected.close()
            selected.start()
            self.assertNotEqual(first, plugin._print_start_token(comm))
            selected.close()

    def test_real_selected_file_overrides_stale_ui_control_and_print_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalFileStorage(directory)
            control = Path(storage.path_on_disk('control.gcode'))
            real = Path(storage.path_on_disk('real.gcode'))
            control.write_text('@pause\n; skip_validation\n; skip-rme-toolmapping\n')
            real.write_text('G28\nT1\nG1 X100 E1\n')
            plugin = RmeCompatibilityPlugin()
            plugin._logger = logging.getLogger(__name__)
            plugin._file_manager = types.SimpleNamespace(
                path_in_storage=lambda origin, path: storage.path_in_storage(path),
                path_on_disk=lambda origin, path: storage.path_on_disk(path))
            for selected, stale, executable in ((control, real, False), (real, control, True)):
                with self.subTest(selected=selected.name):
                    comm = types.SimpleNamespace(_currentFile=PrintingGcodeFileInformation(str(selected)))
                    plugin._printer = types.SimpleNamespace(get_current_data=lambda: {
                        'job': {'file': {'origin': 'local', 'path': stale.name}}})
                    plugin._preflight_file_info = plugin._startup_file_info(comm)
                    self.assertEqual(selected.name, plugin._preflight_file_info['path'])
                    self.assertEqual(executable, plugin._selected_job_has_executable_gcode())


if __name__ == '__main__':
    unittest.main()
