"""Storage rotation: keep live bytes under `storage.max_bytes` by evicting the
oldest objects instead of refusing new work.

Opt-in (`storage.evict = "oldest"`, and a non-zero `max_bytes`). Without it the
cap behaves as before: expired TTL blobs are reclaimed, then 507.

Order, cheapest loss first:
  1. objects already past their TTL (blobs and retained originals)
  2. retained originals of images with NO open feedback, oldest first
  3. redacted blobs of images with NO open feedback, oldest first
  4. retained originals of images WITH open feedback
  5. redacted blobs of images WITH open feedback
Images under review are not exempt -- a review queue must not be able to pin
the disk -- they just go last.

What is deliberately NOT rotated: database rows. An evicted image keeps its
row, detections, codes and thumbnail and answers 410 with reason "evicted".
Database growth is bounded by `storage.max_disk_pct` (a refusal), and the
operator decides whether to grow the disk or prune rows.

Usage is blurd's own accounting (artifacts.blob_size + originals.bytes), so it
is the same on local disk and on S3 -- the S3 protocol has no quota call. When
the bucket itself refuses a write for quota, `store.QuotaExceeded` is raised
and the write path evicts and retries once (see pipeline._put_with_room).
"""

from __future__ import annotations

import threading
from typing import Dict, Optional

from . import db, originals

BATCH = 200
# Inline (on the write path) a worker frees at most this many objects; the
# reaper finishes the job within REAP_INTERVAL. Keeps one unlucky request from
# paying for a cap that was just lowered from 20 GB to 100 MB.
INLINE_MAX_OBJECTS = 1000

_lock = threading.Lock()


def enabled(cfg) -> bool:
    return (str(cfg.get("storage.evict") or "off").lower() == "oldest"
            and int(cfg.get("storage.max_bytes") or 0) > 0)


def usage(conn) -> int:
    return db.live_blob_bytes(conn) + db.live_original_bytes(conn)


def marks(cfg) -> tuple:
    """(cap, high, low) in bytes. Percentages are clamped to sane values and
    low is kept below high, so a typo cannot make rotation spin or no-op."""
    cap = int(cfg.get("storage.max_bytes") or 0)
    hi = min(max(int(cfg.get("storage.evict_high_pct") or 90), 10), 100)
    lo = min(max(int(cfg.get("storage.evict_low_pct") or 75), 5), hi - 1)
    return cap, cap * hi // 100, cap * lo // 100


def evict_to(cfg, conn, blobs, target: int,
             max_objects: Optional[int] = None) -> Dict[str, int]:
    """Evict until live usage <= target. Returns counts per phase plus
    `freed_bytes` and `usage_bytes` (after). Safe to run on N replicas at
    once: deletes are idempotent and usage is re-read between phases."""
    with _lock:
        return _evict_locked(cfg, conn, blobs, target, max_objects)


def _evict_locked(cfg, conn, blobs, target, max_objects):
    out = {"expired": 0, "originals": 0, "blobs": 0,
           "originals_protected": 0, "blobs_protected": 0, "freed_bytes": 0}
    budget = [max_objects if max_objects is not None else 10 ** 9]
    used = usage(conn)
    if used <= target:
        out["usage_bytes"] = used
        return out

    # 1. already expired -- these were going anyway
    rows = db.expired_blobs(conn, BATCH)
    for r in rows:
        _delete(blobs, r["blob_path"])
        db.expire_artifact(conn, r["id"])
    out["expired"] += len(rows)
    if rows:
        conn.commit()
    out["expired"] += originals.prune(cfg, conn, blobs)
    used = usage(conn)

    def originals_phase(protected, key):
        nonlocal used
        while used > target and budget[0] > 0:
            rows = db.original_eviction_candidates(conn, protected,
                                                   min(BATCH, budget[0]))
            if not rows:
                return
            for r in rows:
                _delete(blobs, r["blob_path"])
                db.original_expire(conn, r["source_sha"])
                used -= int(r["bytes"] or 0)
                out["freed_bytes"] += int(r["bytes"] or 0)
                out[key] += 1
                budget[0] -= 1
                if used <= target:
                    break
            conn.commit()

    def blobs_phase(protected, key):
        nonlocal used
        while used > target and budget[0] > 0:
            rows = db.eviction_candidates(conn, protected,
                                          min(BATCH, budget[0]))
            if not rows:
                return
            for r in rows:
                _delete(blobs, r["blob_path"])
                db.evict_artifact(conn, r["id"])
                used -= int(r["blob_size"] or 0)
                out["freed_bytes"] += int(r["blob_size"] or 0)
                out[key] += 1
                budget[0] -= 1
                if used <= target:
                    break
            conn.commit()

    originals_phase(False, "originals")
    blobs_phase(False, "blobs")
    originals_phase(True, "originals_protected")
    blobs_phase(True, "blobs_protected")

    out["usage_bytes"] = usage(conn)
    n = sum(out[k] for k in ("expired", "originals", "blobs",
                             "originals_protected", "blobs_protected"))
    if n:
        # One audit row per pass, not per object: a pass can be thousands.
        db.audit(conn, "system", "storage.evict", None, None,
                 {**out, "target_bytes": target})
        conn.commit()
    return out


def _delete(blobs, rel: str) -> None:
    try:
        blobs.delete(rel)
    except Exception:
        pass          # already gone, or the store hiccupped. The row is still
                      # tombstoned: a stray object is garbage that
                      # `blurd storage scan` reports, never a broken record.


def sweep(cfg, conn, blobs) -> Optional[Dict[str, int]]:
    """Reaper hook: above the high mark, bring usage down to the low mark."""
    if not enabled(cfg):
        return None
    cap, high, low = marks(cfg)
    if usage(conn) <= high:
        return None
    return evict_to(cfg, conn, blobs, low)
