# GitHub firmware discovery

Browser uploads and staging from the OctoPrint Files list also validate the
BBF's embedded version and machine fields against the support matrix, and check
its embedded application checksum. Filename and manifest names cannot override
these fields. Flash checks again against the connected printer and exact staged
file SHA-256. A recovered candidate without a matching local source must be
restaged. Matching new firmware additionally rejects incompatible selected-file
M997 handoffs before arming reboot. These checks supplement, not replace, the
bootloader's signature and hardware validation.

RME checks the official `Rylan-Meilutis/Prusa-Firmware-Buddy` GitHub releases
while an RME printer is connected and idle, at most once every six hours.
Settings can disable automatic checks or choose an exact firmware variant
(including MINI language packs). The firmware update panel also offers a
manual check and lists compatible assets among the latest 30 GitHub releases.
Machine support must match verified upstream Prusa release assets, even if an
older RME release contains a cross-built filename for another machine:
6.9.2 is INDX-only; 6.10.3 is XL-only; 6.8.1 supports CORE One and CORE One L.
6.5.3/6.5.7 support CORE One, CORE One L, MK4-family and MK3.5-family.
Unknown machine/version combinations are hidden and cannot be downloaded.
MINI requires its own supported release line; another machine's build is never
offered as a substitute. See the
[upstream releases](https://github.com/prusa3d/Prusa-Firmware-Buddy/releases).
This is an HTTPS metadata check, not a background flash.

New firmware advertises `firmware_running=1`; the plugin asks for
`@RME FIRMWARE RUNNING`. That returns an `app-sha256-v1` checksum calculated
from the running executable flash and cached per boot. The release's
`rme-firmware-manifest.json` maps each full BBF SHA-256 to its application
SHA-256/size. Matching application bytes suppress the update notice, even if
the release tag is reused. Different bytes offer an update. A version string,
staged USB candidate, successful upload, or remembered flash request is never
treated as proof of what is installed. Running identity is cleared on every
disconnect/reconnect and is not restored from persistent plugin state.

Older firmware or releases missing a manifest show **Installed checksum
unknown**. An explicit exact variant permits downloading for the initial
upgrade. Do not infer a match from an unchanged 6.9.0/6.10.1 version string.
When the printer identifies its model, incompatible configured variants are
rejected. Unrecognized model variants require explicit selection; the printer
bootloader remains the final compatibility/signature authority.

An administrator selects **Download selected BBF** and confirms. RME streams
the asset to a temporary Pi file, bounds size/time, validates the GitHub and
manifest SHA-256/size, and only then publishes the file in firmware storage.
Checksums in filenames preserve older downloads when a release is rebuilt.
Failed downloads remove only their temporary file. No arbitrary download URL
is accepted by the API. The existing **Upload candidate**, **Upload and flash**,
and **Flash verified candidate** actions remain separate and retain their
printing/transfer guards and explicit confirmation.

Release maintainers must generate the manifest from final BBFs using the
firmware repository's `utils/rme_firmware_manifest.py` and upload it alongside
each release's assets. Regenerate it whenever any BBF is replaced. The plugin
does not silently publish or modify GitHub releases.

Hardware checks remain required: running identity after success/rejection of
a flash, reconnect to a different printer, and end-to-end UI download/stage/
flash with the installed OctoPrint version. Network failures and missing
metadata are recoverable UI errors, not reasons to interrupt a print.

## Manual sync and GitHub credentials

In Settings → RME Compatibility → Firmware discovery, choose your exact machine
variant and click **Sync now**. This uses the current selection without needing
to save Settings first and does not wait for the six-hour automatic check.
Save Settings if you want the variant retained for future automatic checks.
The printer must be connected and idle. Results and errors appear beside the
button; use the firmware update panel to select and download a release.

RME uses the GitHub token already saved in OctoPrint Software Update, if present.
It stays server-side and is sent only to GitHub's API, never asset CDN redirects.

Use **RME → Firmware updates** to upload or flash, and **RME → Printer files**
for USB storage. File lists remain collapsed until opened. Settings contains
saved defaults and discovery policy, not duplicate firmware/file panels.
Transfer errors expose a recovery panel at the top of RME and a shortcut in
its top-bar menu. **Stop and disconnect** preserves retry data and locks traffic;
power-cycle the printer, then reconnect or confirm the reboot before resuming
or discarding the partial. It never assumes an unknown receiver accepts ASCII.
