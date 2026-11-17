"""Actual 2.0 facade -> serial connector -> core, no USB hardware access."""
import logging
import tempfile
import types
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import octoprint.filemanager
from octoprint.printer import PrinterInterface
from core_compat import ConnectedSerialPrinter
from octoprint_rme_compatibility import host_compat
from octoprint_rme_compatibility.plugin import RmeCompatibilityPlugin


@unittest.skipUnless(ConnectedSerialPrinter is not None, "OctoPrint 2.0 connector API")
class RealOctoPrint200(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import octoprint.plugin
        import octoprint.settings
        from octoprint.settings import settings
        cls.settings_patch = patch.object(octoprint.settings, '_instance', None)
        cls.plugins_patch = patch.object(octoprint.plugin, '_instance', None)
        cls.settings_patch.start()
        cls.plugins_patch.start()
        cls.directory = tempfile.TemporaryDirectory()
        try:
            settings()
        except ValueError:
            settings(init=True, basedir=cls.directory.name)
        try:
            octoprint.plugin.plugin_manager()
        except ValueError:
            octoprint.plugin.plugin_manager(init=True, plugin_folders=[], plugin_bases=[], plugin_entry_points=[])

    @classmethod
    def tearDownClass(cls):
        cls.plugins_patch.stop()
        cls.settings_patch.stop()
        cls.directory.cleanup()

    def upload_plugin(self):
        printer, connector = self.facade()
        plugin = RmeCompatibilityPlugin()
        plugin._printer = printer
        plugin._logger = logging.getLogger(__name__)
        plugin._state.update(connected=True, supported=True)
        plugin._state['storage'].update(supported=True, caps={'write':1})
        plugin._file_service = types.SimpleNamespace(busy=False)
        plugin._print_job_active = lambda:False
        plugin._set_storage_status = Mock()
        plugin._storage_progress = Mock()
        plugin._defer = Mock()
        host_compat.install_printer_upload(printer, plugin._native_connector_upload)
        return plugin, printer, connector

    def test_native_2_storage_upload_uses_verified_writer_and_cleans_temp(self):
        from octoprint.filemanager.storage.printer import PrinterFileStorage
        from octoprint.filemanager.util import DiskFileWrapper
        plugin, printer, connector = self.upload_plugin()
        storage = PrinterFileStorage(connector)
        storage._get_printer_files = lambda:[]
        complete = threading.Event()
        events, sources = [], []
        def write(source, target, **kwargs):
            sources.append(source)
            self.assertTrue(Path(source).is_file())
            self.assertTrue(plugin._printer_transfer_active())
            self.assertEqual('part.gco', target)
            kwargs['progress'](4,4)
        plugin._write_file_with_manifest = write
        def callback(**kwargs):
            events.append(kwargs)
            if kwargs.get('done') or kwargs.get('failed'):complete.set()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'input.gco'
            path.write_text('G90\n')
            result = storage.add_file('part.gco', DiskFileWrapper(path.name,str(path)), progress_callback=callback)
            self.assertEqual('part.gco', result)
            self.assertTrue(complete.wait(2))
            self.assertEqual(1, len(sources))
            self.assertFalse(Path(sources[0]).exists(), 'core must delete its temporary upload source')
            self.assertEqual(1, sum(bool(e.get('done')) for e in events))
            self.assertFalse(any(e.get('failed') for e in events))
            self.assertFalse(plugin._native_upload_active)
            connector._comm.startFileTransfer.assert_not_called()

    def test_native_2_upload_failure_completes_once_and_releases_ownership(self):
        plugin, _, connector = self.upload_plugin()
        plugin._write_file_with_manifest = Mock(side_effect=RuntimeError('injected write failure'))
        complete = threading.Event()
        events = []
        def callback(**kwargs):
            events.append(kwargs)
            if kwargs.get('failed'):complete.set()
        connector.upload_printer_file('/tmp/not-opened-by-test','part.gco', progress_callback=callback)
        self.assertTrue(complete.wait(2))
        self.assertEqual([{'failed':True}], events)
        self.assertFalse(plugin._native_upload_active)
        connector._comm.startFileTransfer.assert_not_called()

    def test_native_2_upload_rejects_printing_and_competing_transfers(self):
        from octoprint.printer import PrinterFilesUnavailableError
        for printing in (True, False):
            plugin, _, connector = self.upload_plugin()
            plugin._print_job_active = lambda:printing
            plugin._native_upload_active = not printing
            callback = Mock()
            with self.assertRaises(PrinterFilesUnavailableError):
                connector.upload_printer_file('unused','part.gco', progress_callback=callback)
            callback.assert_called_once_with(failed=True)
            connector._comm.startFileTransfer.assert_not_called()

    def test_non_rme_native_upload_retains_original_core_route(self):
        plugin, _, connector = self.upload_plugin()
        plugin._state['supported'] = False
        connector._comm.isBusy.return_value = False
        connector._comm.isSdReady.return_value = True
        connector._comm.startFileTransfer.return_value = 'stock.gco'
        plugin._write_file_with_manifest = Mock()
        self.assertEqual('stock.gco', connector.upload_printer_file('input.gco','stock.gco'))
        connector._comm.startFileTransfer.assert_called_once()
        plugin._write_file_with_manifest.assert_not_called()

    def test_connection_changed_before_worker_starts_never_writes(self):
        plugin, printer, connector = self.upload_plugin()
        plugin._write_file_with_manifest = Mock()
        callback = Mock()
        with patch('octoprint_rme_compatibility.plugin.threading.Thread') as thread:
            connector.upload_printer_file('unused','part.gco',progress_callback=callback)
            self.assertTrue(plugin._printer_transfer_active())
            worker = thread.call_args.kwargs['target']
        printer._connection = None
        worker()
        plugin._write_file_with_manifest.assert_not_called()
        callback.assert_called_once_with(failed=True)
        self.assertFalse(plugin._native_upload_active)

    def test_worker_start_failure_notifies_once_and_releases_owner(self):
        from octoprint.printer import PrinterFilesUnavailableError
        plugin, _, connector = self.upload_plugin()
        callback = Mock()
        with patch('octoprint_rme_compatibility.plugin.threading.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('injected thread start failure')
            with self.assertRaises(PrinterFilesUnavailableError):
                connector.upload_printer_file('unused','part.gco',progress_callback=callback)
        callback.assert_called_once_with(failed=True)
        self.assertFalse(plugin._native_upload_active)

    def facade(self):
        from octoprint.printer.standard import Printer
        from octoprint.printer.connection import ConnectedPrinterState
        connector = ConnectedSerialPrinter.__new__(ConnectedSerialPrinter)
        connector._comm = Mock()
        connector._logger = logging.getLogger(__name__)
        connector._port = "/dev/ttyTEST"
        connector._baudrate = 1000000
        connector._profile = {"id":"_default"}
        connector._state = ConnectedPrinterState.OPERATIONAL
        connector._plugin_settings = types.SimpleNamespace(get_boolean=lambda p:True)
        printer = Printer.__new__(Printer)
        printer._connection = connector
        return printer, connector

    def test_priority_service_survives_actual_2_facade_and_connector(self):
        printer, connector = self.facade()
        plugin = RmeCompatibilityPlugin()
        plugin._printer = printer
        plugin._send_priority_services(["@RME DIALOG QUERY", "@RME SESSION KEEPALIVE"], "test")
        calls = connector._comm.sendCommand.call_args_list
        self.assertEqual(2, len(calls))
        for call in calls:
            self.assertTrue(call.kwargs["force"])
            self.assertIn("rme:priority_control", call.kwargs["tags"])
            self.assertIn("trigger:printer.commands", call.kwargs["tags"])

    def test_real_connector_reconnect_shape_and_file_destination(self):
        from octoprint.filemanager.destinations import FileDestinations
        printer, connector = self.facade()
        self.assertIs(connector._comm, host_compat.serial_comm(printer, object()))
        self.assertEqual({"connector":"serial", "parameters":{"baudrate":1000000}}, host_compat.reconnect_options(printer))
        self.assertEqual(FileDestinations.PRINTER, host_compat.printer_file_origin())
        self.assertEqual("printer", RmeCompatibilityPlugin._empty_state()["host"]["printer_file_origin"])

    def test_real_facade_after_switch_does_not_reuse_old_serial_core(self):
        printer, connector = self.facade()
        old = connector._comm
        printer._connection = types.SimpleNamespace(connector="network", _comm=old)
        self.assertIsNone(host_compat.serial_comm(printer, old))
        with self.assertRaises(RuntimeError):
            host_compat.reconnect_options(printer)
        printer._connection = None
        self.assertIsNone(host_compat.serial_comm(printer, old))

    def test_post_routes_remain_csrf_protected(self):
        plugin = RmeCompatibilityPlugin()
        plugin._identifier = "rme_compatibility"
        plugin._basefolder = str(Path(__file__).resolve().parents[2] / "octoprint_rme_compatibility")
        self.assertTrue(plugin.is_blueprint_csrf_protected())
        self.assertTrue(plugin.is_api_protected())
        self.assertTrue(plugin.is_template_autoescaped())
        # Exercise the real BlueprintPlugin route collector used by OctoPrint.
        blueprint = plugin.get_blueprint()
        self.assertIsNotNone(blueprint)

    def test_replaced_connector_storage_never_falls_back_to_old_serial(self):
        from octoprint.printer import PrinterFilesUnavailableError
        plugin, printer, connector = self.upload_plugin()
        plugin._state.update(connected=False, supported=False)
        printer._connection = types.SimpleNamespace(connector="network")
        callback = Mock()
        with self.assertRaises(PrinterFilesUnavailableError):
            connector.upload_printer_file('unused','part.gco',progress_callback=callback)
        callback.assert_called_once_with(failed=True)
        connector._comm.startFileTransfer.assert_not_called()
