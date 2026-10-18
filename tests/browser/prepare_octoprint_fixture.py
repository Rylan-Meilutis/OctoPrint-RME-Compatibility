"""Prepare private test data only; never restore backup credentials or connect a printer."""
import argparse
import json
from pathlib import Path
import tempfile
import zipfile

import yaml

parser = argparse.ArgumentParser()
parser.add_argument("backup", type=Path)
args = parser.parse_args()
root = Path(tempfile.mkdtemp(prefix="rme-settings-fixture-"))
with zipfile.ZipFile(args.backup) as archive:
    saved = yaml.safe_load(archive.read("basedir/config.yaml"))
    config = {
        "accessControl": {"enabled": True},
        "api": {"enabled": True, "key": "RME-PROFILE-LOCAL-ONLY"},
        "server": {"firstRun": False, "onlineCheck": {"enabled": False},
                   "pluginBlacklist": {"enabled": False}, "seenWizards": saved.get("server", {}).get("seenWizards", {})},
        "serial": {"autoconnect": False},
        "webcam": {"stream": "", "snapshot": "", "webcamEnabled": False},
        "printerProfiles": saved.get("printerProfiles", {}),
        "plugins": {
            "_disabled": ["discovery", "announcements", "tracking", "errortracking", "achievements",
                          "virtual_printer", "corewizard", "softwareupdate"],
            "rme_compatibility": dict(saved["plugins"].get("rme_compatibility", {}),
                                      firmware_auto_check=False, auto_machine_profile=False),
            "uicustomizer": dict(saved["plugins"].get("uicustomizer", {}), themeLocal=True),
            "SpoolManager": saved["plugins"].get("SpoolManager", {}),
            "octopod": {"server_url": "http://127.0.0.1:5517/profile-no-octopod", "tokens": []},
            "continuousprint": {"cp_network_name": ""},
            "dashboard": {k: v for k, v in saved["plugins"].get("dashboard", {}).items()
                          if k.startswith("show") or k.startswith("clearOn_") or k == "ETAUse12HTime"},
        },
    }
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    for name in archive.namelist():
        relative = name.removeprefix("basedir/")
        if not (relative.startswith("printerProfiles/") or relative in (
            "data/SpoolManager/spoolmanager.db", "data/rme_compatibility/state.json",
            "data/PrintTimeGenius/print_history.yaml",
        )):
            continue
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe backup path")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
print(json.dumps({"temporary_basedir": str(root), "printer_autoconnect": False,
                  "credentials_restored": False}))
