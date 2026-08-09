"""OctoPrint RME Compatibility plugin package.

Keeping the protocol helpers importable without an OctoPrint installation makes
the wire protocol and uploader independently testable.
"""

import importlib.util

# OctoPrint parses package metadata from this file's AST before importing the
# package. Keep this as a literal top-level assignment: re-exporting the value
# from ``plugin.py`` happens too late for the pre-import compatibility check.
__plugin_pythoncompat__ = ">=3.8,<4"


if importlib.util.find_spec("octoprint") is not None:
    from .plugin import (  # noqa: F401
        __plugin_hooks__,
        __plugin_implementation__,
        __plugin_name__,
    )
