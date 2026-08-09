"""OctoPrint RME Compatibility plugin package.

Keeping the protocol helpers importable without an OctoPrint installation makes
the wire protocol and uploader independently testable.
"""

import importlib.util


if importlib.util.find_spec("octoprint") is not None:
    from .plugin import (  # noqa: F401
        __plugin_hooks__,
        __plugin_implementation__,
        __plugin_name__,
        __plugin_pythoncompat__,
    )
