# Firmware upstream handoff: authoritative update-stage status

The current firmware correctly distinguishes ordinary `.BBF` files from the
protected `/usb/FWUPD.RME` candidate and only creates `/usb/FWUPD.UI` while
`M997` arms the retained, one-shot bootloader selection. It currently reports
`RME_FILE_FLASH_QUEUED` and, immediately before USB detach,
`RME_FIRMWARE_RESTART reconnect=1`. It does not expose an idle query that tells
a host whether a firmware candidate or bootloader request is authoritative.

Do not infer staging from an arbitrary `.BBF` file. Prusa Connect may leave
valid firmware downloads on USB indefinitely, and those files are not selected
for installation.

## Required protocol addition

Add an idempotent query that reports the firmware application's durable view:

```text
@RME FIRMWARE QUERY
RME_FIRMWARE candidate=0 armed=0 state=idle
ok
```

When a verified protected candidate exists:

```text
RME_FIRMWARE candidate=1 armed=0 state=ready path=FWUPD.RME size=<bytes> sha256=<hex>
```

Once the retained bootloader request and cleanup marker have both been
persisted atomically:

```text
RME_FIRMWARE candidate=1 armed=1 state=restarting path=FWUPD.RME
RME_FIRMWARE_RESTART reconnect=1
```

`armed=1` must come from the retained bootloader selection/one-shot marker, not
from directory contents. On application startup after the attempted update,
the report must return `armed=0`; candidate cleanup can be reported as
`candidate=0` once it succeeds.

## Explicit unstage operation

Add an idle-only, transfer-latched operation:

```text
@RME FIRMWARE UNSTAGE
RME_FIRMWARE_UNSTAGED
ok
```

It must reject `armed=1`, printing, and any active RME/Connect/Link transfer.
Otherwise it removes only the protected candidate and its private upload
partials/metadata. It must not delete ordinary `.BBF` downloads. Repeating the
command when no candidate exists should succeed idempotently.

Until this query exists, the OctoPrint plugin keeps stage provenance only for a
candidate it has just uploaded and verified. It validates that provenance
against `FWUPD.RME`, but never promotes an arbitrary USB file into staged
state. A later live `RME_SESSION ... printer_state=IDLE` also clears a stale
restart claim when USB never detached.
