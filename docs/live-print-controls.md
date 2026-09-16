# Live print controls

Firmware advertising `RME_MACHINE tune=1` enables an RME panel in OctoPrint's
Control tab, including during printing and pauses:

- Off / timed On / Locked-on chamber-light slider, following firmware state.
- Speed 10–300%, physical-slot flow 50–150%, and stealth mode.
- Effective logical-to-physical tool mapping, also displayed in the RME tab
  and printer Tune > Tool Mapping. Mapping is read-only during the job.

Requires the matching firmware implementation; older firmware keeps its
existing controls. Changes use the existing serial connection. Status is polled
every two seconds with one outstanding request; snapshots replace bounded
state rather than growing event history. Controls disable after 15 seconds
without a snapshot, during transfer/recovery, or when the printer is locked.
Mutations are limited to one per second. Queued changes can take time to reach
the firmware; displayed state is authoritative, not optimistic.

Timed On uses the firmware activity timeout. Locked stays on until changed,
session release, reboot or local brightness override. Persistent light profiles
are not modified. The navbar button toggles On/Off; use the slider for Locked.

Validation before deployment: exercise both screens during a serial print,
verify timeout and tool changes, reconnect, and run a heap/stack soak. Automated
queue-backpressure tests do not substitute for a physical-printer soak.
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
