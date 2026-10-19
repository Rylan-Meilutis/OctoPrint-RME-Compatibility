"""Bounded, job-local pause evidence; never invent a missing firmware reason."""
import re
import time


class PauseReason:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.reason = ""
        self.source = ""
        self.candidate = None

    def snapshot(self):
        return {"active": self.active, "reason": self.reason, "source": self.source}

    def evidence(self, message, source, now=None):
        message = str(message or "").strip()[:300]
        if not message or message.lower() in (
                "firmware_pause", "paused", "print paused", "pause",
                "tool change in progress", "tool change complete", "waiting",
                "homing", "loading filament", "unloading filament",
                "waiting for hotend", "waiting for bed", "probing bed"):
            return False
        self.candidate = (time.monotonic() if now is None else now, message, source)
        if self.active:
            self.reason, self.source = message, source
        return True

    def pause(self, reason=None, source="OctoPrint", now=None):
        now = time.monotonic() if now is None else now
        self.evidence(reason, source, now)
        self.active = True
        if self.candidate and 0 <= now - self.candidate[0] <= 60:
            _, self.reason, self.source = self.candidate
        else:
            self.reason, self.source = "Reason not reported by printer or OctoPrint", "unreported"
        return self.snapshot()

    def resume(self):
        self.active = False
        self.candidate = None
        return self.snapshot()

    def observe(self, line):
        action = re.match(r"^//\s*action:\s*(pause|paused)\b\s*(.*)$", line, re.I)
        if action:
            self.evidence(action[2], "Firmware action")
            if action[1].lower() == "paused":
                self.pause()
            return True
        # Keep fault notices before workflow promotion/suppression. Do not
        # treat ordinary load/unload/heating chatter as a pause cause.
        if re.search(r"\b(?:runout|movement|breakout) detection\s+(?:on|off)\b", line, re.I):
            return False
        if re.search(r"runout|stuck filament|filament (?:jam|stuck)|waste.?bin.*(?:full|pause)|thermal runaway|heating failed", line, re.I):
            message = re.sub(r"^(?://\s*action:\s*notification\s*|echo:|Error:)", "", line, flags=re.I)
            return self.evidence(message, "Firmware serial")
        return False
