# Firmware changes required by the OctoPrint RME host

Validated against the on-disk `Prusa-Firmware-Buddy` checkout at
`0702267843` on 2026-08-12. This is an upstream handoff only; no firmware
source is modified by this repository.

## 1. Make the raw binary receiver self-recovering

Observed with the advertised `binary_chunk=1024 binary_window=8` path:

```text
RME_FILE_BINARY_ACK offset=16384
RME_FILE_BINARY_NACK offset=18432 reason=crc_mismatch
RME_FILE_BINARY_NACK offset=18432 reason=offset_mismatch
RME_FILE_BINARY_ACK offset=26624
RME_FILE_BINARY_NACK offset=27648 reason=chunk_too_large
<receiver stops responding, including to the binary abort frame>
```

The receiver currently trusts a corrupted header's 16-bit payload length and
sets `discard_remaining` to that value. A false large length can therefore
consume every later byte, including the framed abort, for an unbounded period.
The host cannot prove that line mode was restored and must require a physical
printer reboot.

Required behavior:

- Never enter a long blind discard based solely on an untrusted header length.
- Keep a rolling ten-byte header resynchronizer or cap malformed-frame discard
  to a small bounded amount, then look for the next valid frame header.
- Recognize the zero-length `offset=0xffffffff` abort frame even while
  recovering from an oversized/corrupt frame.
- Recognize the advertised `offset=0xfffffffe` control frame during recovery;
  `@RME FILE ABORT` must suspend the durable partial and restore line mode.
- Emit exactly one diagnostic NACK for the corrupt frame. Do not count or emit
  a separate retry failure for each later frame already in the old window.
- After any abort, disconnect, parser fault, or storage fault, release the raw
  receiver and shared transfer latch on every exit path.
- Add an inactivity watchdog that suspends the durable partial and restores
  line mode. Its terminal response should include the committed offset and
  `resumable=1`.
- Ensure the CDC receive path can actually sustain every advertised chunk and
  window. If 1024-byte payloads are not reliable, advertise the measured safe
  maximum (for example 512) rather than 1024.

Tests should inject byte loss, duplication, CRC corruption, a corrupted length
of 65535, disconnect mid-header, disconnect mid-payload, and abort during each
state. Every case must either resume at the last committed offset or return to
line mode without a reboot.

## 2. Report authoritative firmware candidate and bootloader stage state

A `.BBF` file being present on `/usb` is not evidence that it is staged. Prusa
Connect may leave ordinary old BBFs there. The host needs firmware-owned state:

```text
@RME FIRMWARE QUERY
RME_FIRMWARE candidate=0 armed=0
```

or, when applicable:

```text
RME_FIRMWARE candidate=1 candidate_path=FWUPD.RME size=<n> sha256=<hex> verified=1 armed=0
RME_FIRMWARE candidate=1 candidate_path=FWUPD.RME size=<n> sha256=<hex> verified=1 armed=1
```

Definitions:

- `candidate=1`: the protected, verified update candidate exists.
- `armed=1`: firmware has successfully handed that exact candidate to the
  bootloader. Merely listing a BBF must never set this bit.
- `RME_FIRMWARE_RESTART` should be emitted only when the reboot/bootloader
  handoff actually begins, not when FILE FLASH is merely queued.
- After reconnect, QUERY must report the durable truth and clear stale armed
  state when the bootloader did not accept or consume the candidate.

Add idempotent unstage support:

```text
@RME FIRMWARE UNSTAGE
RME_FIRMWARE_UNSTAGED candidate=0 armed=0
ok
```

UNSTAGE must refuse while printing/flashing, remove only the protected update
candidate and bootloader handoff marker, preserve unrelated BBFs, and be safe
when no candidate exists.

## 3. Preserve the shared transfer/print latch contract

The current `shared_transfer_latch=1` contract must cover all producers and
consumers, not only RME FILE commands:

- RME, Prusa Connect, PrusaLink, slicer, crash-dump, and local-UI transfers are
  mutually exclusive.
- A print cannot start while a firmware/file mutation owns the latch.
- A file or firmware transfer/mutation cannot start while any print is active.
- Connect downloads paused by a print retain their ownership and resume safely;
  another producer must receive `transfer_busy` rather than corrupting state.
- FLASH/PRINT queue acknowledgement must not release the latch until ownership
  has safely transferred to the relevant subsystem.
- Reboot, disconnect, abort, and all error paths must release or durably suspend
  ownership without leaving a phantom print/cancel state.

Expose enough state in SESSION or FILE STATUS for a host reconnect to
distinguish idle, transfer owner, printing, candidate verified, bootloader armed,
and reboot actually pending.

## 4. INDEX automatic pressure-advance calibration purge geometry

Pressure-advance measurement must not extrude against the silicone wiping
block. Back pressure from the block contaminates the calibration result.

- Position the nozzle over the purge bucket but in free air for every measured
  extrusion segment.
- Keep sufficient X/Y/Z clearance that the extrudate cannot bridge to or press
  against the silicone block during data capture.
- Perform the measurement while the strand falls freely into the bucket.
- Only after the measured segment completes, move through the silicone wiper
  to detach the strand and form a retained pellet.
- Return to the free-air measurement point before the next sample.
- Make the free-air point and wipe path machine-specific and collision checked.

Add a motion-sequence test (or logged integration assertion) proving that no
measurement extrusion occurs at the wipe-contact coordinates and that every
sample is followed by the pellet-forming wipe.

## 5. Protocol and regression coverage

- Keep the protocol document and `RME_FILE_CAPS` generated from the same
  constants so unsupported features cannot be advertised.
- Add end-to-end tests with a byte-stream transport, not only direct calls to
  `classify_binary_frame`.
- Verify a multi-megabyte BBF at the advertised baud, including NACK recovery,
  durable cross-transport resume, SHA-256 finalization, explicit stage query,
  unstage, flash handoff, and reconnect reporting.
- Verify ordinary `.BBF` files on `/usb` remain ordinary files and never appear
  as staged/armed firmware.
