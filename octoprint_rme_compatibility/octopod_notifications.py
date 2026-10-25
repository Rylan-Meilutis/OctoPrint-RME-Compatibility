"""Keep Continuous Print automation files out of OctoPod job notifications.

Only OctoPod's notification boundary is wrapped. Core PrintDone delivery must
remain intact: Continuous Print relies on it to advance to the actual job.
"""
import functools


HELPERS = frozenset("ContinuousPrint/tmp/continuousprint_%s.gcode" % name for name in (
    "activate", "start_print", "success", "cancel", "cooldown", "finish",
    "awaiting_material", "deactivate",
))


def is_helper(file_info):
    return (isinstance(file_info, dict) and file_info.get("origin") == "local"
            and file_info.get("path") in HELPERS)


def install(implementation):
    notifications = getattr(implementation, "_job_notifications", None)
    original = getattr(notifications, "send_print_job_notification", None)
    if not callable(original):
        return False
    if getattr(original, "_rme_helper_filter", False) is True:
        return True
    helper_job = False

    @functools.wraps(original)
    def filtered(settings, printer, event_payload, *args, **kwargs):
        nonlocal helper_job
        # Keep OctoPod's explicit Test button independent of the selected file.
        if kwargs.get("test") or len(args) >= 6 and args[5]:
            return original(settings, printer, event_payload, *args, **kwargs)
        state = (event_payload or {}).get("state_id")
        try:
            file_info = printer.get_current_data().get("job", {}).get("file", {})
        except (AttributeError, TypeError):
            file_info = {}
        if state in ("STARTING", "PRINTING", "PAUSED"):
            helper_job = is_helper(file_info)
        if helper_job or is_helper(file_info):
            # OctoPod detects completion from PrinterStateChanged, not just
            # PrintDone. Advance its state history without scheduling APNS or
            # IFTTT notifications, including delayed finish notifications.
            notifications._lastPrinterState = (event_payload or {}).get("state_string")
            return 0
        return original(settings, printer, event_payload, *args, **kwargs)

    filtered._rme_helper_filter = True
    notifications.send_print_job_notification = filtered
    return True
