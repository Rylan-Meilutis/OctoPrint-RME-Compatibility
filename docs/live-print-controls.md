# Live print controls

## Resume handoff

Firmware remains responsible for reheating, priming and returning to the print
position. RME does not advance the print during `pause_phase=resuming` alone.
Only the physical `//action:resumed` completion or an authoritative
`printer_state=PRINTING pause_phase=none` snapshot permits host reconciliation.

After confirmation, the plugin advances the normal OctoPrint queue toward its
resume-completion marker immediately. Suppressed host resume macros cannot
strand that marker waiting for the next session poll or an unrelated ACK.
Real queued commands still use core's normal ACK path, and foreign job holds
remain owned by their original plugin. Deferred work expires if the connection,
selected job or operator-control revision changes. No ACKs are fabricated and
the plugin does not force OctoPrint into Printing.

During Resuming, periodic non-job M117/M73 display/ETA updates are suppressed
until normal printing resumes. File commands, explicit API/terminal commands
and recovery controls are preserved. This changes the host handoff only, not
firmware thermal readiness, purge/prime policy or motion safety.

Code-less printer status notifications such as paused/resuming “Retracting
filament” are snapshots, not persistent workflows: they expire 15 seconds after
the last update and clear immediately on confirmed resume. The existing
keepalive worker also retires expired snapshots from API state, without sending
additional serial traffic. Explicit workflows, coded faults, runout prompts and
pressure-advance calibration retain their own completion/recovery lifecycle.

Validation includes real OctoPrint 2.0.1, 2.0.0 and 1.11.8 integration paths,
status backlogs, long scripts, real-command ACK boundaries, counted foreign
holds, stale callbacks and repeated pause/resume cycles. Physical printer
timing still needs confirmation after installing the update while idle.

## Operator controls

Optional live-control capabilities are reset on startup and on every new machine
snapshot; persisted flags are not proof of support on the installed firmware.
Translation requires an active RME lease too. Otherwise M104/M140/M141 and fan
commands stay on the native queue rather than becoming unsupported services.

Firmware advertising `live_dock_fan=1` supports `CONTROL SET dock_fan=0..255`.
Explicit M106/M107 P6 and the RME dock-fan control use that priority path during
waits. The printer's mandatory hot-tool cooldown can keep actual PWM higher
than requested. Older firmware retains native queued M106 P6 behavior.

Updated firmware lets explicit print-fan settings, including zero and values
below 30%, override chamber heating's default circulation during that assist
cycle. Heatbreak cooling and heater protections are independent and unchanged.

Firmware advertising live_controls=1 accepts @RME CONTROL SET with exactly one
of bed=N, nozzle=N, fan=N, speed=N or flow=N. Nozzle and flow support tool=N
as a direct slot; the plugin resolves logical API tool selections before sending.
Nozzle defaults to the mounted tool when omitted; no tool is ever picked by
this service. Native heater limits and thermal protections remain authoritative.
Fan is the print fan (0–255), speed is 10–300%, flow is 50–150%.
These operator changes bypass waits; file-derived G-code remains FIFO.
The touchscreen's non-motion variable requests have a separate bounded mailbox,
serviced during nested waits. Motion, calibration and tool-change requests do
not gain recursive execution privileges.

Firmware with live_chamber=1 accepts operator/API chamber changes through
@RME TEMPERATURE SET chamber=N while a heater wait is active. Zero switches
chamber control off. Targets remain clamped by firmware capabilities. File
M141 commands keep FIFO order, and older firmware retains the queued path.
Lighting uses the existing out-of-band service; firmware continues polling
its bounded serial recovery reserve even when the normal G-code FIFO is full.
Unknown pause causes do not occupy the pause banner; diagnostic state remains.

Firmware advertising `RME_MACHINE tune=1` enables an RME panel in OctoPrint's
Control tab, including during printing and pauses:

- Off / On chamber-light slider during printing; timed On / Locked-on are
  available only when the firmware is idle.
- Independent LCD and status brightness controls on matching firmware.
- Speed 10–300%, physical-slot flow 50–150%, and stealth mode.
- Effective logical-to-physical tool mapping, also displayed in the RME tab
  and printer Tune > Tool Mapping. Mapping is read-only during the job.

