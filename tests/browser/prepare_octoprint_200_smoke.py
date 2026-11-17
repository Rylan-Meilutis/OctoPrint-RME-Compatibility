"""Create a disposable fixture; no live config, backups or printer connection."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

import yaml

root = Path(tempfile.mkdtemp(prefix="rme-octoprint-200-smoke-"))
os.chmod(root, 0o700)
credentials = {"user":"rme-smoke", "password":secrets.token_urlsafe(24),
               "api_key":secrets.token_hex(16), "basedir":str(root)}
config = {
    "accessControl":{"enabled":True},
    "api":{"enabled":True, "key":None},
    "server":{"firstRun":False, "onlineCheck":{"enabled":False},
              "pluginBlocklist":{"enabled":False}},
    "printerConnection":{"autoconnect":False},
    "plugins":{"_disabled":["announcements", "discovery", "achievements", "tracking",
                              "errortracking", "softwareupdate", "corewizard", "virtual_printer"],
               "rme_compatibility":{"firmware_auto_check":False, "auto_machine_profile":False}},
}
(root / "config.yaml").write_text(yaml.safe_dump(config))
(root / "credentials.private.json").write_text(json.dumps(credentials))
os.chmod(root / "config.yaml", 0o600)
os.chmod(root / "credentials.private.json", 0o600)
subprocess.run([sys.executable, "-m", "octoprint", "--basedir", str(root), "user", "add",
                credentials["user"], "--password", credentials["password"], "--admin"],
               check=True, stdout=subprocess.DEVNULL)
print(root)
