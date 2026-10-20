"""Order AUTO probes without replacing OctoPrint's handshake or serial writer."""

import functools
import importlib
import sys


def ordered_candidates(original, candidates=None):
    # Pass added rates THROUGH OctoPrint so its configured blocklist still wins.
    # A copy is essential: baudrateList mutates the supplied list in place.
    module = sys.modules.get(original.__module__)
    # 1.11 keeps its default list inside baudrateList; only newer hosts export
    # STANDARD_BAUDRATES. The settings API also calls this with no argument.
    defaults = getattr(module, "STANDARD_BAUDRATES", (115200, 250000, 230400, 57600, 38400, 19200, 9600))
    base = list(candidates if candidates is not None else defaults)
    base.extend(rate for rate in (1000000, 250000, 230400, 115200) if rate not in base)
    rates = original(base)
    # Some host versions remove only ONE occurrence of a blocked rate. An
    # additionalBaudrates duplicate must not accidentally defeat that policy.
    blocked = set()
    settings_factory = getattr(module, "settings", None)
    if callable(settings_factory):
        config = settings_factory()
        for path in (("serial", "blacklistedBaudrates"),
                     ("plugins", "serial_connector", "blocklistedBaudrates")):
            for rate in config.get(list(path)) or []:
                try:
                    blocked.add(int(rate))
                except (ValueError, TypeError):
                    continue
    return sorted({rate for rate in rates if isinstance(rate, int)
                   and not isinstance(rate, bool) and rate > 0 and rate not in blocked}, reverse=True)


def install(enabled):
    """Adapt the shared candidate utility (no baud-list extension hook exists).

    OctoPrint 1.x uses util.comm; 2.x moved it to serial_connector and imports
    an alias for the connection dropdown. Fixed-rate handshakes bypass this
    utility entirely. Never modify an open serial descriptor or reconnect it.
    """
    modules = []
    for name in ("octoprint.plugins.serial_connector.serial_comm", "octoprint.util.comm"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if callable(getattr(module, "baudrateList", None)):
            modules.append(module)
    if not modules:
        return False
    for module in modules:
        original = module.baudrateList
        if getattr(original, "_rme_baud_policy", False):
            original._rme_baud_policy["enabled"] = enabled
            original._rme_baud_policy["failed"] = False
            continue

        policy = {"enabled": enabled, "failed": False}

        @functools.wraps(original)
        def wrapped(candidates=None, _original=original, _policy=policy):
            if _policy["failed"]:
                return _original(candidates)
            try:
                if not _policy["enabled"]():
                    return _original(candidates)
                return ordered_candidates(_original, candidates)
            except Exception:
                # An optional speed preference must never take down the host's
                # settings/connection API. Fall back to native behavior until
                # restart rather than repeatedly failing every page load.
                _policy["failed"] = True
                return _original(candidates)

        wrapped._rme_baud_policy = policy
        # Cover already-imported aliases as well as the module used by AUTO.
        for name, target in list(sys.modules.items()):
            if name.startswith("octoprint.") and getattr(target, "baudrateList", None) is original:
                target.baudrateList = wrapped
    return True
