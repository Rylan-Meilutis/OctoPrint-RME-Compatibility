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
