# OctoPrint RME Compatibility

An OctoPrint plugin for the custom Prusa RME Buddy firmware in
`prusa-firmware-buddy`. It implements the firmware contracts documented in
`doc/rme_serial_handler_integration.md`, `doc/rme_serial_remote_protocol.md`,
and `doc/gcode/M998.md`.

The supported firmware baselines are the `v6.5.7-RME` release, the maintained
`rme-v6.6.3` release branch, and the `v6.8.1-RME` release. The 6.6.3 and 6.8.1
host protocol documents are checked byte-for-byte, while their RME FILE
transport, durable-resume, and cause-specific INDX extrusion-recovery contracts
are exercised independently by the test suite.

## Features

- Negotiates `@RME` support on every serial connection, opens an event session,
  sends 10-second keepalives, detects event sequence gaps, and re-queries the
  authoritative firmware dialog.
- Adds live probing, heating, MMU, tool-change, runout, filament-movement,
  flow-pressure-limit, stuck-filament,
  pressure-advance, firmware-update, waste-bin, and generic workflow detail to
  OctoPrint's progress area. Phase elapsed time continues updating while a
  blocking G-code leaves normal file progress stationary.
- Keeps OctoPrint's elapsed print time ticking and remaining estimate counting
  down locally while a reported blocking workflow prevents normal progress
  updates from arriving.
- Routes pause (`M601`), resume (`M602`), and cancel (`M604`) through
  OctoPrint's forced command path. RME firmware consumes these from its service
  queue even while a heater, probe, MMU operation, or calibration is blocking.
- Shows the active extruder in the RME tab and beside OctoPrint's main progress
  area, including logical-to-physical remapping, loaded material, and the
  firmware/SpoolManager filament color.
- Adds a persistent top-navbar dropdown on detected RME multi-tool and MMU
  machines. Its compact indicator always shows the selected tool and color;
  the dropdown lists every logical/physical assignment and live MMU phase,
  message, progress, and firmware-provided recovery actions without requiring
  the RME or State tab to be open.
- Presents the firmware's dedicated MMU, filament load/unload, tool-change,
  runout, filament-movement, flow-pressure-limit, stuck-filament,
  pressure-advance, probing, heating, firmware-update,
  waste-bin, chamber-vent, and filtration workflows. Detailed MMU states cover
  its load, unload, selector, cutter, purge/ramming, homing, and hardware-test
  phases. Unknown future workflow IDs remain visible with a generated title.
- Probes for the firmware's split statistics response and, when available,
  periodically refreshes distance travelled, filament extruded, MMU changes,
  tool picks, print/filtering time, individual failure counters, and any
  additional future counters.
- Persists active printer prompts and workflow details on the Pi so recovery
  controls survive browser refreshes. Responses use stable named actions and
  are checked again against the printer after each response.
- Holds multi-tool prints at their start, asks for a one-to-one logical to
  physical tool mapping, saves the selection, applies it through the immediate
  RME protocol, and then releases the print. The saved mapping can instead be
  applied automatically.
  The hold is acquired synchronously through OctoPrint's `beforePrintStarted`
  pipeline, so it also covers API and plugin-initiated local prints before any
  print-file G-code is sent. On timeout, an untouched prompt keeps the current
  firmware mapping and proceeds without writing a new one; its timer pauses
  once the operator starts interacting.
  Cancelling while this gate is still ahead of the first print-file command
  suppresses the user-configured `afterPrintCancelled` macro while retaining
  OctoPrint's internal cancellation handling.
- Reads the printer's envelope, logical tool count, shared-nozzle status, and
  live maximum feed rates, then updates the active OctoPrint printer profile.
  If an MMU is still enabling during the initial machine query, its later
  enabled `M865` loadout records expand the profile to all five shared-nozzle
  logical tools automatically.
- Exposes printer lock status and PIN unlock, temporary and persistent light
  services, persistent theme
  colors, and synchronization of the eight RME user filament presets. The RME
  Settings page shows the printer-reported current theme and provides editors
  for the firmware-supported lock, theme, lighting, and filament settings.
- Integrates bidirectionally with either OctoPrint-SpoolManager or
  OctoPrint-Spoolman. If neither is installed, a persistent built-in inventory
  tracks available and per-tool selected spools directly in this plugin.
  Seven active spools are published as stable short aliases in the printer's
  existing filament-load picker and the eighth entry is `NEW`. Printer-side
  choices update the active provider. External selection and deselection opens
  a persistent confirmation before changing firmware, while explicit
  Printer → provider and Provider → printer controls resolve manual edits.
  Revisioned `RME_CHANGE` events import LCD-side configuration changes and
  refresh only the affected domain. Provider plugin events update inventory and
  selections; idle connections do not poll firmware settings or providers.
- Exposes authenticated read-only `/plugin/rme_compatibility/selected-spools`
  and `/plugin/rme_compatibility/filament-report` aliases so OrcaSlicer and other
  clients can poll active tool, mapping, material/color loadout, available
  inventory, selected spools, and firmware statistics with an OctoPrint API key.
