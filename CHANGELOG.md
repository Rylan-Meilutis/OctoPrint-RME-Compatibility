# Changelog

## 0.1.0b13 — 2026-08-09

- Fixed legacy M998 firmware staging on installed RME builds by adding the
  Marlin string-argument sentinel required for the handler to see `P0`–`P3`.
- Replaced OctoPrint SD-card uploads with serialized, acknowledged, SHA-256
  verified RME FILE transfers whenever the printer advertises FILE WRITE.
- Made firmware transfer errors process-local, cleared them at the next
  transfer or automatically after 30 seconds, and excluded them from restart
  persistence.
- Added compact, visually selectable RME theme presets and tightened theme
  swatch/editor spacing.
- Promoted pending filament-provider resynchronization to a persistent global
  OctoPrint notification in addition to the top-bar actions.
- Reported `MMU idle` when no tool is active and displayed firmware MMU phase
  messages such as FINDA/nozzle loading in the compact top-bar status.

## 0.1.0b12 — 2026-08-09

- Queued firmware staging behind an in-progress USB capability probe or
  directory refresh instead of rejecting the request with a transient HTTP 409.

## 0.1.0b11 — 2026-08-09

- Staged and SHA-256-verified firmware through the current RME FILE service,
  avoiding the firmware's legacy `M998` numeric-phase parsing failure while
  retaining `M998` as a fallback for older RME builds.
- Triggered current-firmware bootloader handoff with `RME FILE FLASH` after a
  verified stage, including the existing one-click stage-and-flash workflow.
- Added large live color swatches to the current and editable theme fields in
  Settings while retaining the exact hexadecimal values.
- Limited firmware cancellation to an active firmware transfer so it cannot
  interrupt an unrelated USB storage operation.

## 0.1.0b10 — 2026-08-09

- Added an RME `/usb` storage browser with directory navigation, authenticated
  binary downloads, SHA-256-verified atomic uploads, mkdir, rename, delete,
  print, and BBF flash controls.
- Parsed the current firmware's `RME_FILE_*` records without corrupting file
  names and paths containing spaces, and serialized all 48-byte transactions.
- Fixed firmware workflow completion and empty-prompt records crashing the
  receive hook when no remote prompt was active.
- Ignored SpoolManager selection events that merely repeat the already-selected
  spool, preventing read-side event emission from causing prompts or writes.

## 0.1.0b9 — 2026-08-09

- Formatted firmware distance statistics as centimeters, meters, or kilometers
  and durations as compact seconds, minutes, hours, or days.
- Added RME printer settings for current/editable theme colors, lock behavior,
  temporary and persistent state-based lighting, filament presets, and remote
  screen navigation to the OctoPrint Settings page.
- Added explicit current-theme swatches and hex values to both Settings and the
  main RME controls.
- Added directional Printer → provider and Provider → printer filament sync,
  connection-time printer import, periodic printer polling, and persistent
  confirmation prompts before external provider selections change firmware.
- Added a one-click Stage and flash action that triggers the bootloader only
  after the selected BBF has transferred and passed printer-side verification.
- Refocused the RME tab on live firmware activity, tool/filament state, machine
  information, and telemetry; routine controls, synchronization, inventory
  management, firmware updates, and configuration now live in Settings.

## 0.1.0b8 — 2026-08-09

- Added the required OctoPrint `atcommand.sending` hook for the reserved
  `@RME` namespace. OctoPrint normally consumes every at-command locally and
  skips its serial write, which prevented machine discovery and all subsequent
  RME session traffic from ever reaching the firmware.

## 0.1.0b7 — 2026-08-09

- Added support for OctoPrint's spooled-upload contract. Large multipart files
  arrive as trusted `file.path` and `file.name` fields rather than an entry in
  Flask's `request.files`; both forms are now validated and stored atomically.

## 0.1.0b6 — 2026-08-09

- Fixed the OctoPrint body-size hook to return a blueprint-relative route.
  OctoPrint now registers the intended 33 MiB allowance on
  `/plugin/rme_compatibility/firmware` instead of double-prefixing the path and
  rejecting normal BBF uploads with a blank HTTP 400 before the plugin runs.

