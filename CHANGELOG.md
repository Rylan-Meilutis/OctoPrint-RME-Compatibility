# Changelog

## 0.1.0b41 — 2026-08-12

- Aligned with Buddy firmware `50293bda570fd5a0ca6771baca81a06fd7b4e5f5`
  and its hidden-artifact contract. Private `FWUPD.RME`, `FWUPD.UI`,
  `.rme-part`, `.rme-meta`, and `.rme-old` files are never inferred from an
  ordinary USB listing.
- Added a synchronous, atomic host transfer manifest written before every FILE
  BEGIN. The exact destination, byte count, SHA-256, retained Pi source, and
  selected transport now survive OctoPrint restarts and printer reconnects.
- Added explicit Resume and Discard workflows for interrupted uploads. Resume
  repeats the identical BEGIN and honors the firmware-recovered offset;
  discard uses text/bulk BEGIN followed by a confirmed line-mode ABORT so the
  firmware removes the partial and metadata together.
- Retain line-mode partials after transport failures and cancellation instead
  of issuing an unconfirmed implicit ABORT. Provenance is cleared only after a
  verified completion record or `RME_FILE_ABORTED`.
- Added lost-manifest cleanup for a user-supplied final path. It probes and
  deletes only the mechanically derived `.rme-part` and `.rme-meta` siblings,
  treats `not_found` as clean, and never scans, parses metadata, or touches
  `.rme-old`.
- Retain a durable copy of every upload source until completion/discard and
  expose interrupted-transfer status plus recovery actions in Settings and
  the top-bar transfer indicator.

## 0.1.0b40 — 2026-08-12

- Aligned upload transport selection with current Buddy firmware
  `e20edcbb946f0c8b6eb61b0e517e91af3a3dabc0`: `binary=1` now directly selects
  the documented raw path without the removed `binary_resync` compatibility
  gate.
- Use the firmware-negotiated 1024-byte/eight-frame binary window and
  384-byte/four-command bulk window without the obsolete host-side frame
  reductions or pacing added for the now-fixed re-entrant parser corruption.
- Treat the current firmware's inactivity `RME_FILE_BINARY_SUSPENDED` record as
  authoritative line-mode recovery, retaining its durable prefix and avoiding
  a false reboot-required communication lock.
- Removed legacy M998 upload, M997 flash, protected-file STAT, and generic
  DELETE staging fallbacks. Firmware candidate status, unstage, and flash now
  use only the current FILE/FIRMWARE protocol and validate candidate size and
  SHA-256 before enabling installation.

## 0.1.0b39 — 2026-08-12

- Added the current firmware's authoritative `@RME FIRMWARE QUERY` lifecycle.
  The plugin now reports a candidate only when firmware returns `candidate=1`,
  preserves its verified size/SHA-256, distinguishes an unarmed ready candidate
  from `armed=1 state=restarting`, and clears stale UI state when firmware
  reports no candidate.
- Replaced generic deletion of protected `FWUPD.RME` with the idempotent
  `@RME FIRMWARE UNSTAGE` operation when `firmware_unstage=1` is advertised.
  Older firmware retains the provenance-gated compatibility fallback.
- Enabled the raw binary fast path for current firmware advertising a positive
  `binary_timeout_ms`, the documented bounded-recovery release contract, while
  keeping older firmware without either recovery capability on safe bulk.
- Added `RME_FILE_BINARY_SUSPENDED` handling so an inactivity recovery returns
  cleanly to line mode and resumes the durable prefix through bulk transport
  instead of falsely latching a reboot-required communication failure.
- Firmware uploads now require the authoritative ready candidate to match the
  uploaded byte count and SHA-256 before the UI enables flashing.

## 0.1.0b38 — 2026-08-12

- Disabled raw binary uploads for firmware that does not explicitly advertise
  `binary_resync=1`. Current firmware through `0702267843` can still become
  permanently silent at 512-byte payloads and ignore its abort frame, so its
  `binary=1` capability alone is not safe enough to enter raw mode.