- Integrates the current RME firmware's sandboxed `/usb` filesystem in Settings:
  browse directories, download files through authenticated OctoPrint, upload
  with negotiated raw binary frames when firmware advertises `binary=1`
  (otherwise pipelined bulk or acknowledged text transport), SHA-256 atomic
  finalization, create and
  rename directories/files, delete entries, start USB prints, and flash BBFs.
  Paths are percent-encoded and cannot escape the printer's user-visible USB
  volume.
- Persists the current firmware's required upload manifest before BEGIN,
  including the exact final path, size, SHA-256, retained Pi source, and
  selected transport. Interrupted transfers survive OctoPrint/printer
  reconnects and present explicit Resume and Discard actions. Discard recovers
  with a matching text/bulk BEGIN and waits for confirmed line-mode ABORT;
  ordinary failure and cancellation preserve the resumable partial. A
  maintained-firmware `resume_failed resumable=1` response retries only the
  identical BEGIN, retaining the firmware-authoritative verified prefix rather
  than falling back to an offset-zero upload.
- Supports explicit lost-manifest cleanup from an operator-supplied final path.
  It probes only the derived `.rme-part` and `.rme-meta` siblings, never scans
  hidden transfer files, never parses `.rme-meta`, and never deletes the
  firmware-owned `.rme-old` rollback copy.
- Hooks OctoPrint's standard SD-card upload action and replaces M28/M29
streaming with acknowledged, atomic RME FILE transfers when FILE WRITE is
advertised. OctoPrint still receives its normal transfer lifecycle callbacks.
Printer storage and firmware operations are rejected while a print is active;
if a print races an operation that already owns the transfer channel, the print
is held and canceled without inserting print-control G-code into that channel.
- Accepts signed `.bbf` files up to 32 MiB on the Pi. Current RME firmware
  accepts the compatible `FWUPD.BBF` wire name but protects the verified file
  as hidden `/usb/FWUPD.RME` until an explicit RME FILE FLASH request hands it
  to the bootloader. It can be staged and flashed in one operation, and the browser shows
  upload-to-Pi progress separately from the printer transfer. Candidate truth,
  unstage, and bootloader handoff use the authoritative current
  `@RME FIRMWARE QUERY`, `@RME FIRMWARE UNSTAGE`, and FILE FLASH operations.
- Adds firmware actions to `.BBF` entries in OctoPrint's standard local Files
  sidebar, so an existing OctoPrint file can be uploaded as a verified
  candidate or uploaded and flashed without making a second browser upload.

The plugin always uses OctoPrint's serialized printer command queue. It never
opens a competing serial descriptor, suppresses normal Marlin responses, or
places `@RME` frames in sliced files.

## Remote prompts and recovery

Live firmware state, tool mapping, and firmware action dialogs are rendered in
the **RME** tab. Routine configuration, inventory synchronization, remote
controls, and firmware update operations live in **Settings → RME
Compatibility**, keeping the main tab focused on status and telemetry.
This includes MMU loading/errors, filament runout, filament-not-moving and
flow-pressure-limit faults, stuck filament, tool-change
or pickup failures, purge-bucket/waste-bin warnings, and any future RME dialog
that supplies named actions. Prompt and workflow state is written to the Pi,
so it survives a browser refresh or OctoPrint restart. The printer remains the
authority: resolving an issue on its LCD produces a closed workflow or
`RME_PROMPT none`, which automatically removes the OctoPrint prompt.

Current INDX firmware reports automatic extrusion faults with stable
workflow/code pairs: `filament_runout/runout`,
`filament_movement/not_moving`, and
`extrusion_flow_limit/flow_limit`. The plugin keeps that original cause visible
while the shared M1601 load/unload recovery reports progress, queries the
firmware's Continue/Unload/Abort actions for movement and flow-limit faults,
and clears the cause only when recovery closes or the print ends. The printer's
`M591 S` and `M591 U` settings independently control the optional runout and
movement detectors; calibrated flow-pressure-limit protection remains enabled.

Tool mapping is the first local-print preflight gate. After confirmation, the
mapping is supplied to Nozzle Filament Validator before OctoPrint releases the
job hold. The validator therefore compares each slicer's logical tool against
the chosen physical tool's nozzle, SpoolManager material, and spool identity.

## Filament inventory integration

Choose Automatic, SpoolManager, or Spoolman under **Settings → RME
Compatibility → Filament inventory**. Full names, colors, remaining weights, and tool
assignments appear in the OctoPrint RME tab; the printer receives seven-character
aliases because that is the RME firmware's preset-name limit. Choosing `NEW` or
an unlinked built-in material on the printer opens a persistent form in
OctoPrint. Saving it creates a record in the active provider, selects it for the tool,
and writes the selected material/color back to firmware with `M865`.
On connection the plugin first builds the seven-slot alias table, then reads
the printer's current `M865` assignments into the provider. Provider-originated
selection changes wait for confirmation rather than silently overwriting the
printer; both synchronization directions are also available as manual buttons.

