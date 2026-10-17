"""Translate only explicit non-motion operator controls, never sliced G-code."""
import re


def command(code, state):
    if state.get("machine", {}).get("live_controls") != 1:
        return None
    code = str(code).split(";", 1)[0].strip().upper()
    match = re.fullmatch(r"(M104|M140|M106|M107|M220|M221)\s*((?:[STP]\s*\d{1,4}(?:\.\d{1,3})?\s*)*)", code)
    if not match:
        return None
    pairs = re.findall(r"([STP])\s*(\d+(?:\.\d+)?)", match[2])
    values = {key: int(float(value)) for key, value in pairs}
    if len(values) != len(pairs):
        return None
    opcode = match[1]
    if opcode == "M107" and not values:
        return "@RME CONTROL SET fan=0"
    if opcode == "M106" and set(values) <= {"S", "P"} and values.get("P", 0) == 0:
        target = values.get("S", 255)
        return "@RME CONTROL SET fan=%d" % target if target <= 255 else None
    if "S" not in values:
        return None
    target = values["S"]
    if opcode == "M140" and set(values) == {"S"} and target <= 150:
        return "@RME CONTROL SET bed=%d" % target
    if opcode == "M220" and set(values) == {"S"} and 10 <= target <= 300:
        return "@RME CONTROL SET speed=%d" % target
    if opcode not in ("M104", "M221") or set(values) - {"S", "T", "P"}:
        return None
    if opcode == "M104" and "P" in values:
        return None
    if opcode == "M221" and values.get("P", 0) not in (0, 1):
        return None
    tool = values.get("T")
    if tool is not None and not (opcode == "M221" and values.get("P") == 1):
        mapping = state.get("toolmap", {})
        if mapping.get("enabled"):
            slots = mapping.get("mapping") or {}
            tool = slots.get(tool, slots.get(str(tool), tool))
    if tool is None:
        tool = state.get("active_tool", {}).get("physical")
    if tool is None or not 0 <= int(tool) < int(state.get("machine", {}).get("tool_capacity", 0)):
        return None
    kind, limit = ("nozzle", (0, 500)) if opcode == "M104" else ("flow", (50, 150))
    if limit[0] <= target <= limit[1]:
        return "@RME CONTROL SET %s=%d tool=%d" % (kind, target, int(tool))
    return None