- Use the paced, cumulative-ACK bulk transport for current firmware. This is
  slower than raw framing but retains byte-count/SHA-256 verification, atomic
  publication, durable resume, shared transfer latching, and—critically—keeps
  the printer in recoverable line mode.
- Extended the upstream firmware handoff with the capability gate required to
  re-enable binary transfers after bounded parser resynchronization is added.

## 0.1.0b37 — 2026-08-12

- Started current-firmware raw uploads at a reliable 512-byte CDC boundary and
  immediately reduce/cap the payload after reasoned CRC or oversize NACKs.
  Stale offset-mismatch responses from an already-sent window no longer hide
  the diagnostic that should drive recovery.
- Added Upload candidate and Upload and flash actions to `.BBF` entries in
  OctoPrint's standard local Files sidebar. These use the same print/transfer
  exclusion, verification, progress, recovery, and explicit flash workflow as
  firmware selected in RME settings.
- Parsed the current firmware's schema-2 lighting state matrix, live state, and
  timeout policy while preserving event-driven refresh behavior and avoiding
  periodic configuration polling.
- Added `FIRMWARE_UPSTREAM_REQUIRED_FIXES.md`, validated against firmware
  `0702267843`, covering raw-parser resynchronization, abort/reconnect recovery,
  authoritative candidate/bootloader-stage reporting, unstage, shared latches,
  and free-air INDEX PA calibration followed by pellet-forming wiping.

## 0.1.0b36 — 2026-08-12

- Added an idempotent “Unstage from printer” action that removes only the
  protected `FWUPD.RME` candidate and preserves firmware files stored on the
  Pi and ordinary BBFs downloaded by Prusa Connect.
- Stopped persisting connection-local workflow/prompt records and reconcile a
  restored protected candidate only when the plugin already has upload
  provenance; arbitrary USB firmware files are never promoted into update state.
- Renamed the pre-flash state from “staged” to “ready”: a verified
  `FWUPD.RME` candidate is not described as bootloader-staged until firmware
  actually confirms the M997 restart handoff.
- Distinguished a queued FILE FLASH acknowledgement from the firmware's actual
  `RME_FIRMWARE_RESTART` confirmation. A later authoritative
  `RME_SESSION ... printer_state=IDLE` clears a stale handoff instead of
  canceling a valid print.
- Released the direct transfer-conflict job hold during cancellation, allowing
  OctoPrint to leave `Cancelling` after an interlocked print start.
- Allowed long firmware workflow messages to wrap in the top workflow strip so
  their status, progress, and elapsed time remain readable on narrow screens.
- Documented the upstream `RME_FIRMWARE` status/unstage protocol needed for the
  firmware to distinguish a verified candidate from an armed bootloader update.

## 0.1.0b35 — 2026-08-12

- Matched the current firmware's durable, cross-transport upload-resume
  contract. A confirmed binary suspension now falls directly into a matching
  bulk or legacy BEGIN without sending the line-mode ABORT that would discard
  the verified partial file.
- Parsed the extended `RME_FILE_BINARY_ABORTED offset=... resumable=1` and
  structured FILE error records, preventing valid current-firmware responses
  from being mistaken for teardown timeouts that require a power cycle.
- Resumed legacy text fallback from the firmware-reported READY offset, so all
  three upload transports preserve an already committed prefix.
- Restored the firmware-advertised 1024-byte raw-frame fast path at startup,
  while retaining adaptive reduction after repeated NACKs.
- Confirmed that protected `FWUPD.RME` staging, explicit FILE FLASH, the shared
  RME/Connect/Link transfer latch, and INDX free-air PA capture/pellet wiping
  match the current on-disk firmware implementation and protocol docs.

## 0.1.0b34 — 2026-08-11

- Added host-side support for the firmware's shared RME/Prusa Connect/PrusaLink
  activity latch. `transfer_busy` and remote `printer_busy` responses now pause
  and retry the queued RME operation instead of turning ordinary contention
  into an upload failure.
- Kept OctoPrint's local RME operation lock held while waiting so file,
  firmware, and print actions cannot overtake one another.
