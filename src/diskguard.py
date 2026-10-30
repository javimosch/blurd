"""Opt-in disk guard: stop accepting work before the disk fills.

`storage.max_bytes` counts what blurd *thinks* it stores. This module asks the
filesystem instead, so it also catches originals, thumbnails, the database and
anything else sharing the volume. It is off by default (`max_disk_pct` = 0)
because statvfs on the home only describes the disk that matters when the blob
store lives on it -- true for local storage or a MinIO on the same VM, false
for an external S3 bucket.

Reads, deletes and the TTL sweep are never gated: they are how space is freed.
"""

import shutil
from typing import Any, Dict, Optional

from .errors import StorageFull


def _path(cfg) -> str:
    return str(cfg.get("storage.disk_path") or cfg.home)


def usage(cfg) -> Optional[Dict[str, Any]]:
    """{path,total,used,free,pct,max_pct}, or None if the path is unreadable."""
    path = _path(cfg)
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None
    pct = round(100.0 * du.used / du.total, 1) if du.total else 0.0
    return {"path": path, "total_bytes": du.total, "used_bytes": du.used,
            "free_bytes": du.free, "used_pct": pct,
            "max_pct": int(cfg.get("storage.max_disk_pct") or 0)}


def check(cfg) -> None:
    """Raise 507 `storage_full` when the guard is on and the disk is over it.
    An unreadable path fails open: the guard must not take the service down
    because of a misconfigured `disk_path`."""
    limit = int(cfg.get("storage.max_disk_pct") or 0)
    if not limit:
        return
    u = usage(cfg)
    if u is None or u["used_pct"] < limit:
        return
    raise StorageFull(
        f"Disk {u['used_pct']}% used, at or above the {limit}% guard; "
        "new work is refused until space is freed",
        details={"used_pct": u["used_pct"], "max_pct": limit},
        retry_after=300)
