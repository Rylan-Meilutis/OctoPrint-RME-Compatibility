# Changelog

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