- Delayed reservation of OctoPrint's raw writer until firmware has granted the
  binary upload and returned `RME_FILE_BINARY_READY`; a long Connect transfer
  can no longer wedge normal line traffic while an RME upload waits.
- Added `FIRMWARE_UPSTREAM_TRANSFER_LATCH.md`, a clean upstream-firmware
  handoff for the shared storage/print latch and INDX PA free-air pellet cycle.

## 0.1.0b33 — 2026-08-11

- Turned an unconfirmed binary teardown into a durable printer-reboot safety
  latch. The navbar and firmware settings show a persistent reboot warning,
  and the state survives OctoPrint restarts.
- Blocked RME commands, batches, keepalives, priority controls, file/firmware
  actions, session restoration, configuration discovery, and print starts
  while recovery is required. A connection attempted while latched is closed
  immediately so OctoPrint cannot continue normal serial traffic.
- Clear the latch automatically only when a firmware `start` banner proves a
  reboot. If that banner occurred while disconnected, settings provide an
  explicit “I rebooted the printer” confirmation before reconnecting.

## 0.1.0b32 — 2026-08-11

- Kept the navbar transfer percentage in a dedicated non-shrinking badge, so
  narrow headers may shorten the operation label without hiding progress.
- Started current-firmware binary uploads with reliable 512-byte atomic writes
  and halved frame pacing to 5 ms. After 16 clean cumulative-ACK windows the
  uploader restores larger frames up to the firmware's negotiated maximum;
  repeated NACKs still reduce them for recovery.
- Verified the firmware's hidden `/usb/FWUPD.RME` stage with a fresh `STAT`
  and exact byte count before enabling Flash. The UI now states explicitly
  that protected staged firmware is intentionally absent from the printer's
  ordinary BBF picker and must be installed with the plugin's FILE FLASH action.
- Disconnect OctoPrint after an unconfirmed raw abort so normal line traffic
  cannot continue feeding a firmware receiver that may still be in binary
  mode. The printer must then be reconnected or power-cycled before retrying.

## 0.1.0b31 — 2026-08-11

- Increased raw-frame pacing from 1 ms to 10 ms so Buddy's application-level
  binary decoder can drain CDC buffers without the NACK storms that reduced
  effective upload throughput. Negotiated 1024-byte frames still target about
  100 KiB/s, or roughly 40 seconds for a 4 MiB firmware image.
- Made `RME_FILE_BINARY_ABORTED` mandatory before returning to line mode. If
  raw abort cannot be confirmed, the upload stops with explicit recovery
  guidance and sends no ASCII abort or fallback command into an uncertain raw
  parser state. Disconnecting or power-cycling clears that safety latch.
- Made release of OctoPrint's reserved raw writer synchronous. After binary
  completion or abort, the plugin now waits for the sending hook to consume its
  sentinel and exit before queuing the acknowledged line-mode fence, removing
  the race that caused that fence itself to time out.

## 0.1.0b30 — 2026-08-11

- Added an acknowledged line-mode abort fence between a failed raw-binary
  upload and its text fallback. Bulk mode now starts only after the firmware
  confirms that binary parsing has ended and its upload state is empty,
  preventing immediate `upload_state` failures during fallback.
- Repeated binary NACKs at one offset now progressively reduce the raw frame
  size from the negotiated maximum before abandoning the fast transport. This
  improves recovery from marginal USB/CDC packet boundaries while retaining
  the firmware-advertised cumulative ACK window.

## 0.1.0b29 — 2026-08-11

- Stopped periodic session keepalive acknowledgements from being interpreted
  as newly opened sessions. The initial inactive-to-active lease transition
  still loads the complete configuration once, but subsequent ten-second
  keepalives no longer trigger repeated dialog, lock, theme, light, filament,
  color, manufacturer, and tool-map queries.

## 0.1.0b28 — 2026-08-11

- Prevented corrupted bulk-fallback bursts by limiting Base64 payloads to 192
  bytes and pacing the firmware's four-command cumulative-ACK window.
- A recoverable binary-transfer failure now applies only to that operation;
  later uploads retry the preferred fast binary transport instead of remaining
  downgraded to bulk mode until OctoPrint restarts.