## 0.1.0b5 — 2026-08-09

- Fixed the native OctoPrint multipart upload path so root installations no
  longer interpret `//plugin/...` as a request to a host named `plugin`.

## 0.1.0b4 — 2026-08-09

- Routed browser-to-Pi firmware uploads through OctoPrint's authenticated
  multipart client so API-key and CSRF headers are applied consistently.
- Made firmware upload validation errors machine-readable and included the HTTP
  status and server explanation in the persistent browser notification.

## 0.1.0b3 — 2026-08-09

- Fixed the package-level Python compatibility declaration so OctoPrint 1.11
  no longer rejects the plugin as Python 2-only before importing it.
- Made the RME navbar item an always-visible frontend health indicator, with
  explicit disconnected, unsupported, ready, active-tool, and MMU states.
- Added live plugin discovery status and the complete firmware upload, staging,
  progress, verification, flash, and cancellation workflow to Settings.

## 0.1.0b2 — 2026-08-09

- Added an always-available OctoPrint navbar dropdown for RME multi-tool and
  MMU machines. It shows the active tool/material/color, remapped physical tool,
  all tool assignments, live MMU workflow state/progress, and firmware-provided
  recovery actions.
- Declared an explicit OctoPrint server restart after install/update and added
  a settings notice explaining that serial hooks, APIs, assets, and background
  services initialize after that restart.

## 0.1.0b1 — 2026-08-09

- Made SpoolManager, Spoolman, and built-in inventory mutually exclusive.
  Inactive-provider events are ignored, external selections clear stale
  built-in tool assignments, and an unavailable explicitly selected external
  provider is no longer silently replaced by built-in storage.

## 0.1.0.dev2 — 2026-08-09

- Added a live active-extruder indicator to both the RME tab and OctoPrint's
  main printer-state area.
- Displayed the logical tool, remapped physical tool, loaded material, color
  name, and a filament color swatch using firmware `M865 Q` metadata.
- Added immediate updates for transmitted `Tn` commands and refreshed loadout
  metadata after firmware-side filament workflows.
- Added dedicated presentation for the firmware's filament load/unload,
  chamber-vent, and filtration workflows while retaining every detailed MMU
  state/code, including selector, cutter, purge/ramming, homing, and test phases.
- Changed development tag guidance to OctoPrint-safe dotted versions, avoiding
  its legacy hyphen sanitization during update comparisons.
- Added capability-gated periodic polling of the split `RME_STATS`,
  `RME_STATS_OPERATIONS`, and `RME_STATS_FAILURES` response, preserving the
  firmware's meter/second units and distinct lifetime/reset failure counters.
- Added selectable SpoolManager and Spoolman providers plus a persistent
  built-in inventory fallback, with per-tool assignment controls in OctoPrint.
- Added an authenticated `filament-report` JSON endpoint for OrcaSlicer and
  other clients to poll inventory, printer selections, mapping, and colors.
- Added OrcaSlicer's auto-detected `selected-spools` endpoint alias.
- Routed pause, resume, and cancel service commands through OctoPrint's forced
  send path so RME firmware can consume them during blocking G-code.
- Kept elapsed print time and time remaining moving in OctoPrint while an RME
  blocking workflow is active.

## 0.1.0.dev1 — 2026-08-09

First development release.

- Added the RME event session, persistent remote workflow prompts, and named
  printer-dialog responses.
- Added bed-probing, heating, MMU, tool-change, runout, stuck-filament,
  firmware-update, and waste-bin progress details to OctoPrint.
- Added a synchronous print-start tool-mapping gate with an interaction-aware
  timeout and remapped Nozzle Filament Validator checks.
- Added bidirectional SpoolManager inventory, selection, color, material, and
  new-spool synchronization.
- Added machine-profile discovery, remote printer controls, and RME settings.
- Added acknowledged BBF upload, printer-side verification, and explicit
  bootloader flashing.
- Added Stable (`main`) and Beta (`beta`) OctoPrint update channels.
