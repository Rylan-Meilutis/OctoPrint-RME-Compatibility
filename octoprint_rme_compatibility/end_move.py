"""Conservative end-of-file bed-lowering detection; never infer from Z alone."""
import math
import re
import time

WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


def normalized(command):
    return " ".join(str(command).split(";", 1)[0].strip().upper().split())


def analyze(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    absolute = False
    z = None
    extruded = parked = nozzle_off = bed_off = False
    candidate = None
    position = 0
    safe_tail = {"M400", "M572", "M221", "M84", "M151", "M77", "M73", "M117",
                 "M104", "M140", "M141", "M107", "M106", "M201", "M204", "M205"}
    with open(path, "rb") as stream:
        for index, raw in enumerate(stream):
            position += len(raw)
            if position > 100 * 1024 * 1024 or (index % 1024 == 0 and time.monotonic() > deadline):
                return None
            code = normalized(raw.decode("utf-8"))
            if not code:
                continue
            # Explicit markers win; never capture twice. Numbered files,
            # expressions, parenthesized comments and macros are not guessed.
            if code.startswith("@") or any(c in code for c in "()*{}") or re.match(r"^N\d", code):
                return None
            match = re.match(r"^([GMTP]\d+(?:\.\d+)?)(?=\s|[A-Z]|$)", code)
            if not match:
                return None
            command = match.group(1)
            rest = code[match.end():]
            pairs = WORD.findall(rest)
            values = {key: float(value) for key, value in pairs}
            if command in {"G0", "G1", "G92", "M104", "M140"}:
                if WORD.sub("", rest).strip() or len(values) != len(pairs) or not all(math.isfinite(v) for v in values.values()):
                    return None
            if command in ("G20", "G53", "G54", "G55", "G56", "G57", "G58", "G59"):
                return None
            if command == "G90":
                absolute = True
            elif command == "G91":
                absolute = False
            elif command in ("G0", "G1"):
                candidate = None
                if "E" in values:
                    extruded |= values["E"] > 0
                    parked = nozzle_off = bed_off = False
                if (absolute and z is not None and extruded and parked and nozzle_off and bed_off
                        and set(values) <= {"Z", "F"} and "Z" in values and values["Z"] - z >= 10):
                    candidate = (position, code)
                if "Z" in values:
                    z = values["Z"] if absolute else (z + values["Z"] if z is not None else None)
            elif command == "P0":
                parked = True
                candidate = None
            elif command == "M104":
                nozzle_off = values.get("S") == 0
                if not nozzle_off:
                    candidate = None
            elif command == "M140":
                bed_off = values.get("S") == 0
                if not bed_off:
                    candidate = None
            elif command == "G92":
                if "Z" in values:
                    z = None
                candidate = None
            elif command not in safe_tail:
                candidate = None
                if command in ("G2", "G3") and "E" in values:
                    parked = nozzle_off = bed_off = False
                if command.startswith("G") and command not in ("G21", "G4"):
                    z = None
                if command.startswith("T") or command in ("M600", "M701", "M702", "M976"):
                    parked = False
    return candidate