- Made print jobs and printer file/firmware operations mutually exclusive:
  active prints reject storage and firmware actions, while a print that races
  an active transfer or flash is held and canceled without injecting a serial
  cancel command into the transfer stream.

## 0.1.0b27 — 2026-08-11

- Added live firmware and printer-file transfer status to the RME/MMU navbar
  item, including compact percentage text, operation-specific icons, detailed
  hover/dropdown text, and a dropdown progress bar.
- Navbar progress covers browser-to-OctoPrint firmware/file uploads,
  OctoPrint-to-printer firmware/file uploads, printer-to-Pi downloads,
  verification, flashing, and restart handoff states.
- Kept exclusive ownership of OctoPrint's serial writer for the complete raw
  upload, paced CDC frame boundaries, and extended stale-NACK draining so
  background queries cannot corrupt a binary firmware transfer.
- Matched RME firmware build 8's renamed session lease and protected
  `FWUPD.RME` staging/explicit-flash contract.

## 0.1.0b26 — 2026-08-10

- Fixed pipelined binary recovery counting NACKs from already-in-flight frames
  as separate failed retransmissions at the same offset.
- After a binary NACK, the uploader now drains the rejected window before
  retransmitting from the committed offset. It retains the negotiated
  eight-frame cadence because current firmware emits cumulative ACKs only at
  that boundary (or the end of the file).

## 0.1.0b25 — 2026-08-10

- Removed the remaining timed filament-provider reconciliation and its obsolete
  interval setting. SpoolManager, Spoolman, and the built-in provider now
  synchronize only on connection, provider events, explicit user actions, or
  relevant revisioned firmware changes.
- Idle connections no longer poll statistics, firmware configuration, or
  filament providers. The required `@RME SESSION KEEPALIVE` every 10 seconds is
  retained because current firmware expires the RME control lease after 30
  seconds without it.

## 0.1.0b24 — 2026-08-10

- Aligned the plugin with the current Buddy RME 6.6.3 protocol: binary upload
  frames now use the firmware-negotiated size (currently 1024 bytes), and
  session baud hints, memory statistics, binary-read control records, binary
  abort confirmation, and firmware-restart records are parsed and preserved.
- Raw uploads now keep exclusive ownership of OctoPrint's serial writer until
  the firmware confirms `RME_FILE_BINARY_COMPLETE` or
  `RME_FILE_BINARY_ABORTED`; normal line traffic can no longer resume during
  the firmware's final hash/rename or abort transition.
- Binary upload cancellation now performs one confirmed abort handshake.
- Fast raw downloads remain on the protocol's supported Base64 fallback because
  OctoPrint 1.11.2 owns and decodes its serial `readline()` before plugin hooks;
  attempting a second raw reader would corrupt the shared receive stream.

## 0.1.0b23 — 2026-08-10

- Firmware and USB uploads now prefer the negotiated raw-binary transport:
  CRC32 frames in eight-frame cumulative-ACK windows.
- Raw transfers exclusively reserve OctoPrint's existing serial writer thread,
  preventing normal G-code or plugin traffic from being interleaved while the
  firmware's receiver is in binary mode.
- Binary NACKs retry from the firmware's last committed offset; failed raw
  negotiation or transport safely aborts and falls back to text bulk upload.
- Upload completion still requires firmware-side byte-count and full-file
  SHA-256 verification before the atomic file is exposed or flashed.

## 0.1.0b22 — 2026-08-10

- Fixed firmware and USB uploads failing with `write_failed` when the
  negotiated 384-byte Base64 chunks produced commands longer than the safe
  OctoPrint serial-command boundary.
- Text-mode bulk uploads now use 320-byte chunks while retaining the firmware's
  four-command cumulative acknowledgement window.

## 0.1.0b21 — 2026-08-10

- Fixed a provider synchronization feedback loop that repeatedly rewrote RME
  colors, filament presets, and manufacturer assignments, then queried the
  complete firmware catalogs again.
- Provider-originated transaction acknowledgements now advance session state
  without scheduling another configuration refresh.
