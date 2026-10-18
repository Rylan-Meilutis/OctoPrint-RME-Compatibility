# Browser performance validation

The headless Chromium fixture mounts the real RME workspace, saved Settings,
native SpoolManager integration panel and full RME filament chooser, using
OctoPrint's jQuery, Knockout, Bootstrap and PNotify. It supplies eight PLA tools
and 1,000 inventory spools, then sends 60 full state updates 30 ms apart while
an action-required notification is visible. The event ID and provider status
change each time. It records a Chrome CPU profile, DOM mutations, element
count, event-loop heartbeat, Settings opening and long tasks.

This is a local integration fixture, not a profile of the user's installed
OctoPrint or its other plugins. It did not reproduce a 15-second freeze.
It does reproduce unnecessary Knockout array work, popup animation churn and
an excessively large chooser. Measurements vary by host.

## Results (2026-10-17)

| Metric | Released b126 (`8561476`) | Fixed working tree |
| --- | ---: | ---: |
| Elements, full chooser | 24,839 | 1,411 |
| Total JavaScript in 60 updates | 362 ms | 35 ms |
| Worst update | 20 ms | 3.9 ms |
| Settings opening, next frame | 15 ms | 6.4 ms |
| DOM mutation records | 60,015 | 376 |
| Notifications remaining at end | 31 | 1 |

With Chrome CPU throttling set to 4x:

| Metric | Released b126 | Fixed working tree |
| --- | ---: | ---: |
| Total JavaScript in 60 updates | 1,443 ms | 148 ms |
| Worst update | 52 ms | 16 ms |
| Largest event-loop heartbeat gap | 213 ms | 35 ms |
| Settings opening, next frame | 55 ms | 14 ms |
| Long tasks in measured window | 23 | 0 |

Profiles identify Knockout array reconciliation, jQuery style/layout operations
and garbage collection as prominent costs in the released fixture. The fixed
fixture spends most sampled time idle. Initial chooser creation also produced
a roughly 270 ms long task with the unpaged 1,000-spool inventory.

## Changes

- Preserve selector identity so unrelated state fields cannot invalidate the
  complete inventory and every tool's material-matching list.
- Render 30 matching spools per tool, with full-inventory search and Show more.
  Keep the selected-spool summary visible even if it is outside the first page.
- Keep one action notice per distinct message/actions, independent of sequence
  numbers and timestamps. Avoid notification animation churn.
- Avoid writing unchanged sidebar attributes/classes, reducing feedback with
  DOM observers in other integrations.

## Reproduce

Install Playwright in a temporary dependency directory and its Chromium browser.
Set `OCTOPRINT_STATIC` to an OctoPrint checkout's `src/octoprint/static` folder.
Then, from this repository:

```sh
NODE_PATH=/tmp/rme-browser-profile/node_modules node tests/browser/profile_rme.js
NODE_PATH=/tmp/rme-browser-profile/node_modules node tests/browser/profile_rme.js 8561476
RME_PROFILE_CPU_RATE=4 NODE_PATH=/tmp/rme-browser-profile/node_modules node tests/browser/profile_rme.js
```

The script writes `.cpuprofile` and JSON metrics under `/tmp/rme-browser-*`.
CPU profiles can be loaded in Chromium DevTools Performance. The regular UI
regressions additionally verify searching the last spool, Show more, no
inventory-choice notifications for unrelated telemetry, real inventory updates,
and notice reuse. They do not enforce machine-dependent timing thresholds.
