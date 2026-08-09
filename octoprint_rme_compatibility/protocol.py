"""Parsing and command construction for the Prusa RME serial extensions."""

import base64
import re
import shlex


MAX_FIRMWARE_SIZE = 32 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 48
WORKFLOWS = {
    "mmu",
    "filament_load",
    "filament_unload",
    "tool_change",
    "filament_runout",
    "stuck_filament",
    "pressure_advance",
    "probing",
    "heating",
    "firmware_update",
    "waste_bin",
    "chamber_vent",
    "filtration",
    "printer",
}
TERMINAL_WORKFLOW_STATES = {
    "canceled",
    "cancelled",
    "closed",
    "complete",
    "completed",
    "idle",
    "skipped",
    "stopped",
}

# M865 is an existing Buddy diagnostic command.  The quoted fields deliberately
# use a narrow parser: accepting a partial match here could select the wrong
# physical spool after a malformed serial line.
LOADED_FILAMENT_RE = re.compile(
    r'^loaded_filament T(?P<tool>\d+) S"(?P<material>[^"]*)" '
    r'O"(?P<color_name>[^"]*)" H"(?P<color>[^"]*)"$'
)


def _scalar(value):
    try:
        if re.match(r"^-?\d+$", value):
            return int(value)
        if re.match(r"^-?(?:\d+\.\d*|\d*\.\d+)$", value):
            return float(value)
    except (TypeError, ValueError):
        pass
    return value


def parse_fields(text):
    """Parse space separated key=value fields, including quoted values."""
    fields = {}
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = _scalar(value)
    return fields


def parse_line(raw_line):
    """Return a structured RME/FW upload record or ``None``."""
    line = raw_line.strip()
    if line.startswith("RME_EVENT "):
        result = parse_fields(line[len("RME_EVENT ") :])
        result["record"] = "event"
        return result
    for prefix, record in (
        ("RME_MACHINE ", "machine"),
        ("RME_ENVELOPE ", "envelope"),
        ("RME_LIMITS ", "limits"),
        ("RME_SESSION ", "session"),
        ("RME_LOCK ", "lock"),
        ("RME_THEME ", "theme"),
        ("RME_LIGHT ", "light"),
        ("RME_FILAMENT ", "filament"),
        ("RME_STATS ", "stats"),
        ("RME_STATS_OPERATIONS ", "stats"),
        ("RME_STATS_FAILURES ", "stats"),
    ):
        if line.startswith(prefix):
            result = parse_fields(line[len(prefix) :])
            result["record"] = record
            return result
    if line.startswith("RME_PROMPT "):
        actions = line[len("RME_PROMPT ") :].strip()
        return {
            "record": "prompt",
            "actions": [] if actions == "none" else [x for x in actions.split(",") if x],
        }
    if line.startswith("RME_TOOLMAP "):
        tokens = line[len("RME_TOOLMAP ") :].split()
        mapping = {}
        for token in tokens[1:]:
            match = re.match(r"L(\d+)=(-?\d+)$", token)
            if match:
                mapping[int(match.group(1))] = int(match.group(2))
        return {
            "record": "toolmap",
            "enabled": bool(int(tokens[0])) if tokens and tokens[0] in ("0", "1") else False,
            "mapping": mapping,
        }
    loaded_filament = LOADED_FILAMENT_RE.match(line)
    if loaded_filament:
        result = loaded_filament.groupdict()
        result["record"] = "loaded_filament"
        result["tool"] = int(result["tool"])
        return result
    if line.startswith("echo:RME_ERROR"):
        return {"record": "rme_error", "message": line}
    if line.startswith("FW_UPLOAD READY"):
        fields = parse_fields(line[len("FW_UPLOAD READY") :])
        return {"record": "upload_ready", "chunk": int(fields.get("chunk", DEFAULT_CHUNK_SIZE))}
    if line.startswith("FW_UPLOAD OFFSET "):
        try:
            return {"record": "upload_offset", "offset": int(line.rsplit(" ", 1)[1])}
        except ValueError:
            return {"record": "upload_error", "message": line}
    if line.startswith("FW_UPLOAD COMPLETE "):
        return {"record": "upload_complete", "path": line[len("FW_UPLOAD COMPLETE ") :]}
    if line.startswith("FW_UPLOAD ABORTED"):
        return {"record": "upload_aborted"}
    upload_error = re.match(r"(?:Error:\s*)?FW_UPLOAD\s+(.+)$", line)
    if upload_error:
        return {"record": "upload_error", "message": upload_error.group(1)}
    return None