Requires the matching firmware implementation; older firmware keeps its
existing controls. Changes use the existing serial connection. Status is polled
every ten seconds with one outstanding request; snapshots replace bounded
state rather than growing event history. Controls disable after 15 seconds
without a snapshot, during transfer/recovery, or when the printer is locked.
Mutations are limited to one per second. Queued changes can take time to reach
the firmware. Lighting changes use bounded optimistic feedback, then synchronize
with authoritative firmware state. Explicit refreshes after controls bypass
the background polling interval.

Timed On uses the firmware activity timeout. Locked stays on until changed,
session release, reboot or local brightness override. Persistent light profiles
are not modified. The navbar button toggles On/Off; use the slider for Locked.

Validation before deployment: exercise both screens during a serial print,
verify timeout and tool changes, reconnect, and run a heap/stack soak. Automated
queue-backpressure tests do not substitute for a physical-printer soak.

## Safe host-side object cancellation

The RME object preview integrates with the compatible Cancel Object plugin for
local text G-code. Cancellation checks the actual streaming file, print-start
identity and byte position, not percent, height or elapsed time. Each object's
last extrusion across the entire file is indexed: a per-layer stop is not a
completion marker. Cancellation is refused once the final extrusion has been
queued, even if the printer is still executing buffered moves. The RME API
also rejects a stale selection from an earlier print of the same file.

Enable slicer object labeling. Use a stable unique name for each instance/copy:

```gcode
@Object boat id:0 copy 0
; only this object's moves
@Objectstop boat id:0 copy 0
; entire shared tool-change, purge, prime-tower and priming sequence here
T1
; shared extrusion moves
@Object other id:1 copy 0
; only this object's moves
@Objectstop other id:1 copy 0
```

Prusa `; printing object ...` / `; stop printing object ...` comments are also
recognized. Use absolute XYZ positioning. Tool changes and the *entire* shared
priming/tower sequence must be outside object spans, including the last stop
before end G-code. Missing/ambiguous boundaries, relative XYZ object moves, or
tool/tower blocks inside a span disable cancellation rather than guessing.
Non-motion commands (including tool selection, heaters, extrusion mode and
coordinate resets) remain in the stream. Explicit object-stop labels end the
host skip even when the external plugin's optional stop-tag setting is off.
The adapter also validates cancellation through that plugin's API/helper.

These completion checks apply to local OctoPrint streaming, not native USB/SD
printing or unlabeled/binary files. Native firmware M486 cancellation is a
separate path. Regenerate unlabeled G-code in the slicer; the plugin does not
rewrite user files to invent object boundaries.

## Pause causes and progress telemetry (b123)

The reported pause cause appears above the main progress bar and in RME only
while paused. Resume hides it; completion clears it. Genuine causes remain in
job-local diagnostic state until completion and are logged. Routine tool-change,
heating and probing text is not a cause. If no cause is reported, the banner
stays hidden and the unreported pause remains in diagnostic state.

Changed host progress estimates are sent at most every five seconds; unchanged
estimates use a twenty-second heartbeat within the firmware's sixty-second
validity window. Pause-bit changes bypass the rate limit, but still respect the
one-outstanding-frame guard. Control polling skips startup and recovery states.
These service queries do not request motion synchronization. Reduced traffic
has not yet been confirmed to resolve intermittent motion stalls on hardware.

## INDX temperature controls

INDX discovery can report `hotends=8 tool_capacity=8` (current firmware) or
`hotends=1 tool_capacity=8` (older firmware). Both describe passive tool slots.
Do not reduce OctoPrint's internal tool array: temperature updates index it by
physical slot. Filter only the Temperature/Dashboard presentation collections.

Only the firmware-selected physical tool with a valid reading may be heated.
Use parameterless `M104 S...` for that head to avoid applying logical tool
mapping a second time. Bed uses OctoPrint's normal target API; chamber uses
`M141 S...`, also allowing older profiles to control it. Profile synchronization
enables heated bed/chamber capabilities. Firmware limits and thermal protections
remain authoritative. No additional polling is introduced.

Chamber control retains the firmware's bed-target-preserving heating assistance
and automatic fan cooling. It is not a separate heater and cannot guarantee a
target above the heat available from the configured bed temperature. Do not
override the bed target or print cooling to force chamber convergence.

Validation: Python suite (208 tests, four skipped), passive-tool unit tests,
Temperature/Dashboard Knockout DOM and submit tests, and mapping/control DOM
tests pass. Physical verification is still required: mounted/parked transitions,
tool-mapped heating, bed target changes, and chamber heating/cooling targets.
