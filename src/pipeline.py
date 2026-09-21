"""The processing pipeline: bytes in, stored redacted artifact out.

Contract (identical whether called from the CLI, the API or a future port):
    process(cfg, source) -> result dict

The source image is never written to disk. Only sha256(source bytes), the
dimensions, and the redacted output are persisted.
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from . import db, detect, fetch, redact, store
from .canonical import canonical_json, sha256_hex
from .config import Config
from .errors import ValidationError

THUMB_MAX = 320


class _Timer:
    """Millisecond breakdown per stage -- what the API returns as stats.timings."""

    def __init__(self):
        self.t = {}
        self._start = time.perf_counter()

    def mark(self, name: str, t0: float) -> None:
        self.t[name] = round((time.perf_counter() - t0) * 1000, 2)

    @property
    def total_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 2)


def acquire(cfg: Config, *, url: str = None, path: str = None,
            data: bytes = None) -> tuple:
    """Return (bytes, mime, kind, ref)."""
    fcfg = cfg.get("fetch", {})
    if data is not None:
        return data, fetch.sniff_mime(data), "stream", None
    if url:
        raw, mime = fetch.fetch_url(url, fcfg)
        return raw, fetch.sniff_mime(raw) or mime, "url", url
    if path:
        raw, mime = fetch.read_file(path, fcfg)
        return raw, mime, "file", str(path)
    raise ValidationError("No source provided",
                          suggestions=["Pass a file path, --url, or a request body"])


def decode(raw: bytes, max_pixels: int) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValidationError(
            "Could not decode image",
            {"bytes": len(raw), "sniffed_mime": fetch.sniff_mime(raw)},
            ["Supported: jpeg, png, webp, bmp, tiff"])
    h, w = img.shape[:2]
    if h * w > max_pixels:
        # Decompression-bomb guard: refuse rather than allocate gigabytes.
        raise ValidationError(
            f"Image too large: {w}x{h} = {w*h} pixels exceeds max_pixels={max_pixels}",
            {"width": w, "height": h})
    return img


def run_detectors(cfg: Config, img: np.ndarray, profile: Dict[str, Any],
                  timer: _Timer) -> List[detect.Detection]:
    """Detect on a downscaled copy, then map boxes back to full resolution.
    Detection cost is quadratic in pixels; redaction quality is not."""
    dcfg = profile["detect"]
    max_side = int(dcfg.get("max_side", 1280))
    h, w = img.shape[:2]
    scale = 1.0
    work = img
    if max_side and max(h, w) > max_side:
        scale = max_side / float(max(h, w))
        work = cv2.resize(img, (int(round(w * scale)), int(round(h * scale))),
                          interpolation=cv2.INTER_AREA)

    dets: List[detect.Detection] = []
    for cls in ("face", "plate"):
        spec = dcfg.get(cls)
        if not spec or not spec.get("model"):
            continue
        t0 = time.perf_counter()
        det = detect.build(cfg.models_dir, spec["model"], spec["min_score"])
        found = det.detect(work)
        timer.mark(f"detect_{cls}_ms", t0)
        for d in found:
            if scale != 1.0:
                d = detect.Detection(d.cls, int(d.x / scale), int(d.y / scale),
                                     int(d.w / scale), int(d.h / scale),
                                     d.score, d.detector)
            dets.append(d)
    return dets


def encode(img: np.ndarray, out_cfg: Dict[str, Any]) -> tuple:
    """Encode the redacted image. cv2.imencode writes no EXIF, which is the
    point: GPS coordinates in the output would defeat the whole exercise."""
    fmt = out_cfg.get("format", "jpeg")
    quality = int(out_cfg.get("quality", 90))
    max_side = int(out_cfg.get("max_side", 0) or 0)
    if max_side and max(img.shape[:2]) > max_side:
        h, w = img.shape[:2]
        s = max_side / float(max(h, w))
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    if fmt == "png":
        ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        return (buf.tobytes(), "image/png", ".png") if ok else (None, None, None)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValidationError("Failed to encode output image")
    return buf.tobytes(), "image/jpeg", ".jpg"


def _thumb(img: np.ndarray) -> bytes:
    h, w = img.shape[:2]
    s = min(1.0, THUMB_MAX / float(max(h, w)))
    small = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                       interpolation=cv2.INTER_AREA) if s < 1.0 else img
    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 72])
    return buf.tobytes() if ok else b""


def process(cfg: Config, *, url: str = None, path: str = None, data: bytes = None,
            tags: List[str] = None, metadata: Dict[str, Any] = None,
            overrides: Dict[str, Any] = None, force: bool = False,
            tenant: str = "global") -> Dict[str, Any]:
    timer = _Timer()
    profile = cfg.resolve_profile(overrides)
    phash = Config.hash_of(profile)
    conn = db.connect(cfg.db_file)

    t0 = time.perf_counter()
    raw, mime, kind, ref = acquire(cfg, url=url, path=path, data=data)
    timer.mark("fetch_ms", t0)
    sha = sha256_hex(raw)
    source_bytes = len(raw)

    # Cache hit is on (source bytes, full profile) -- never on the sha alone,
    # or a model upgrade would silently keep serving stale redactions.
    cached = db.find_artifact(conn, sha, phash)
    blobs = store.build(cfg)
    live = (cached and cached["blob_path"] and not db.artifact_expired(cached))
    if live and not force and blobs.exists(cached["blob_path"]):
        db.merge_tags(conn, sha, tags or [], tenant)
        db.merge_metadata(conn, sha, metadata or {}, tenant)
        db.touch_image(conn, sha)
        conn.commit()
        result = artifact_result(cfg, conn, cached, cached=True, tenant=tenant)
        result["stats"]["timings"] = {"fetch_ms": timer.t.get("fetch_ms", 0),
                                      "total_ms": timer.total_ms}
        return result

    t0 = time.perf_counter()
    img = decode(raw, int(cfg.get("fetch.max_pixels", 50_000_000)))
    timer.mark("decode_ms", t0)
    height, width = img.shape[:2]
    del raw  # the source bytes are not kept a moment longer than needed

    dets = run_detectors(cfg, img, profile, timer)

    t0 = time.perf_counter()
    # In place: `img` is not read again, and for a 10 MP image the copy this
    # avoids is ~32 MB held for the whole encode.
    out_img = redact.apply(img, dets, profile["redact"], inplace=True)
    img = None
    timer.mark("redact_ms", t0)

    t0 = time.perf_counter()
    blob, out_mime, ext = encode(out_img, profile["output"])
    thumb = _thumb(out_img)
    timer.mark("encode_ms", t0)
    # Everything after this point is bytes and rows; the decoded frame is the
    # biggest thing in the process and there is no reason to hold it across the
    # store write and the database work.
    out_img = None

    t0 = time.perf_counter()
    # Object first, row second. An orphaned object is garbage a sweep can find;
    # a row pointing at an object that was never written is a broken record.
    rel = store.rel_path(sha, phash, ext)
    blobs.put(rel, blob, out_mime)
    timer.mark("store_ms", t0)

    n_faces = sum(1 for d in dets if d.cls == "face")
    n_plates = sum(1 for d in dets if d.cls == "plate")
    scores = [d.score for d in dets]
    needs_review = 1 if (not dets or min(scores, default=1.0) < 0.55) else 0

    stats = {
        "timings": dict(timer.t, total_ms=timer.total_ms),
        "source": {"bytes": source_bytes, "width": width, "height": height,
                   "mime": mime, "kind": kind},
        "detections": {"faces": n_faces, "plates": n_plates,
                       "min_score": round(min(scores), 4) if scores else None,
                       "max_score": round(max(scores), 4) if scores else None},
        "models": {c: profile["detect"][c]["model"] for c in ("face", "plate")
                   if profile["detect"].get(c)},
        "redact": profile["redact"]["mode"],
    }

    db.upsert_image(conn, sha, source_bytes, width, height, mime, kind, ref)
    db.merge_tags(conn, sha, tags or [], tenant)
    db.merge_metadata(conn, sha, metadata or {}, tenant)
    if cached:
        db.delete_artifact(conn, cached["id"])
    aid = db.insert_artifact(conn, {
        "source_sha": sha, "profile_hash": phash,
        "profile_json": canonical_json(profile), "blob_path": rel,
        "blob_sha": sha256_hex(blob), "blob_size": len(blob), "mime": out_mime,
        "n_faces": n_faces, "n_plates": n_plates,
        "min_score": min(scores) if scores else None,
        "needs_review": needs_review, "stats_json": canonical_json(stats),
        "thumb": thumb, "created_at": db.now(),
        "expires_at": _expires_at(profile),
    }, [d.as_dict() for d in dets])
    conn.commit()

    row = db.find_artifact_by_id(conn, aid)
    return artifact_result(cfg, conn, row, cached=False, tenant=tenant)


def _expires_at(profile: dict) -> Optional[str]:
    """TTL lives in the profile (so it is part of profile_hash) but the
    deadline is counted from processing, not submission: queued time must not
    eat the blob's lifetime."""
    ttl = profile.get("storage", {}).get("ttl")
    if ttl is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(ttl))
            ).isoformat(timespec="seconds").replace("+00:00", "Z")


