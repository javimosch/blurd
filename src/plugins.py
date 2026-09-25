"""Optional plugin seam.

If a `blurd_pro` package is importable (installed alongside this source, or
on PYTHONPATH), `serve` hands it the running server once at startup. The
plugin registers extra route handlers through `BlurdServer.extra_routes` --
a dict of (method, path-prefix) -> fn(handler) -> bool. Returning True means
the request was handled.

Core code never imports plugin internals; the seam is one dict and one hook.
Nothing in this file is required for blurd to run -- the feature set in the
repo is complete without it.
"""

from typing import Any, Optional


def load(cfg) -> Optional[Any]:
    """Import `blurd_pro` if present; returns the module or None.

    The plugin decides for itself whether it is licensed/enabled; the loader
    only checks importability and calls install()."""
    try:
        import blurd_pro  # noqa: F401 -- presence check is the point
    except ImportError:
        return None
    return blurd_pro
