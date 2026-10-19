"""Validated host estimates; never infer remaining time from file bytes."""
import math


def progress_command(data, paused=False):
    if (data.get("job", {}).get("file") or {}).get("origin") != "local":
        return None  # Firmware owns media prints.
    progress = data.get("progress") or {}
    percent = progress.get("completion")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 100:
        return None
    remaining = progress.get("printTimeLeft")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or not math.isfinite(remaining) or not 0 <= remaining <= 31536000 or (remaining == 0 and percent < 100):
        remaining = "unknown"
    else:
        remaining = str(int(round(remaining)))
    return "@RME PROGRESS SET percent=%d remaining=%s paused=%d" % (int(percent), remaining, bool(paused))
