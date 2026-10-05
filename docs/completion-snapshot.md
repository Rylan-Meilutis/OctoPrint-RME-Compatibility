# OctoPod completion snapshot before lowering the bed

RME automatically inserts a capture barrier for recognizable end-of-print bed
lowering in local text jobs. No slicer change or re-slicing is needed for the
standard sequence below. At preflight it finds a final absolute Z-only move
of at least 10 mm after extrusion, nozzle/bed heater-off commands and P0 tool
parking, with only ordinary shutdown/status commands afterward. At that exact
file byte position it queues M400, the host-only `@RME SNAPSHOT` marker, then
the original move. It also checks that the streamed command still matches.

Travel hops, relative Z, later extrusion/motion, unknown macros/transforms,
missing parking/shutdown, analysis over 5 seconds/100 MiB, and remote/binary
jobs are not guessed. Missing file-position tags or commands rewritten by
other plugins also skip automatic capture. Analysis runs only with OctoPod
enabled and an RME-compatible printer. Original file contents are unchanged.

For an ambiguous/custom sequence, the explicit marker remains available.
Add it after parking and M400, before the final Z move and lights-off:

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

Keep your machine's validated parking route and Z limits. Explicit-marker jobs
disable automatic insertion to avoid duplicate capture. Do not place the
marker in OctoPrint's after-print script: by then the bed has already moved.

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

Host tests cover end-sequence detection, byte offsets, exact-position dispatch,
reuse, ordering, timeout, cancellation, expiry and fallback.
A supervised camera/bed-move test on the installed OctoPrint/OctoPod versions
is still required. The completion-snapshot feature was not present in b118.
