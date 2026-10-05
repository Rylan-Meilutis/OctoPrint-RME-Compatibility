"""Bounded, job-local pre-lowering snapshot for OctoPod notifications.

Only the explicit host marker is a capture barrier; never guess from Z motion.
The network operation runs in a daemon worker. The serialized sending hook
waits at most four seconds, so the next bed move cannot overtake a good capture.
"""
import threading
import time


class CompletionSnapshot:
    def __init__(self):
        self._lock = threading.RLock()
        self._generation = 0
        self._frame = None
        self._expires = 0
        self._worker = None
        self._owner = None
        self._original = None
        self._adapter = None

    def reset(self):
        with self._lock:
            self._generation += 1
            self._frame = None
            self._expires = 0
            if self._owner is not None and self._owner.image is self._adapter:
                self._owner.image = self._original
            self._owner = self._original = self._adapter = None

    def capture(self, manager, printer, logger, timeout=4.0):
        info = manager.plugins.get("octopod")
        if not info or not info.enabled:
            logger.info("RME completion snapshot skipped: OctoPod unavailable")
            return False
        implementation = info.implementation
        owner = getattr(implementation, "_job_notifications", None)
        original = getattr(owner, "image", None)
        settings = getattr(implementation, "_settings", None)
        if not callable(original) or settings is None:
            logger.warning("RME completion snapshot skipped: unsupported OctoPod image interface")
            return False
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                logger.warning("RME completion snapshot skipped: previous camera request still pending")
                return False
            self.reset()
            original = owner.image
            generation = self._generation
            camera = settings.get(["camera_snapshot_url"])
            if not camera:
                logger.warning("RME completion snapshot skipped: no OctoPod snapshot URL")
                return False
            orientation = [settings.get([key]) for key in ("webcam_flipH", "webcam_flipV", "webcam_rotate90")]
            ready = threading.Event()
            result = []

            def fetch():
                try:
                    # Keep the current lighting unchanged, even if the camera
                    # request finishes after timeout/cancel. OctoPod still
                    # applies its configured orientation and image sizing.
                    frame = original(False, camera, *orientation)
                    if isinstance(frame, bytes) and frame:
                        result.append(frame)
                except Exception:
                    logger.warning("RME completion snapshot camera request failed", exc_info=True)
                finally:
                    ready.set()

            self._worker = threading.Thread(target=fetch, name="rme-completion-snapshot", daemon=True)
            self._worker.start()
        # This barrier belongs only to the explicit at-command. No network
        # calls execute on the serial writer; receive processing remains live.
        if not ready.wait(timeout):
            logger.warning("RME completion snapshot timed out; continuing end G-code")
            return False
        with self._lock:
            if generation != self._generation or not result:
                return False
            self._frame = result[0]
            self._expires = time.monotonic() + 600

            def completion_image(*args, **kwargs):
                with self._lock:
                    frame = self._frame if generation == self._generation and time.monotonic() < self._expires else None
                # Do not substitute for live print-progress or paused images.
                if frame is not None and printer.get_state_id() in ("FINISHING", "OPERATIONAL"):
                    return frame
                return original(*args, **kwargs)

            self._owner, self._original, self._adapter = owner, original, completion_image
            owner.image = completion_image
        logger.info("RME completion snapshot captured before final bed move")
        return True