Only one inventory backend is active at a time. SpoolManager and Spoolman each
disable the built-in RME inventory when selected, and events from an inactive
provider are ignored. Automatic prefers SpoolManager, then Spoolman, and starts
the built-in backend only when neither external plugin is available. An
explicitly selected external provider reports unavailable instead of silently
switching to built-in storage.

SpoolManager currently exposes events and implementation methods rather than
registered public helpers. All such access is feature-detected and isolated in
`spoolmanager.py`; an unavailable external provider does not affect non-filament
RME features.
The Spoolman adapter reuses the companion plugin's configured server URL, TLS
policy, and API credentials. The built-in provider requires no other service
and persists its inventory and printer-reported selections on the Pi.

## Firmware statistics contract

After RME machine discovery, the plugin sends one `@RME STATS QUERY` capability
probe. Current firmware replies with three independently parseable records:

```text
RME_STATS distance_x_m=1234.5 distance_y_m=456 distance_z_m=12 distance_total_m=1702.5 extruded_m=82 print_time_s=900 current_print_time_s=120 jobs_started=7
RME_STATS_OPERATIONS tool_picks=12 mmu_changes=8 filtering_time_s=300 wastebin_pellets=19
RME_STATS_FAILURES crash_x=1 crash_y=0 power_panics=2 mmu_load_since_reset=0 mmu_load_total=3 mmu_general_since_reset=0 mmu_general_total=1
```

Optional hardware fields are omitted by firmware when unavailable. Keys are
deliberately forward-compatible: the plugin merges the unordered snapshots,
retains unknown counters, preserves `_m`, `_s`, `_total`, and `_since_reset`
semantics, and renders meter/second values with useful units. Statistics are
queried once after discovery and refreshed after a print finishes; they are not
polled continuously. Firmware that returns an `RME_ERROR` mentioning `STATS`,
or does not respond, is probed only once per connection and otherwise sees no
statistics traffic.

## Priority print controls

On an RME printer, any OctoPrint pause, resume, or cancel transition—and any
explicit `M601`, `M602`, or `M604` submitted by an API client or another
plugin—is submitted with OctoPrint's `force=True` path and then written with
its guarded out-of-band communication primitives, without waiting for the
previous command's `ok`. The original explicit command is removed from the
normal queue to prevent a delayed duplicate. The firmware must advertise and
preserve its priority/service command receiver so these commands are consumed
from serial RX while foreground G-code is blocked.
Firmware `paused`/`resumed` completion actions only synchronize OctoPrint state
and are not echoed back as duplicate service commands.
On a printer that did not pass RME discovery, the hooks do nothing and
OctoPrint retains its standard behavior.

## Install

Install through OctoPrint's Plugin Manager using this repository URL, or from a
checkout in the same Python environment as OctoPrint:

```console
pip install .
```

Restart OctoPrint after every installation or update, then connect the printer
and open the **RME** tab. OctoPrint Plugin Manager and Software Update recognize
this requirement and prompt or restart automatically when a server restart
command is configured. The plugin's serial hooks, navbar, API, and background
services are not fully initialized until that restart. It falls back quietly
when `@RME MACHINE QUERY` is not supported.

## Update channels

OctoPrint's Software Update settings expose two release channels:

- **Stable** follows the `main` branch and ignores development prereleases.
- **Beta** follows the `beta` branch and receives beta/development GitHub
  prereleases in addition to stable releases.

Beta tags use OctoPrint-safe PEP 440 versions such as `v0.1.0b1` and
`v0.1.0b2` and are published from `beta`. Stable releases are tagged from
`main`.

## Firmware update safety

Firmware storage on the Pi, transfer to printer USB, and flashing remain
separately controllable, and a combined **Stage and flash** action performs the
last two in one click while still waiting for printer-side verification before
bootloader handoff. Uploads, downloads, storage mutations, firmware staging,
and flashing are rejected while a print is active or paused. Conversely, a
print that starts while one of those operations owns the printer is canceled.
The plugin verifies the Pi copy before transfer and
the printer verifies the declared byte count and SHA-256 before publishing its
protected `/usb/FWUPD.RME` stage. The Prusa bootloader remains responsible for signature,
printer-model, and compatibility checks. After transfer reaches 100%, the
current firmware hashes that protected multi-megabyte candidate synchronously;
the UI remains in **verifying** until the authoritative result arrives.

## Development

Protocol and transport tests do not require OctoPrint itself:

```console
python -m unittest discover -s tests -v
```

When the Buddy firmware checkout is adjacent to this repository, the serial
link suite also reads its shared RME transfer constants and drives the real
plugin file service through an independent fragmented-byte firmware peer. This
is the release gate for negotiated binary windows, CRC/NACK recovery, abort,
disconnect/inactivity suspension, durable resume, fallback transports, SHA-256
publication, and firmware flash selection.

For integration testing, follow the matrix in the firmware's
`doc/rme_serial_handler_integration.md`: reconnect and sequence-gap recovery,
blocking heater/probing commands, MMU and filament errors, tool changes,
pressure-advance calibration, UI lock transitions, emergency stop, and both
legacy notification modes.