def _blob_location(cfg: Config, rel: str) -> str:
    """Where the bytes are, in whichever vocabulary the backend speaks."""
    blobs = store.build(cfg)
    local = blobs.path_for(rel)
    return str(local) if local is not None else \
        f"s3://{blobs.bucket}/{blobs._key(rel)}"


def artifact_result(cfg: Config, conn, row, cached: bool,
                    tenant: str = None) -> Dict[str, Any]:
    """`tenant=None` means an unrestricted reader: show every label. A scoped
    reader is shown only its own, because the image row behind them is shared
    by dedup."""
    import json as _json
    sha = row["source_sha"]
    img = db.find_image(conn, sha)
    return {
        "source_sha": sha,
        "profile_hash": row["profile_hash"],
        "cached": cached,
        "needs_review": bool(row["needs_review"]),
        "blob": {
            "key": row["blob_path"],
            "location": _blob_location(cfg, row["blob_path"]),
            "url": f"/v1/blobs/{sha}?profile={row['profile_hash']}",
            "sha256": row["blob_sha"], "bytes": row["blob_size"], "mime": row["mime"],
        },
        "image": {"width": img["width"], "height": img["height"],
                  "mime": img["mime"], "source_kind": img["source_kind"],
                  "source_ref": img["source_ref"]},
        "codes": db.codes_of(conn, sha, tenant),
        **({"codes_detail": db.codes_detail(conn, sha)} if tenant is None else {}),
        "tags": db.tags_of(conn, sha, tenant),
        "metadata": db.metadata_of(conn, sha, tenant),
        "detections": db.detections_of(conn, row["id"]),
        "stats": _json.loads(row["stats_json"]),
        "created_at": row["created_at"],
        "expires_at": (row["expires_at"] if "expires_at" in row.keys()
                       else None),
        "errors": [],
        "warnings": (["no detections: redaction may have missed something"]
                     if row["needs_review"] else []),
    }
