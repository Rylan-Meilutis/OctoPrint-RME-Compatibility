# Changelog

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
