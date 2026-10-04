# File-derived adaptive meshing

Requires RME Compatibility 0.1.0b118 and firmware advertising `mesh_area=1` in
`@RME MACHINE QUERY` (October 4 builds of 6.9.0-RME / 6.10.1-RME, except MINI).

At the synchronous before-print boundary, RME scans the selected local .gcode
or .gco file once. It tracks absolute/relative XY and E, E resets, tool changes,
and positive-extrusion moves. All tools, skirts/brims and towers contribute;
pure travel and stationary purge do not. I/J arcs use a conservative full-circle
envelope. It adds a 1 mm line-width allowance and inserts this immediately before
ordinary file `G29 P1` commands:

```gcode
@RME MESH SET x=50.00 y=40.00 width=80.00 height=60.00
G29 P1
```

Firmware stages the area and consumes it at the next P1; ongoing probing is not
changed. It retains the native grid, bed limits and probe margin. End/cancel
cleanup clears pending bounds. This is an arbitrary XY rectangle, not a new
variable-spacing mesh algorithm. Print files and extrusion moves are untouched.

Explicit G29 XY/size or C extension passes are not rewritten. Missing firmware
capability, remote/binary files, unknown coordinates/macros, inch/workspace
transforms, R-format arcs and off-bed extrusion use the existing slicer behavior.
Analysis is limited to 5 seconds and 100 MiB; failure is logged, never a reason
to cancel the print. `RME adaptive mesh` in octoprint.log reports the decision.

The command requires an unlocked, active RME serial job. Four finite in-bed
values are required and width/height must be positive. Success reports
`RME_MESH accepted=1`. No startup priority traffic is injected before M110.

Check the first small test print's probe coverage and use the normal first-layer
checks. Third-party plugins that transform geometry after preflight are not
accounted for; use the slicer's explicitly sized probe when using such transforms.
Host tests validate analysis and dispatch, not physical hardware behavior.