- Unchanged provider snapshots are now idempotent, while reconnects and actual
  provider metadata or selection changes still publish to the firmware.

## 0.1.0b20 — 2026-08-10

- Removed the obsolete configurable statistics polling interval and all
  steady-state `@RME STATS QUERY` traffic.
- Kept one statistics capability/snapshot query after RME discovery and one
  supported snapshot refresh after each print completes.

## 0.1.0b19 — 2026-08-10

- Replaced request-context-bound printer downloads with atomic background
  printer-to-Pi jobs, byte-count validation, live progress, durable Pi copies,
  and a stable follow-up browser download.
- Added explicit **Download to Pi** and **Download to device** choices; the
  latter completes the printer transfer on the Pi before sending the file to
  the browser.
- Integrated RME USB files into OctoPrint's native Files view in place of its
  line-based SD listing, including RME-backed upload, download, rename, move,
  and delete actions.
- Added `.gcode`, `.gco`, `.bgcode`, `.bbf`, and Buddy dump `.bin` visibility
  while preventing firmware/dump artifacts from being selected or sliced.
- Suppressed native `M20` refreshes only after positive RME FILE discovery and
  retained normal OctoPrint SD behavior for non-RME printers.

## 0.1.0b18 — 2026-08-10

- Deferred statistics polling and event-triggered configuration snapshots while
  printing or paused so background RME reads cannot compete with streamed job
  G-code in OctoPrint's normal command queue.
- Deferred automatic manufacturer/profile publication until the active job
  finishes, then reconciled it automatically.
- Kept the minimal RME session keepalive active during jobs so remote workflow
  and error events remain available without enabling telemetry polling.

## 0.1.0b17 — 2026-08-10

- Fixed current Buddy `loaded_filament` parsing so its manufacturer field no
  longer prevents color, material, or provider selection synchronization.
- Made SpoolManager and Spoolman authoritative whenever either configured
  provider is available; the local inventory is now only a no-provider
  fallback.
- Added bidirectional RME manufacturer profile and assignment bridging, plus
  external-provider custom color profile publication.
- Replaced local spool editing controls with a clear provider ownership message
  while an external inventory provider is active.
- Removed the redundant restart-required settings banner and obsolete direct
  user-filament editor.
- Avoided unchanged periodic profile rewrites to reduce serial and UI churn.

## 0.1.0b16 — 2026-08-09

- Negotiated the current RME FILE capabilities and accelerated uploads with
  four-frame, 384-byte bulk windows and cumulative acknowledgements, while
  retaining the legacy 48-byte fallback.
- Adopted `events=31`, revisioned `RME_CHANGE` synchronization, per-mutation
  transaction IDs, gap recovery, and event-driven domain refreshes instead of
  steady-state printer settings polling.
- Coalesced serial-record persistence and WebSocket publication, stopped
  rebuilding unchanged spool selectors, and animation-frame-throttled core UI
  rendering to remove plugin-caused UI churn.
- Added separate browser-to-Pi firmware upload progress using OctoPrint's
  authenticated form client.
- Simplified theme editing to one clickable color swatch per field and removed
  blind Encoder, Click, Back, and Home controls from Settings.

## 0.1.0b15 — 2026-08-09

- Reported firmware staging as queued while waiting for the serialized printer
  USB service and explicitly stated that no bytes have been sent yet.
- Changed the status to starting only after the RME FILE operation lock is
  acquired immediately before `WRITE_BEGIN`.
- Made cancellation of a queued firmware transfer leave the unrelated USB
  operation ahead of it untouched and stop before the first firmware command.

## 0.1.0b14 — 2026-08-09

- Recovered manufacturer, display name, provider, and spool identity for
  printer loadout records by matching firmware aliases against the active
  SpoolManager, Spoolman, or built-in provider table.
- Displayed known manufacturers in loadout, top-bar tool, and inventory labels;
  printer-only materials remain correctly marked as unknown.
- Added a confirmed **Delete from Pi** action beside the firmware picker and
  clarified that printer-side BBFs are deleted through the USB storage browser.

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
