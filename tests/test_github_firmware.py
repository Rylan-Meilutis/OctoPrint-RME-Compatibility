import hashlib
import logging
import os
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

from test_toolmap_gate import RmeCompatibilityPlugin
from octoprint_rme_compatibility import github_firmware as fw
from octoprint_rme_compatibility.protocol import parse_line


class FirmwareReleaseTests(unittest.TestCase):
    def test_token_only_sent_to_api_not_redirect_cdn(self):
        redirect = Mock(status_code=302, headers={"Location": "https://release-assets.githubusercontent.com/example"})
        success = Mock(status_code=200)
        with patch("requests.get", side_effect=[redirect, success]) as get:
            fw._response(fw.API + "/releases", token="test-secret")
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer test-secret")
        self.assertNotIn("Authorization", get.call_args_list[1].kwargs["headers"])

    def test_manual_variant_uses_current_selection_and_saved_credential(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state.update(connected=True, supported=True)
        plugin._settings = Mock()
        plugin._settings.get.return_value = ""
        plugin._settings.global_get.return_value = "test-secret"
        plugin._print_job_active = lambda: False
        plugin._publish = lambda: None
        plugin._logger = logging.getLogger("release-test")
        plugin._release_check_after = float("inf")
        with patch("octoprint_rme_compatibility.plugin.threading.Thread") as thread, patch.object(fw, "catalog", return_value=[]) as catalog:
            thread.side_effect = lambda **kw: types.SimpleNamespace(start=kw["target"])
            plugin._start_release_task(variant_override="coreone_indx")
            catalog.assert_called_once_with("coreone_indx", {}, token="test-secret")
        plugin._settings.global_get.assert_called_once_with(["plugins", "softwareupdate", "credentials", "github"])
        self.assertEqual(plugin._state["firmware_releases"]["status"], "ready")
        self.assertNotIn("test-secret", str(plugin._state))

    def fixture(self, manifest=True):
        asset = dict(id=123, name="coreone_indx_6.10.1-RME.bbf", size=1024,
                     digest="sha256:" + "a" * 64, browser_download_url=fw.DOWNLOAD + "v6.10.1-RME/coreone_indx_6.10.1-RME.bbf")
        assets = [asset]
        if manifest:
            assets.append(dict(name="rme-firmware-manifest.json", browser_download_url=fw.DOWNLOAD + "v6.10.1-RME/rme-firmware-manifest.json"))
        releases = [dict(tag_name="v6.10.1-RME", assets=assets)]
        metadata = dict(schema=1, algorithm="app-sha256-v1", assets=[dict(name=asset["name"], variant="coreone_indx", size=1024,
                     sha256="a" * 64, application_size=448, application_sha256="b" * 64)])
        return releases, metadata

    def test_compare_actual_application_not_bbf_or_version(self):
        releases, metadata = self.fixture()
        running = dict(algorithm="app-sha256-v1", sha256="b" * 64, size=448)
        with patch.object(fw, "get_json", side_effect=[releases, metadata]):
            self.assertEqual(fw.catalog("coreone_indx", running)[0]["comparison"], "current")
        running["sha256"] = "c" * 64
        with patch.object(fw, "get_json", side_effect=[releases, metadata]):
            self.assertEqual(fw.catalog("coreone_indx", running)[0]["comparison"], "different")

    def test_old_release_or_old_firmware_is_unknown(self):
        releases, _ = self.fixture(False)
        with patch.object(fw, "get_json", return_value=releases):
            self.assertEqual(fw.catalog("coreone_indx")[0]["comparison"], "unknown")

    def test_numbered_rebuild_uses_base_filename_and_identity(self):
        releases, metadata = self.fixture()
        releases[0]["tag_name"] += "-b2"
        with patch.object(fw, "get_json", side_effect=[releases, metadata]):
            result = fw.catalog("coreone_indx")
        self.assertEqual(result[0]["version"], "6.10.1-RME-b2")
        self.assertEqual(result[0]["name"], "coreone_indx_6.10.1-RME.bbf")
        self.assertEqual(result[0]["application_sha256"], "b" * 64)

    def test_exact_variant_and_manifest_mismatch(self):
        releases, metadata = self.fixture()
        with patch.object(fw, "get_json", return_value=releases):
            self.assertEqual(fw.catalog("coreone"), [])
        metadata["assets"][0]["sha256"] = "d" * 64
        with patch.object(fw, "get_json", side_effect=[releases, metadata]):
            with self.assertRaises(ValueError):
                fw.catalog("coreone_indx")
        self.assertIsNone(fw.variant_for("UNKNOWN"))
        self.assertEqual(fw.variant_for("Prusa-COREONEINDX"), "coreone_indx")

    def test_download_verifies_atomic_file_and_reuses_identical(self):
        data = b"x" * 1024
        asset = dict(name="coreone_indx_6.10.1-RME.bbf", sha256=hashlib.sha256(data).hexdigest(), size=len(data), url="unused")
        with tempfile.TemporaryDirectory() as directory, patch.object(fw, "chunks", return_value=[data]):
            name = fw.download(asset, directory)
            self.assertEqual(fw.download(asset, directory), name)
            self.assertEqual(os.listdir(directory), [name])
            with open(os.path.join(directory, name), "rb") as file:
                self.assertEqual(file.read(), data)

    def test_bad_download_is_removed_and_traversal_rejected(self):
        asset = dict(name="coreone_indx_6.10.1-RME.bbf", sha256="a" * 64, size=1024, url="unused")
        with tempfile.TemporaryDirectory() as directory, patch.object(fw, "chunks", return_value=[b"bad"]):
            with self.assertRaises(ValueError):
                fw.download(asset, directory)
            self.assertEqual(os.listdir(directory), [])
            asset["name"] = "../bad.bbf"
            with self.assertRaises(ValueError):
                fw.download(asset, directory)

    def test_non_repository_urls_rejected_before_network(self):
        for url in ("http://github.com/", "https://evil.invalid/", "https://api.github.com.evil.invalid/repos/anything"):
            with self.assertRaises(ValueError):
                fw._response(url)

    def test_running_record_is_separate_and_not_persisted(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state.update(connected=True, supported=True)
        plugin._schedule_publish = lambda: None
        plugin._handle_record(parse_line("RME_FIRMWARE_RUNNING algorithm=app-sha256-v1 model=COREONEINDX version=6.10.1-RME size=448 sha256=" + "b" * 64))
        self.assertEqual(plugin._state["running_firmware"]["sha256"], "b" * 64)
        self.assertIsNone(plugin._state["firmware"]["sha256"])
        self.assertNotIn("running_firmware", plugin._persistent_snapshot())

    def test_task_blocks_wrong_machine_and_printing(self):
        plugin = RmeCompatibilityPlugin()
        plugin._state.update(connected=True, supported=True, running_firmware={"model": "COREONEINDX"})
        plugin._settings = types.SimpleNamespace(get=lambda _: "coreone")
        plugin._print_job_active = lambda: False
        with self.assertRaisesRegex(ValueError, "does not match"):
            plugin._start_release_task()
        plugin._print_job_active = lambda: True
        with self.assertRaisesRegex(ValueError, "idle"):
            plugin._start_release_task()
