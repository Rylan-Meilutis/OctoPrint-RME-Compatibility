"""Parsing and command construction for the Prusa RME serial extensions."""

import base64
import re
import shlex
from urllib.parse import unquote


MAX_FIRMWARE_SIZE = 32 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 48
WORKFLOWS = {
    "mmu",
    "filament_load",
    "filament_unload",
    "tool_change",
    "filament_runout",
    "filament_movement",
    "extrusion_flow_limit",
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
    r'(?:P"(?P<profile>[^"]*)" )?'
    r'O"(?P<color_name>[^"]*)" H"(?P<color>[^"]*)"'
    r'(?: M"(?P<vendor>[^"]*)")?$'
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
    # File names may contain spaces, so the generic whitespace field parser is
    # intentionally not used for records whose path/name field is unquoted.
    file_entry = re.match(r"^RME_FILE_ENTRY name=(.*?) type=(dir|file) size=(\d+)$", line)
    if file_entry:
        return {
            "record": "file_entry", "name": file_entry.group(1),
            "type": file_entry.group(2), "size": int(file_entry.group(3)),
        }
    file_stat = re.match(
        r"^RME_FILE_STAT path=(.*?) type=(dir|file) size=(\d+) mtime=(\d+)$", line
    )
    if file_stat:
        return {
            "record": "file_stat", "path": file_stat.group(1),
            "type": file_stat.group(2), "size": int(file_stat.group(3)),
            "mtime": int(file_stat.group(4)),
        }
    file_data = re.match(
        r"^RME_FILE_DATA path=(.*?) offset=(\d+) length=(\d+) eof=([01]) data=(.*)$",
        line,
    )
    if file_data:
        return {
            "record": "file_data", "path": file_data.group(1),
            "offset": int(file_data.group(2)), "length": int(file_data.group(3)),
            "eof": bool(int(file_data.group(4))), "data": file_data.group(5),
        }
    if line == "RME_FILE_LIST_END":
        return {"record": "file_list_end"}
    for prefix, record in (
        ("RME_FILE_CAPS ", "file_caps"),
        ("RME_FILE_WRITE_READY ", "file_write_ready"),
        ("RME_FILE_WRITE_OFFSET ", "file_write_offset"),
        ("RME_FILE_BULK_READY ", "file_bulk_ready"),
        ("RME_FILE_BULK_ACK ", "file_bulk_ack"),
        ("RME_FILE_BINARY_READY ", "file_binary_ready"),
        ("RME_FILE_BINARY_ACK ", "file_binary_ack"),
        ("RME_FILE_BINARY_NACK ", "file_binary_nack"),
        ("RME_FILE_BINARY_SUSPENDED ", "file_binary_suspended"),
        ("RME_FILE_SUSPENDED ", "file_suspended"),
        ("RME_FILE_BINARY_ABORTED ", "file_binary_aborted"),
        ("RME_FILE_BINARY_CONTROL_NACK ", "file_binary_control_nack"),
        ("RME_FILE_BINARY_READ_READY ", "file_binary_read_ready"),
        ("RME_FILE_BINARY_READ_COMPLETE ", "file_binary_read_complete"),
    ):
        if line.startswith(prefix):
            result = parse_fields(line[len(prefix) :])
            result["record"] = record
            return result
    if line.startswith("RME_FILE_WRITE_COMPLETE path="):
        return {"record": "file_write_complete", "path": line.split("=", 1)[1]}
    if line.startswith("RME_FILE_BULK_COMPLETE path="):
        return {"record": "file_bulk_complete", "path": line.split("=", 1)[1]}
    if line.startswith("RME_FILE_BINARY_COMPLETE path="):
        return {"record": "file_binary_complete", "path": line.split("=", 1)[1]}
    for text, record in (
        ("RME_FILE_ABORTED", "file_aborted"),
        ("RME_FILE_BINARY_ABORTED", "file_binary_aborted"),
        ("RME_FILE_BINARY_CONTROL_COMPLETE", "file_binary_control_complete"),
        ("RME_FILE_DELETED", "file_deleted"),
        ("RME_FILE_RENAMED", "file_renamed"),
        ("RME_FILE_DIRECTORY_CREATED", "file_directory_created"),
        ("RME_FILE_PRINT_QUEUED", "file_print_queued"),
        ("RME_FILE_FLASH_QUEUED", "file_flash_queued"),
    ):
        if line == text:
            return {"record": record}
    if line.startswith("echo:RME_ERROR workflow=file "):
        result = parse_fields(line[len("echo:RME_ERROR workflow=file ") :])
        result.update(record="file_error", message=line)
        return result
    if line.startswith("echo:RME_ERROR workflow=firmware "):
        result = parse_fields(line[len("echo:RME_ERROR workflow=firmware ") :])
        result.update(record="firmware_error", message=line)
        return result
    if line.startswith("RME_EVENT "):
        result = parse_fields(line[len("RME_EVENT ") :])
        result["record"] = "event"
        return result
    if line.startswith("RME_CHANGE "):
        result = parse_fields(line[len("RME_CHANGE ") :])
        result["record"] = "change"
        return result
    if line.startswith("RME_MANUFACTURER "):
        result = parse_fields(line[len("RME_MANUFACTURER ") :])
        result["record"] = "manufacturer"
        result["name"] = unquote(str(result.get("name", "")))
        return result
    if line.startswith("RME_MANUFACTURER_LOADED "):
        result = parse_fields(line[len("RME_MANUFACTURER_LOADED ") :])
        result["record"] = "manufacturer_loaded"
        result["name"] = unquote(str(result.get("name", "")))
        return result
    for prefix, record in (
        ("RME_LIGHT_STATE ", "light_state"),
        ("RME_LIGHT_POLICY ", "light_policy"),
        ("RME_LIGHT_LIVE ", "light_live"),
    ):
        if line.startswith(prefix):
            result = parse_fields(line[len(prefix) :])
            result["record"] = record
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
        ("RME_STATS_MEMORY ", "stats"),
    ):
        if line.startswith(prefix):
            result = parse_fields(line[len(prefix) :])
            result["record"] = record
            return result
    if line.startswith("RME_FIRMWARE_UNSTAGED "):
        result = parse_fields(line[len("RME_FIRMWARE_UNSTAGED ") :])
        result["record"] = "firmware_unstaged"
        return result
    if line.startswith("RME_FIRMWARE_RESTART "):
        result = parse_fields(line[len("RME_FIRMWARE_RESTART ") :])
        result["record"] = "firmware_restart"
        return result
    if line.startswith("RME_FIRMWARE "):
        result = parse_fields(line[len("RME_FIRMWARE ") :])
        result["record"] = "firmware_status"
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
        if result.get("profile") is None:
            result["profile"] = result["material"]
        if result.get("vendor") is None:
            result.pop("vendor", None)
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
        ("filament_movement", ("filament not moving", "movement fault")),
        ("extrusion_flow_limit", ("flow-pressure limit", "flow limit")),
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
    # The underscore is an intentional Marlin-parser sentinel. M998's firmware
    # implementation reads ``parser.string_arg``; without a leading nonnumeric
    # token that pointer begins at H/D and the handler cannot see its P phase.
    return "M998 _ P1 O%d D%s" % (offset, encoded)


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