def workflow_is_terminal(record):
    """Return whether a workflow record means its remote UI can be dismissed.

    Firmware is authoritative for prompt lifetime.  In particular, this means
    an action completed on the printer LCD closes the matching OctoPrint prompt
    without requiring the browser that originally displayed it to remain open.
    """
    state = str(record.get("state", "")).lower()
    if state in TERMINAL_WORKFLOW_STATES:
        return True
    # A chamber vent reports its final physical position rather than a generic
    # ``closed`` workflow state.  A completed open operation must therefore
    # dismiss just like a completed close operation.
    try:
        progress = float(record.get("progress", 0))
    except (TypeError, ValueError):
        progress = 0
    return record.get("workflow") == "chamber_vent" and state == "open" and progress >= 100


def classify_workflow(record):
    """Refine generic firmware events into stable OctoPrint workflow groups.

    Newer firmware can send these names directly. Older RME builds classify
    less common status messages as ``printer``; matching only that generic
    fallback preserves authoritative MMU/error routing while improving remote
    presentation while keeping the firmware's documented routing keys intact.
    """
    workflow = str(record.get("workflow", "printer"))
    if workflow != "printer":
        return workflow
    message = str(record.get("message", "")).lower()
    # Mirror SerialPrinting::classify_workflow in the current Buddy RME tree.
    # This fallback only handles old/generic records; dedicated workflow names
    # from firmware pass through unchanged above.
    rules = (
        ("mmu", ("mmu",)),
        ("filament_load", ("loading filament",)),
        ("filament_unload", ("unloading filament",)),
        ("chamber_vent", ("vent",)),
        ("filtration", ("filter", "filtration")),
        ("tool_change", ("tool change",)),
        ("filament_runout", ("filament runout",)),
        ("stuck_filament", ("stuck",)),
        ("pressure_advance", ("pressure", "pa calibration")),
        ("probing", ("probing", "probe")),
        ("heating", ("heating", "heat soaking")),
        ("firmware_update", ("firmware",)),
        ("waste_bin", ("bucket", "waste")),
    )
    for candidate, phrases in rules:
        if any(phrase in message for phrase in phrases):
            return candidate
    return workflow


def chunk_command(offset, payload):
    encoded = base64.b64encode(payload).decode("ascii")
    return "M998 P1 O%d D%s" % (offset, encoded)


def dialog_response_command(action):
    if not isinstance(action, str) or not action or len(action) > 64:
        raise ValueError("Invalid dialog action")
    if any(character in action for character in '\"\r\n'):
        raise ValueError("Invalid dialog action")
    return '@RME DIALOG RESPOND A"%s"' % action


def toolmap_commands(mapping, enabled=True):
    normalized = []
    seen_physical = set()
    for logical, physical in sorted(mapping.items(), key=lambda item: int(item[0])):
        logical, physical = int(logical), int(physical)
        if logical < 0 or physical < 0 or logical > 255 or physical > 255:
            raise ValueError("Tool indices must be between 0 and 255")
        if physical in seen_physical:
            raise ValueError("Each physical tool may only be mapped once")
        seen_physical.add(physical)
        normalized.append("@RME TOOLMAP SET logical=%d physical=%d" % (logical, physical))
    normalized.append("@RME TOOLMAP ENABLE value=%d" % (1 if enabled else 0))
    normalized.append("@RME TOOLMAP QUERY")
    return normalized
