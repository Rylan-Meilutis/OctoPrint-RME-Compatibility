# Upstream firmware handoff: shared RME/Connect transfer latch and INDX PA isolation

This file describes firmware-side changes that must be applied to the current
upstream Prusa-Firmware-Buddy tree. They were intentionally **not** retained in
the older local firmware checkout. The OctoPrint plugin side recognizes the
`transfer_busy` and `printer_busy` protocol responses and waits without failing
or arming raw mode.

## Required ownership rules

Use `transfers::Monitor::instance` as the single storage-transfer latch. Do not
introduce an independent RME busy boolean.

1. An RME write (`WRITE_BEGIN`, `WRITE_BULK_BEGIN`, or
   `WRITE_BINARY_BEGIN`) must allocate and retain a `Monitor::Slot` before
   opening a partial file.
2. If a Connect/Link transfer owns the slot, reply:
   `echo:RME_ERROR workflow=file code=transfer_busy` and make no storage or raw
   transport state change.
3. Retain the RME slot until verified completion or abort/error cleanup. Report
   byte progress through the slot for both text/bulk chunks and binary frames.
4. Release it with `Monitor::Outcome::Finished` only after SHA-256 verification
   and atomic publication. All other exits release it as an error/stopped
   outcome and remove the partial file as appropriate.
5. RME `PRINT` and `FLASH` must reject with `transfer_busy` while any monitor
   slot exists. Connect `StartPrint` must likewise reject while a transfer slot
   exists.
6. RME reads and storage mutations (`READ`, `READ_BINARY`, `DELETE`, `RENAME`,
   and `MKDIR`) must not run while a storage transfer owns the slot. Writes and
   mutations also remain guarded by `marlin_server::printer_idle()`.
7. Link/Slicer HTTP upload start must return HTTP Conflict while printing.
8. A Connect download that observes an active print must keep its monitor slot,
   close/pause its network download, preserve its partial-file state, and resume
   only after printing ends. Keeping the slot prevents RME/Link from overtaking
   the paused Connect job.

### `src/marlin_stubs/rme_file_service.cpp`

Include `transfers/monitor.hpp` and retain a move-only slot alongside the
existing upload state:

```cpp
std::optional<transfers::Monitor::Slot> upload_slot;
```

At write-begin, perform checks in this order:

```cpp
if (upload.file) report_error("upload_state");
else if (!marlin_server::printer_idle()) report_error("printer_busy");
else if (!size || *size > maximum_file_size || !sha) report_error("invalid_upload");
else {
    auto slot = transfers::Monitor::instance.allocate(
        transfers::Monitor::Type::Link, path->data(), *size);
    if (!slot) {
        report_error("transfer_busy");
        return true;
    }
    upload_slot.emplace(std::move(*slot));
    // Only now initialize/open the RME upload.
}
```

On every successfully committed payload:

```cpp
upload_slot->progress(committed_payload_size);
```

Extend upload reset/finalization so the slot is completed and destroyed on
every path. Take care not to call `done()` twice:

```cpp
void reset_upload(bool remove_partial,
    transfers::Monitor::Outcome outcome = transfers::Monitor::Outcome::ErrorOther) {
    // close/free/remove existing upload state
    if (upload_slot) {
        upload_slot->done(outcome);
        upload_slot.reset();
    }
    // clear state and UI transfer indication
}
```

Use `Outcome::Finished` only on verified text/bulk and binary completion.
ABORT, CRC/write/hash/finalize errors, disconnect cleanup, and parser recovery
must all release the slot.

Before RME `READ`, `READ_BINARY`, storage mutations, `PRINT`, or `FLASH`:

```cpp
if (transfers::Monitor::instance.id().has_value())
    report_error("transfer_busy");
```

`PRINT`/`FLASH` must also retain the existing `printer_idle()` and validation
checks. Do not acquire a slot for `PRINT`; reject until the transfer that
created the file has fully released its slot, then queue the print/flash.

## Connect and Link integration

### `src/connect/planner.cpp`

Before `StartPrint` calls `printer.start_print(...)`, reject when
`Monitor::instance.id()` is present with machine reason
`TransferInProgress`. This includes a paused Connect download and an active RME
upload.

### `src/transfers/transfer.cpp`

At the start of `Transfer::step(bool is_printing)`, when printing is true and
state is `Downloading` or `Retrying`:

- force-update the transfer backup;
- destroy/reset the active network `download` object;
- retain the `Monitor::Slot` and partial file;
- mark progress as having no network issue;
- delay restart and return the current state.

The existing retry path should recreate the download after printing becomes
false. This is a pause, not a failed transfer, and must not consume network
retries.

### `lib/WUI/nhttp/gcode_upload.cpp`

Before allocating a Link upload slot, reject with HTTP Conflict when
`marlin_client::is_printing()` is true. The existing monitor allocation remains
the symmetric guard against Connect or RME transfers.

## INDX automatic PA calibration: free air over bucket

Measured excitation must occur over the purge bucket at the calibrated purge
parking point, with a positive air gap from the silicone cleaner. Silicone is
used only between cycles to wipe/eject the completed pellet.

### `src/feature/extrusion_calibration.hpp/.cpp`

Add capture-preserving `pause()` and `resume()` methods. `pause()` disables
sample collection without clearing the count; `resume()` re-enables it only if
the fixed capture buffer has not overflowed. Add a unit test proving samples
during a paused cleaner wipe are excluded and samples afterward append.

### `src/marlin_stubs/M976.cpp`

For each INDX excitation cycle:

1. Pause capture and disable calibration mode.
2. Park at `mapi::get_parking_position(ParkPosition::purge)`. This is the
   calibrated free-air position over the bucket, not the silicone path.
3. Synchronize motion, resume capture, and enable calibration mode.
4. Run fast then slow extrusion in free air and synchronize.
5. Disable calibration mode and pause capture.
6. Run `nozzle_cleaner::Sequence::eject_blob` to wipe the pellet into the
   bucket and account for the pellet.
7. The next cycle explicitly returns to the purge free-air point before
   capture resumes.

Never record cleaner contact in the PA loadcell trace. Never extrude a measured
fast/slow segment while the nozzle is touching or pressing into silicone,
because cleaner back-pressure biases the PA result.

## Regression coverage

Add firmware tests or integration assertions for:

- Connect transfer prevents RME write-begin and leaves RME/raw state untouched.
- RME upload prevents Connect and Link slot allocation.
- Abort, binary abort, write error, hash error, and success each release the
  monitor slot exactly once.
- Binary and bulk progress updates the shared monitor.
- RME/Connect/Link print starts are rejected while any transfer is active.
- Connect transfer pauses (slot retained, no download traffic) during a print
  and resumes afterward without losing partial progress.
- Link upload is rejected during a print.
- INDX PA capture contains excitation samples but no samples from the silicone
  wipe; every measured cycle starts at the purge free-air pose over the bucket.

