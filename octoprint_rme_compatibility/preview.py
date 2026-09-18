"""Read-only first-layer preview for labeled Orca/Prusa text G-code."""
import re
import math


def read_preview(path):
    objects = {}
    bed = None
    estimate = None
    height = None
    layer_height = None
    position = dict(X=None, Y=None, Z=None, E=0.0)
    relative = False
    relative_e = False
    layer = 0
    current = None
    segments = 0
    warnings = set()
    with open(path, encoding="utf-8", errors="replace") as source:
        for raw in source:
            line = raw.strip()
            if line.startswith("; max_z_height:"):
                try:
                    value = float(line.split(":", 1)[1])
                    if math.isfinite(value) and 0 < value < 10000:
                        height = value
                except ValueError:
                    pass
            # Slicer layer comments are coordinates, unlike arbitrary Z-like
            # text in thumbnails, custom macros, or accumulated travel moves.
            match = re.fullmatch(r";\s*(?:Z|Z_HEIGHT|LAYER_Z)\s*:\s*(\d+(?:\.\d+)?)\s*", line)
            if match:
                value = float(match.group(1))
                if math.isfinite(value) and 0 < value < 10000:
                    layer_height = max(layer_height or 0, value)
            if line.startswith("; estimated printing time (normal mode) ="):
                duration = line.split("=", 1)[1].strip()
                estimate = sum(int(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u]
                               for n, u in re.findall(r"(\d+)\s*([dhms])", duration))
            if line.startswith("; printable_area ="):
                bed = [[float(x), float(y)] for x, y in re.findall(
                    r"(-?[\d.]+)x(-?[\d.]+)", line.split("=", 1)[1])]
            if line == ";LAYER_CHANGE":
                layer += 1
            if layer > 1:
                continue
            match = re.match(r"(?:@Object\s+|; printing object\s+)(.+)", line)
            if match:
                current = match.group(1).strip()
                objects.setdefault(current, [])
                continue
            if line.startswith(("@Objectstop", "; stop printing object")):
                current = None
            code = line.split(";", 1)[0].strip().upper()
            command = code.split(" ", 1)[0]
            values = {k: float(v) for k, v in re.findall(r"([XYZEIJR])\s*(-?\d*\.?\d+)", code)}
            if command == "G90": relative = False
            elif command == "G91": relative = True
            elif command == "M83": relative_e = True
            elif command == "M82": relative_e = False
            elif command == "G92": position.update({k: v for k, v in values.items() if k in position})
            elif command in ("G0", "G1", "G2", "G3"):
                old = dict(position)
                for axis, value in values.items():
                    if axis not in position:
                        continue
                    rel = relative_e if axis == "E" else relative
                    position[axis] = ((position[axis] or 0) + value) if rel else value
                if current and layer == 1 and position["E"] > old["E"] and all(
                    p[a] is not None for p in (old, position) for a in ("X", "Y")
                ) and (old["X"], old["Y"]) != (position["X"], position["Y"]):
                    points = [(old["X"], old["Y"]), (position["X"], position["Y"])]
                    if command in ("G2", "G3"):
                        if "R" in values or not ("I" in values or "J" in values):
                            warnings.add("Unsupported arc omitted; preview incomplete.")
                            continue
                        cx, cy = old["X"] + values.get("I", 0), old["Y"] + values.get("J", 0)
                        radius = math.hypot(old["X"] - cx, old["Y"] - cy)
                        start = math.atan2(old["Y"] - cy, old["X"] - cx)
                        end = math.atan2(position["Y"] - cy, position["X"] - cx)
                        sweep = (end - start) % (2 * math.pi)
                        if command == "G2": sweep -= 2 * math.pi
                        steps = max(2, min(720, math.ceil(abs(sweep) * radius / .25)))
                        points = [(cx + radius * math.cos(start + sweep * i / steps),
                                   cy + radius * math.sin(start + sweep * i / steps)) for i in range(steps + 1)]
                    if segments + len(points) - 1 <= 50000:
                        objects[current].extend([*a, *b] for a, b in zip(points, points[1:]))
                        segments += len(points) - 1
                    else:
                        warnings.add("Preview segment limit reached.")
    return dict(objects=[dict(name=n, segments=s) for n, s in objects.items() if s],
                bed=bed, estimated_seconds=estimate, height=height or layer_height, warnings=sorted(warnings))
