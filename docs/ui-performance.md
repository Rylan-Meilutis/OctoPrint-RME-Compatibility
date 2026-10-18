# Browser performance validation

## Restored OctoPrint Settings stall (follow-up)

The original small fixture did not exercise the full installed Settings
lifecycle. A later test used a real isolated OctoPrint 1.11.8 server with the
backup's 23-spool SpoolManager database, saved RME state and UI Customizer 0.1.10.0
Cyborg configuration. SpoolManager 1.7.8, Dashboard 1.19.13, PrintTimeGenius 2.5.1,
and the additional plugins with Settings-opening hooks (Camera Settings,
Continuous Print, OctoPod, PrettyGCode, Firmware Updater and Nozzle Filament
Validator) were loaded. Only test credentials were used; printer autoconnect
was disabled and no saved credentials, network tokens or scripts were restored.

Opening Settings invokes PrinterProfilesViewModel.requestData. Its response
publishes a fresh mapped currentProfileData object even when every value is
unchanged. SpoolManager subscribes to that object and rebuilds sidebar rows.
Creating those read-only rows repeatedly writes the shared Quill note editor;
the profile showed expensive Quill updates and forced style/visibility reads.
This occurs after the synchronous Settings show handlers, so timing only those
handlers misses most of the freeze.

At 4x Chrome CPU throttling, the unmodified opening caused a 5,844 ms long task
and a 5,912 ms event-loop gap. With the integrated RME asset (no injected test
workaround), the largest gap was 563 ms and the long tasks were 417 and 401 ms.
Settings show returned in 416 ms; UI Customizer's hook accounted for 206 ms.
The multi-second rebuild disappeared. This reproduces a substantial stall in
the restored setup, not an exact measurement of the user's 10–15-second stall
on their particular browser/device.

A final same-server A/B run (restoring only the native comparer in the
disposable baseline browser) reproduced 5,661 ms versus 565 ms maximum gaps.
The separate CPU profiles and metrics are preserved as
`/tmp/rme-real-settings-cpu4-baseline.*` and
`/tmp/rme-real-settings-cpu4-fixed.*`. Both runs retain the fixture's expected
blank-camera errors; no profile-serialization or new JavaScript errors occur.

The fix compares complete profile values using ko.mapping.toJS, not ko.toJS
(the latter leaves circular mapping bookkeeping in OctoPrint 1.11.x). It skips
identical notifications but retains real changes, explicit valueHasMutated(),
and native fallback for unexpected extensions. It does not modify SpoolManager,
skip its actual settings hooks, or suppress changes to tools, offsets or limits.

`tests/browser/profile_octoprint_settings.js` records a real server's modal
opening, per-view-model hook times, slow local requests, long tasks and CPU
profile. It intentionally targets only the test server on 127.0.0.1:5517 and
uses the fixture-only account `rmeprofile` with password `profile-local-only`.
Create that account in the temporary basedir with OctoPrint's `user add` CLI.
`tests/browser/prepare_octoprint_fixture.py BACKUP.zip` creates a private fresh
temporary directory containing only whitelisted settings and data. Never run
this fixture on a real printer server or expose its loopback service remotely.
The intentionally blank camera URL produces a known webcam error in the
fixture; no camera is connected or streamed.

```sh
RME_PROFILE_CPU_RATE=4 NODE_PATH=/tmp/rme-browser-profile/node_modules node tests/browser/profile_octoprint_settings.js
RME_PROFILE_BASELINE=1 RME_PROFILE_CPU_RATE=4 NODE_PATH=/tmp/rme-browser-profile/node_modules node tests/browser/profile_octoprint_settings.js
```

The profile and JSON metrics are written under `/tmp/rme-real-settings*`.
The unit regressions additionally need the `knockout-mapping` npm package.

## Earlier RME-only fixture

The headless Chromium fixture mounts the real RME workspace, saved Settings,
native SpoolManager integration panel and full RME filament chooser, using
OctoPrint's jQuery, Knockout, Bootstrap and PNotify. It supplies eight PLA tools
and 1,000 inventory spools, then sends 60 full state updates 30 ms apart while
an action-required notification is visible. The event ID and provider status
change each time. It records a Chrome CPU profile, DOM mutations, element
count, event-loop heartbeat, Settings opening and long tasks.

This earlier test is a local integration fixture, not a profile of the user's installed
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

The script now uses the actual Bootstrap modal manager and supports the optional
`RME_PROFILE_BACKUP` archive and `RME_PROFILE_UI_CUSTOMIZER` source directory;
it can include whitelisted saved RME state and UI settings without restoring
credentials. These optional hooks are still not a substitute for the real-server
test described above. The original table measures only showing the Settings
content, not the full modal lifecycle.

The script writes `.cpuprofile` and JSON metrics under `/tmp/rme-browser-modal-*`.
CPU profiles can be loaded in Chromium DevTools Performance. The regular UI
regressions additionally verify searching the last spool, Show more, no
inventory-choice notifications for unrelated telemetry, real inventory updates,
and notice reuse. They do not enforce machine-dependent timing thresholds.
