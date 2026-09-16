"""Bounded slicer metadata parsing and material-safe mapping recommendations."""
import functools
import math
import re


def material(value):
    value = str(value or "").strip().upper()
    if value in ("", "---", "NONE", "UNKNOWN", "NEW"):
        return ""
    # Do not equate composite/specialty materials with their base polymer.
    return {"PET": "PETG", "TPU": "FLEX", "TPE": "FLEX", "NYLON": "PA"}.get(value, value)


def compatible(required, loaded):
    return bool(material(required)) and material(required) == material(loaded)


def color_distance(a, b):
    if not all(re.fullmatch(r"#[0-9a-fA-F]{6}", str(c or "")) for c in (a, b)):
        return 200000  # Unknown color always ranks after a known RGB match.
    return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (1, 3, 5))


def read_requirements(path, count):
    """Read at most 2 MiB; support Orca/Prusa text G-code config comments."""
    count = min(8, max(0, int(count)))
    fields = {}
    with open(path, "rb") as stream:
        head = stream.read(1024 * 1024)
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(len(head), size - 1024 * 1024))
        text = (head + b"\n" + stream.read(1024 * 1024)).decode("utf-8", "replace")
    for line in text.splitlines():
        match = re.match(r"^;\s*(filament_type|filament_colour|filament_color|filament_used_mm|filament used \[mm\])\s*=\s*(.*)$", line)
        if match:
            fields[match[1]] = [v.strip().strip('"') for v in re.split(r"[;,]", match[2])][:8]
    materials = fields.get("filament_type", [])
    colors = fields.get("filament_colour", fields.get("filament_color", []))
    usage = fields.get("filament_used_mm", fields.get("filament used [mm]", []))
    result = []
    for tool, value in enumerate(materials[:count]):
        try:
            if tool < len(usage) and math.isfinite(float(usage[tool])) and float(usage[tool]) <= 0:
                continue
        except ValueError:
            pass
        if material(value):
            result.append({"logical": tool, "material": value,
                           "color": colors[tool] if tool < len(colors) else None})
    return result


def recommend(requirements, loaded, count):
    """Minimum total RGB distance among same-material one-to-one assignments."""
    count = min(8, max(0, int(count)))
    by_tool = {int(row["tool"]): row for row in loaded if "tool" in row}
    candidates = []
    for req in requirements:
        candidates.append([
            (tool, color_distance(req.get("color"), row.get("color")))
            for tool, row in sorted(by_tool.items()) if 0 <= tool < count and
            compatible(req["material"], row.get("firmware_material") or row.get("material"))
        ])

    @functools.lru_cache(maxsize=2048)
    def solve(index, used):
        if index == len(candidates):
            return (0, ())
        best = None
        for tool, cost in candidates[index]:
            if used & (1 << tool):
                continue
            tail = solve(index + 1, used | (1 << tool))
            if tail is not None:
                proposal = (cost + tail[0], (tool,) + tail[1])
                if best is None or proposal < best:
                    best = proposal
        return best

    if not requirements:
        return None
    result = solve(0, 0)
    if result is None:
        return None
    mapping = {req["logical"]: tool for req, tool in zip(requirements, result[1])}
    remaining = set(range(count)) - set(mapping.values())
    for logical in range(count):
        if logical not in mapping:
            destination = logical if logical in remaining else min(remaining)
            mapping[logical] = destination
            remaining.remove(destination)
    return mapping


def validate(requirements, loaded, mapping, enabled):
    by_tool = {int(row["tool"]): row for row in loaded if "tool" in row}
    for req in requirements:
        logical = int(req["logical"])
        physical = mapping.get(logical, mapping.get(str(logical), logical)) if enabled else logical
        row = by_tool.get(int(physical), {})
        if not compatible(req["material"], row.get("firmware_material") or row.get("material")):
            raise ValueError("T%d requires %s; physical T%d has no matching loaded material" %
                             (logical, req["material"], physical))
