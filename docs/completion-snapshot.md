# OctoPod completion snapshot before lowering the bed

RME's host-only `@RME SNAPSHOT` marker captures the finished part before the
large accessibility Z move. Add it to slicer end G-code after the parking moves
and their `M400`, but before the final Z move and before turning lights off:

```gcode
P0 S1
G1 Y0 F10200
G1 X242 Y205 F10200
M400
@RME SNAPSHOT
G1 Z{min(max_layer_z + z_offset + 50, max_print_height)} F720
M400
; remaining heater/motor/light/timer cleanup
```

Only the snapshot marker is new; keep your machine's validated parking route
and Z limits. Re-slice existing jobs to include it. Do not place the marker in
OctoPrint's after-print script: by then the bed has already moved. This feature
does not infer capture points from arbitrary Z moves or alter the print file.

The marker is handled in OctoPrint's serialized send path. An acknowledged
M400 must precede it. A camera worker uses OctoPod's configured snapshot URL,
image resizing and orientation, without toggling lights. The writer waits at
most four seconds before continuing; receive processing remains live. Thus a
successful capture finishes before the following Z command can be sent. Camera
failure/timeout logs a warning and allows normal end G-code to continue.

The captured image replaces only OctoPod's job-notification image when the
printer is Finishing/Operational. No fake PrintDone event, premature progress,
extra push notification, or OctoPod configuration change is made. The normal
completion notification still occurs at actual job completion. Failed/canceled
jobs, disconnects and a new print clear the frame; reuse is capped at ten
minutes (including OctoPod's optional notification delay). A timed-out worker
cannot install its result later or create unlimited camera requests.

Compatibility uses OctoPod's `_job_notifications.image` instance interface;
missing/disabled/incompatible OctoPod installations fall back without capture.
No OctoPod source files are edited. Verified against the upstream interface:
[job notifications](https://github.com/gdombiak/OctoPrint-OctoPod/blob/master/octoprint_octopod/job_notifications.py),
[image acquisition](https://github.com/gdombiak/OctoPrint-OctoPod/blob/master/octoprint_octopod/base_notification.py).
Other OctoPod image consumers are unchanged. USB/local-printer jobs do not
stream this host marker through OctoPrint and are not supported by this path.

Host tests cover reuse, ordering, timeout, cancellation, expiry and fallback.
A supervised camera/bed-move test on the installed OctoPrint/OctoPod versions
is still required. The completion-snapshot feature was not present in b118.
