"""Small coalescing JSON persistence worker used outside the serial receive loop."""

import json
import os
import threading


class StateStore(object):
    """Coalesce snapshots and atomically replace the durable JSON state file."""
    def __init__(self, path, snapshot, logger=None):
        self.path = path
        self.snapshot = snapshot
        self.logger = logger
        self._dirty = threading.Event()
        self._stopping = threading.Event()
        self._thread = None

    def load(self):
        """Load valid object-shaped state or return an empty recovery value."""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
                return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rme-state-writer", daemon=True)
            self._thread.start()

    def request_save(self):
        """Schedule a save without doing filesystem I/O on the caller's thread."""
        self._dirty.set()

    def stop(self):
        self._stopping.set()
        self._dirty.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self):
        while True:
            self._dirty.wait()
            self._dirty.clear()
            self._write()
            if self._stopping.is_set() and not self._dirty.is_set():
                return

    def _write(self):
        """Flush and atomically replace so a power loss cannot leave half JSON."""
        temporary = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.snapshot(), handle, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            if self.logger:
                self.logger.exception("Could not persist RME UI state")
