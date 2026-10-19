"""Conservative extrusion bounds for ordinary millimetre, XY-plane G-code.

Never infer geometry from thumbnails or slicer estimates. Unsupported geometry
falls back to the slicer's existing mesh. Includes skirts, brims and wipe towers.
"""
import math
import re
import time

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
WORD = re.compile(r"([A-Z])\s*(" + NUMBER + r")", re.I)


def analyze(path, bed_x, bed_y, timeout=5.0):
    if not all(math.isfinite(v) and v > 0 for v in (bed_x, bed_y)):
        return None
    deadline = time.monotonic() + timeout
    x = y = None
    e = 0.0
    relative_xyz = False
    relative_e = False
    e_override = False
    bounds = None
    consumed = 0
    with open(path, "r", encoding="utf-8", errors="strict") as stream:
        for index, raw in enumerate(stream):
            consumed += len(raw)
            if consumed > 100 * 1024 * 1024 or (index % 1024 == 0 and time.monotonic() > deadline):
                return None
            line = re.sub(r"\([^)]*\)", "", raw.split(";", 1)[0]).strip().upper()
            if not line:
                continue
            match = re.match(r"^([GMT]\d+(?:\.\d+)?)(?=\s|[A-Z]|$)", line)
            if not match:
                # INDX P0 parks without depositing material. At-commands and
                # unknown macros may inject geometry: do not guess their bounds.
                if re.match(r"^P0(?:\s|$)", line):
                    x = y = None
                    continue
                return None
            command = match.group(1)
            rest = line[match.end():]
            if command in ("G20", "G53", "G54", "G55", "G56", "G57", "G58", "G59", "G10", "G11", "M206", "M218"):
                return None
            if command == "G90":
                relative_xyz = False
                if not e_override:
                    relative_e = False
            elif command == "G91":
                relative_xyz = True
                if not e_override:
                    relative_e = True
            elif command in ("M82", "M83"):
                relative_e = command == "M83"
                e_override = True
            elif command == "G92":
                pairs = WORD.findall(rest)
                words = dict((k, float(v)) for k, v in pairs)
                if (WORD.sub("", rest).strip() or len(words) != len(pairs)
                        or not all(math.isfinite(v) for v in words.values())
                        or set(words) != {"E"}):
                    return None
                e = words.get("E", e)
            elif command in ("G0", "G1", "G2", "G3"):
                if WORD.sub("", rest).strip():
                    return None
                pairs = WORD.findall(rest)
                words = dict((k, float(v)) for k, v in pairs)
                if len(words) != len(pairs) or not all(math.isfinite(v) for v in words.values()):
                    return None
                if set(words) - {"X", "Y", "Z", "E", "F", "I", "J", "R"}:
                    return None
                nx, ny = x, y
                if "X" in words:
                    nx = (x + words["X"] if x is not None else None) if relative_xyz else words["X"]
                if "Y" in words:
                    ny = (y + words["Y"] if y is not None else None) if relative_xyz else words["Y"]
                delta_e = words.get("E", 0) if relative_e else words.get("E", e) - e
                e = e + delta_e
                depositing = delta_e > 0 and ("X" in words or "Y" in words or command in ("G2", "G3"))
                if depositing:
                    if None in (x, y, nx, ny):
                        return None
                    points = [(x, y), (nx, ny)]
                    if command in ("G2", "G3"):
                        # A full-circle envelope safely contains either sweep.
                        if "R" in words or not ({"I", "J"} & set(words)):
                            return None
                        i, j = words.get("I", 0), words.get("J", 0)
                        radius = max(math.hypot(i, j), math.hypot(nx - x - i, ny - y - j))
                        points += [(x + i - radius, y + j - radius), (x + i + radius, y + j + radius)]
                    for px, py in points:
                        if not (0 <= px <= bed_x and 0 <= py <= bed_y):
                            return None
                        bounds = [px, py, px, py] if bounds is None else [min(bounds[0], px), min(bounds[1], py), max(bounds[2], px), max(bounds[3], py)]
                x, y = nx, ny
            elif command.startswith("G") and command not in ("G4", "G21", "G17"):
                if command.startswith("G29") or command in ("G28", "G12", "G27", "G427", "G750"):
                    x = y = None
                else:
                    return None
            elif command.startswith("T") or command in ("M870", "M976", "M701", "M702"):
                x = y = None
    if bounds is None:
        return None
    # Small line-width allowance; firmware retains its existing probe margin.
    left = max(0, math.floor((bounds[0] - 1) * 100) / 100)
    front = max(0, math.floor((bounds[1] - 1) * 100) / 100)
    right = min(bed_x, math.ceil((bounds[2] + 1) * 100) / 100)
    back = min(bed_y, math.ceil((bounds[3] + 1) * 100) / 100)
    return "@RME MESH SET x=%.2f y=%.2f width=%.2f height=%.2f" % (left, front, right - left, back - front)


def is_adaptive_probe(command):
    code = str(command).split(";", 1)[0].strip().upper()
    # Leave explicitly sized probes and extension passes untouched.
    return bool(re.match(r"^G29\s+", code) and re.search(r"(?:\s)P1(?:\s|$)", code)
                and not re.search(r"(?:\s)[XYWHC]", code))
