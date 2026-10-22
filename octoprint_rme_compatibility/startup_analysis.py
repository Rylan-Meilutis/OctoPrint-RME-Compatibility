"""Bounded background cache for optional, whole-file startup optimizations.

Callers never wait for a scan. A miss means use the slicer's original mesh or
omit an inferred completion snapshot for this job, never apply a late result.
"""
import collections
import logging
import os
import threading


def fingerprint(path):
    stat = os.stat(path)
    return os.path.realpath(path), stat.st_size, stat.st_mtime_ns, stat.st_ino


class BackgroundAnalysis:
    def __init__(self):
        self.lock = threading.Lock()
        self.results = collections.OrderedDict()
        self.pending = collections.OrderedDict()
        self.running = None
        self.worker = None

    def request(self, path, kind, callback, *args):
        key = (fingerprint(path), kind, args)
        with self.lock:
            if key in self.results:
                self.results.move_to_end(key)
                return self.results[key]
            if key != self.running:
                self.pending[key] = (callback, args)
                while len(self.pending) > 4:
                    self.pending.popitem(last=False)
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name='rme-file-analysis', daemon=True)
                self.worker.start()
        return None

    def _run(self):
        while True:
            with self.lock:
                if not self.pending:
                    self.running = None
                    self.worker = None
                    return
                key, (callback, args) = self.pending.popitem(last=False)
                self.running = key
            path = key[0][0]
            try:
                result = callback(path, *args)
                if fingerprint(path) != key[0]:
                    continue
            except Exception:
                logging.getLogger(__name__).debug('Optional file analysis unavailable', exc_info=True)
                result = None
            with self.lock:
                self.results[key] = result
                while len(self.results) > 8:
                    self.results.popitem(last=False)
