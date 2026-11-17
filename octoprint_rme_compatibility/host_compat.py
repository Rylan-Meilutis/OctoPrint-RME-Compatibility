"""Small, feature-detected adapters for OctoPrint 1.11 and 2.0.

RME's wire protocol still requires the serial connector. Never substitute a
cached serial descriptor after the user switches to a different connector.
"""
import inspect

_MISSING = object()


def current_connection(printer):
    """Return (modern API present, active connection), without legacy probes."""
    if inspect.getattr_static(printer, "current_connection", _MISSING) is _MISSING:
        return False, None
    return True, printer.current_connection


def serial_comm(printer, cached=None):
    modern, connection = current_connection(printer)
    if modern:
        if connection is None or connection.connector != "serial":
            return None
        return getattr(connection, "_comm", None)
    return cached if cached is not None else getattr(printer, "_comm", None)


def reconnect_options(printer):
    modern, connection = current_connection(printer)
    if modern:
        if connection is None or connection.connector != "serial":
            raise RuntimeError("RME firmware reconnect requires an active serial connector")
        parameters = connection.connection_parameters
        rate = parameters.get("baudrate")
        options = {"connector": "serial", "parameters": {}}
        if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0:
            options["parameters"]["baudrate"] = rate
        return options
    options = {}
    try:
        rate = printer.get_current_connection()[2]
        if isinstance(rate, int) and not isinstance(rate, bool) and rate > 0:
            options["baudrate"] = rate
    except (AttributeError, IndexError, TypeError):
        pass
    return options


def printer_file_origin():
    try:
        from octoprint.filemanager.destinations import FileDestinations
    except ImportError:
        try:
            from octoprint.filemanager import FileDestinations
        except ImportError:
            return "sdcard"
    if hasattr(FileDestinations, "PRINTER"):
        return FileDestinations.PRINTER
    return getattr(FileDestinations, "SDCARD", "sdcard")


def is_printer_file(origin):
    # Also tolerate old snapshots/serial file objects during host upgrades.
    return origin in ("sdcard", "printer")


def install_printer_upload(printer, handler):
    """Adapt only this live serial connector's public upload method.

    2.0 PrinterFileStorage bypasses the legacy sdcardupload hook. Never patch
    a global connector class or alter another printer/connector's behavior.
    Returning NotImplemented explicitly retains OctoPrint's original route.
    """
    modern, connection = current_connection(printer)
    if not modern or connection is None or connection.connector != "serial":
        return False
    original = getattr(connection, "upload_printer_file", None)
    if not callable(original):
        return False
    policy = getattr(original, "_rme_upload_policy", None)
    if policy is not None:
        policy["handler"] = handler
        return True
    policy = {"handler":handler}

    def upload(source, target, progress_callback=None, *args, **kwargs):
        if current_connection(printer)[1] is not connection:
            from octoprint.printer import PrinterFilesUnavailableError
            if callable(progress_callback):
                progress_callback(failed=True)
            raise PrinterFilesUnavailableError("The printer connection changed before upload")
        result = policy["handler"](connection, source, target, progress_callback=progress_callback)
        if result is NotImplemented:
            return original(source, target, progress_callback=progress_callback, *args, **kwargs)
        return result

    upload._rme_upload_policy = policy
    connection.upload_printer_file = upload
    return True
