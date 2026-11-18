"""Orphaned objects: bytes in the store that no database row points at.

They cost disk (or bucket quota) but are invisible to blurd's accounting, so
neither `storage.max_bytes` nor rotation can see or free them. The report is
read-only; deletion is a separate, explicit step.

Every object is classified by what the database says about its key:

  probe              .blurd-write-probe and similar housekeeping files
  tombstoned         the artifact row exists but was expired/evicted
                     (blob_path cleared) and the object delete failed or was
                     skipped -- e.g. a store hiccup during a sweep
  superseded         the image is known, but no artifact row has this
                     (sha, profile) any more -- the row was replaced by a
                     reprocess/force/on_conflict=replace without deleting the
                     old object, or an older blurd forgot to
  image_deleted      no image row for this sha at all: the image was deleted
                     (or the database was reset) and its objects stayed
  original_pruned    a retained original whose row says pruned (or is gone)
  unparseable        a key blurd would never write

Safety for deletion: the write path stores the OBJECT first and the ROW second
(rule 22), so an object a few milliseconds old legitimately has no row yet.
Nothing younger than `min_age_s` (default one hour) is ever deleted.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, Iterable, Optional

from . import db

_BLOB = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})-([0-9a-f]{8,64})(\.[a-z0-9]+)?$")
_ORIG = re.compile(r"^originals/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")
CLASSES = ("tombstoned", "superseded", "image_deleted", "original_pruned",
           "unparseable", "probe")
SAMPLE = 8


def _parse_ts(s: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def live_keys(refs) -> set:
    live = {a["blob_path"] for a in refs["artifacts"] if a["blob_path"]}
    live |= {o["blob_path"] for o in refs["originals"] if o["blob_path"]}
    return live


def classify_all(refs, objects: Iterable[Dict[str, Any]]):
    """Yield (object, class) for every object; class None = referenced."""
    live = live_keys(refs)
    pairs = {(a["source_sha"], a["profile_hash"]): a for a in refs["artifacts"]}
    images = set(refs["images"])
    originals = {o["source_sha"]: o for o in refs["originals"]}
    for o in objects:
        key = o["key"]
        if key in live:
            yield o, None
            continue
        if key.startswith(".blurd") or key.endswith("write-probe"):
            yield o, "probe"
            continue
        m = _BLOB.match(key)
        if m:
            sha, ph = m.group(3), m.group(4)
            a = pairs.get((sha, ph))
            if a is not None:
                yield o, "tombstoned"
            elif sha in images:
                yield o, "superseded"
            else:
                yield o, "image_deleted"
            continue
        m = _ORIG.match(key)
        if m:
            yield o, "original_pruned"
            continue
        yield o, "unparseable"


def report(conn, store, min_age_s: int = 3600, delete: bool = False,
           classes: Iterable[str] = None) -> Dict[str, Any]:
    """Walk the store once. Dry-run unless `delete`; with `delete`, removes
    orphans of the given classes (default: all but `probe`/`unparseable`)
    that are older than `min_age_s`."""
    want = set(classes or ("tombstoned", "superseded", "image_deleted",
                           "original_pruned"))
    now = _dt.datetime.now(_dt.timezone.utc)
    stats = {c: {"objects": 0, "bytes": 0, "sample": [], "by_month": {}}
             for c in CLASSES}
    total = {"objects": 0, "bytes": 0}
    referenced = {"objects": 0, "bytes": 0}
    deleted = {"objects": 0, "bytes": 0, "skipped_young": 0, "errors": 0}
    refs = db.storage_refs(conn)
    live = live_keys(refs)
    seen = set()
    for o, cls in classify_all(refs, store.iter_objects()):
        total["objects"] += 1
        total["bytes"] += o["size"]
        if cls is None:
            seen.add(o["key"])
            referenced["objects"] += 1
            referenced["bytes"] += o["size"]
            continue
        st = stats[cls]
        st["objects"] += 1
        st["bytes"] += o["size"]
        month = (o.get("last_modified") or "?")[:7]
        bm = st["by_month"].setdefault(month, [0, 0])
        bm[0] += 1
        bm[1] += o["size"]
        if len(st["sample"]) < SAMPLE:
            st["sample"].append({"key": o["key"], "size": o["size"],
                                 "last_modified": o.get("last_modified")})
        if delete and cls in want:
            ts = _parse_ts(o.get("last_modified") or "")
            if ts is None or (now - ts).total_seconds() < min_age_s:
                deleted["skipped_young"] += 1
                continue
            try:
                store.delete(o["key"])
                deleted["objects"] += 1
                deleted["bytes"] += o["size"]
            except Exception:
                deleted["errors"] += 1
    orphan_objects = sum(s["objects"] for s in stats.values())
    orphan_bytes = sum(s["bytes"] for s in stats.values())
    out = {"backend": store.kind, "dry_run": not delete,
           "min_age_s": min_age_s, "total": total, "referenced": referenced,
           "orphans": {"objects": orphan_objects, "bytes": orphan_bytes},
           "accounted_bytes": db.live_blob_bytes(conn) + db.live_original_bytes(conn),
           # The reverse problem: a row pointing at an object that is not
           # there (a broken record -- rule 22 exists to prevent it).
           "missing_objects": {"count": len(live - seen),
                               "sample": sorted(live - seen)[:SAMPLE]},
           "classes": {c: {**s, "by_month": {m: {"objects": v[0], "bytes": v[1]}
                                             for m, v in sorted(s["by_month"].items())}}
                       for c, s in stats.items() if s["objects"]}}
    if delete:
        out["deleted"] = deleted
        out["delete_classes"] = sorted(want)
    return out
