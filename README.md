# OctoPrint RME Compatibility

An OctoPrint plugin for the custom Prusa RME Buddy firmware in
`prusa-firmware-buddy`. It implements the firmware contracts documented in
`doc/rme_serial_handler_integration.md`, `doc/rme_serial_remote_protocol.md`,
and `doc/gcode/M998.md`.

## Features

- Negotiates `@RME` support on every serial connection, opens an event session,
  sends 10-second keepalives, detects event sequence gaps, and re-queries the
  authoritative firmware dialog.
- Adds live probing, heating, MMU, tool-change, runout, stuck-filament,
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
- Presents the firmware's dedicated MMU, filament load/unload, tool-change,
  runout, stuck-filament, pressure-advance, probing, heating, firmware-update,
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
- Exposes guarded remote encoder/click/back/home controls, printer lock status
  and PIN unlock, temporary and persistent light services, persistent theme
  colors, and synchronization of the eight RME user filament presets.
- Integrates bidirectionally with either OctoPrint-SpoolManager or
  OctoPrint-Spoolman. If neither is installed, a persistent built-in inventory
  tracks available and per-tool selected spools directly in this plugin.
  Seven active spools are published as stable short aliases in the printer's
  existing filament-load picker and the eighth entry is `NEW`. Printer-side
  choices update the active provider; selection and deselection update the
  firmware. Periodic reconciliation also catches inventory, weight, and other
  edits for which a provider does not emit an event.
- Exposes authenticated read-only `/plugin/rme_compatibility/selected-spools`
  and `/plugin/rme_compatibility/filament-report` aliases so OrcaSlicer and other
  clients can poll active tool, mapping, material/color loadout, available
  inventory, selected spools, and firmware statistics with an OctoPrint API key.
- Accepts signed `.bbf` files up to 32 MiB on the Pi, streams them with the
  acknowledged M998 Base64 protocol, verifies size and SHA-256 on the printer,
  and exposes a separate confirmed `M997 /usb/FWUPD.BBF` bootloader handoff.

The plugin always uses OctoPrint's serialized printer command queue. It never
opens a competing serial descriptor, suppresses normal Marlin responses, or
places `@RME` frames in sliced files.

## Remote prompts and recovery

Tool remapping and firmware action dialogs are rendered in the **RME** tab.
This includes MMU loading/errors, filament runout, stuck filament, tool-change
or pickup failures, purge-bucket/waste-bin warnings, and any future RME dialog
that supplies named actions. Prompt and workflow state is written to the Pi,
so it survives a browser refresh or OctoPrint restart. The printer remains the
authority: resolving an issue on its LCD produces a closed workflow or
`RME_PROMPT none`, which automatically removes the OctoPrint prompt.

Tool mapping is the first local-print preflight gate. After confirmation, the
mapping is supplied to Nozzle Filament Validator before OctoPrint releases the
job hold. The validator therefore compares each slicer's logical tool against
the chosen physical tool's nozzle, SpoolManager material, and spool identity.

## Filament inventory integration

Choose Automatic, SpoolManager, Spoolman, or Built-in under **Settings → RME
Compatibility → Filament inventory**. Full names, colors, remaining weights, and tool
assignments appear in the OctoPrint RME tab; the printer receives seven-character
aliases because that is the RME firmware's preset-name limit. Choosing `NEW` or
an unlinked built-in material on the printer opens a persistent form in
OctoPrint. Saving it creates a record in the active provider, selects it for the tool,
and writes the selected material/color back to firmware with `M865`.

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
semantics, and renders meter/second values with useful units. Once any response
has been seen, it polls at the configured interval. Firmware that returns an
`RME_ERROR` mentioning `STATS`, or does not respond, is probed only once per
connection and otherwise sees no statistics traffic.

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

Restart OctoPrint, connect the printer, and open the **RME** tab. The plugin
falls back quietly when `@RME MACHINE QUERY` is not supported.

## Update channels

OctoPrint's Software Update settings expose two release channels:

- **Stable** follows the `main` branch and ignores development prereleases.
- **Beta** follows the `beta` branch and receives beta/development GitHub
  prereleases in addition to stable releases.

Development tags use OctoPrint-safe PEP 440 versions such as `v0.1.0.dev3` and
are published from `beta`. Stable releases are tagged from `main`.

## Firmware update safety

Firmware storage on the Pi, transfer to printer USB, and flashing are three
separate user-visible operations. Transfer and flashing are rejected while a
print is active or paused. The plugin verifies the Pi copy before transfer and
the printer verifies the declared byte count and SHA-256 before renaming it to
`/usb/FWUPD.BBF`. The Prusa bootloader remains responsible for signature,
printer-model, and compatibility checks.

## Development

Protocol and transport tests do not require OctoPrint itself:

```console
python -m unittest discover -s tests -v
```

For integration testing, follow the matrix in the firmware's
`doc/rme_serial_handler_integration.md`: reconnect and sequence-gap recovery,
blocking heater/probing commands, MMU and filament errors, tool changes,
pressure-advance calibration, UI lock transitions, emergency stop, and both
legacy notification modes.
