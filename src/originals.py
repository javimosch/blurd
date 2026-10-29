"""Opt-in retention of the SOURCE image, bounded by a TTL.

This is the one sanctioned exception to "never persist the source image"
(AGENTS.md rule 1). It holds only when BOTH are true:

  * the instance enables it   -- `originals.enabled`, default off
  * the request asks for it   -- `keep_original=SECONDS`

and the original always carries a deadline, capped by `originals.max_ttl`.
It is never a durability mechanism for queued uploads (rule 6): an original is
written only once the job is running and its bytes have decoded.

Settings resolve env > database row (dashboard) > config.json/defaults. The
database tier exists so a dashboard change reaches every replica on the next
request without a restart; env wins so a deployment can pin a value, and the
dashboard shows such a key as locked.
"""

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from . import db, store
from .errors import Conflict, ValidationError

HARD_MAX_TTL = 365 * 86400
MIN_TTL = 60
URL_MODES = ("authenticated", "signed")

# key -> (env var, default). Env vars are also in config.ENV_OVERRIDES.
SETTINGS = {
    "originals.enabled": ("BLURD_ORIGINALS_ENABLED", False),
    "originals.max_ttl": ("BLURD_ORIGINALS_MAX_TTL", 30 * 86400),
    "originals.url_mode": ("BLURD_ORIGINALS_URL_MODE", "authenticated"),
    "originals.sign_ttl": ("BLURD_ORIGINALS_SIGN_TTL", 900),
}
# A credential: read straight from the environment, never shown in the UI.
SECRET_ENV = "BLURD_ORIGINALS_SIGN_SECRET"
SECRET_KEY = "originals.sign_secret"


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _validate(key: str, value: Any) -> Any:
    if key == "originals.enabled":
        return _bool(value)
    if key in ("originals.max_ttl", "originals.sign_ttl"):
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{key} must be an integer number of seconds",
                                  {"got": value})
        if not MIN_TTL <= n <= HARD_MAX_TTL:
            raise ValidationError(
                f"{key} must be between {MIN_TTL} and {HARD_MAX_TTL} seconds",
                {"got": n, "min": MIN_TTL, "max": HARD_MAX_TTL})
        return n
    if key == "originals.url_mode":
        if value not in URL_MODES:
            raise ValidationError("originals.url_mode must be one of "
                                  f"{URL_MODES}", {"got": value})
        return value
    raise ValidationError(f"Unknown setting '{key}'",
                          {"known": sorted(SETTINGS)})


def settings(cfg, conn) -> Dict[str, Any]:
    """Effective values plus which keys env has locked."""
    rows = db.settings_all(conn)
    out: Dict[str, Any] = {"locked": []}
    for key, (env, default) in SETTINGS.items():
        name = key.split(".", 1)[1]
        if os.environ.get(env):
            raw, locked = cfg.get(key, default), True
        elif key in rows:
            raw, locked = rows[key], False
        else:
            raw, locked = cfg.get(key, default), False
        try:
            out[name] = _validate(key, raw)
        except ValidationError:
            out[name] = default         # a bad env value must not brick reads
        if locked:
            out["locked"].append(name)
    return out


def update(cfg, conn, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate everything first, then write: a half-applied form is worse
    than a refused one."""
    current = settings(cfg, conn)
    staged = {}
    for name, value in payload.items():
        key = f"originals.{name}"
        if key not in SETTINGS:
            raise ValidationError(f"Unknown setting '{name}'",
                                  {"known": [k.split(".", 1)[1] for k in SETTINGS]})
        if name in current["locked"]:
            raise Conflict(f"'{name}' is set by the environment and cannot be "
                           "changed at runtime",
                           {"env": SETTINGS[key][0]})
        staged[key] = _validate(key, value)
    for key, value in staged.items():
        db.setting_put(conn, key, value)
    return settings(cfg, conn)


def requested_ttl(cfg, conn, keep_original) -> Optional[int]:
    """Validate a request's `keep_original` at submit time (rule 7), so a
    caller learns of a refusal from the 4xx, not from polling."""
    if keep_original in (None, "", 0, "0"):
        return None
    try:
        n = int(keep_original)
    except (TypeError, ValueError):
        raise ValidationError("keep_original must be an integer number of seconds",
                              {"got": keep_original})
    s = settings(cfg, conn)
    if not s["enabled"]:
        raise ValidationError(
            "Original retention is disabled on this instance",
            {"setting": "originals.enabled", "env": SETTINGS["originals.enabled"][0]},
            ["Ask the operator to enable it (env or dashboard settings)",
             "Or submit without keep_original"])
    if not MIN_TTL <= n <= s["max_ttl"]:
        raise ValidationError(
            f"keep_original must be between {MIN_TTL} and {s['max_ttl']} seconds",
            {"got": n, "min": MIN_TTL, "max": s["max_ttl"]})
    return n


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def retain(cfg, conn, blobs, sha: str, raw: bytes, mime: str, seconds: int) -> None:
    """Object first, row second (rule 22). Re-putting identical bytes under
    the same content-addressed key is harmless, and refreshes a blob a racing
    sweep may have just deleted."""
    expires = _iso(datetime.now(timezone.utc) + timedelta(seconds=int(seconds)))
    rel = store.original_rel(sha)
    blobs.put(rel, raw, mime or "application/octet-stream")
    db.original_upsert(conn, sha, rel, mime or "application/octet-stream",
                       len(raw), expires)


def lookup(cfg, conn, sha: str, blobs=None):
    """(row, expired). Past its deadline the bytes are pruned here, lazily, so
    a slow sweep never serves them -- but the row stays as a tombstone, which
    is what lets a read answer 410 ("it existed; resubmit") instead of 404."""
    row = db.original_find(conn, sha)
    if row is None:
        return None, False
    if row["expires_at"] <= db.now():
        if row["blob_path"]:
            _prune_one(conn, blobs or store.build(cfg), row)
        return row, True
    return row, False


def _prune_one(conn, blobs, row) -> None:
    try:
        blobs.delete(row["blob_path"])
    except Exception:
        pass                    # already gone; the row is still marked
    db.original_expire(conn, row["source_sha"])


def prune(cfg, conn, blobs=None, limit: int = 200) -> int:
    """Reaper sweep. Bounded per pass; replicas racing is safe because both
    the object delete and the row delete are idempotent."""
    blobs = blobs or store.build(cfg)
    rows = db.expired_originals(conn, limit)
    for r in rows:
        _prune_one(conn, blobs, r)
    return len(rows)


# --- URLs ---------------------------------------------------------------------

def _secret(conn) -> bytes:
    env = os.environ.get(SECRET_ENV)
    if env:
        return env.encode()
    rows = db.settings_all(conn)
    if SECRET_KEY not in rows:
        # Replicas racing here: re-read after writing so each signs with the
        # value that actually landed. A lost race overwrites a secret that no
        # URL has been minted with yet.
        db.setting_put(conn, SECRET_KEY, secrets.token_hex(32))
        rows = db.settings_all(conn)
    return str(rows[SECRET_KEY]).encode()


def _sig(conn, sha: str, exp: int) -> str:
    return hmac.new(_secret(conn), f"{sha}.{exp}".encode(),
                    hashlib.sha256).hexdigest()


def info(cfg, conn, sha: str) -> Optional[Dict[str, Any]]:
    """The `original` field of a record: {url, expires_at} or None."""
    row, expired = lookup(cfg, conn, sha)
    if row is None or expired:
        return None
    url = f"/v1/originals/{sha}"
    out = {"url": url, "expires_at": row["expires_at"],
           "bytes": row["bytes"], "mime": row["mime"]}
    s = settings(cfg, conn)
    if s["url_mode"] == "signed":
        deadline = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        exp = min(int(time.time()) + s["sign_ttl"], int(deadline.timestamp()))
        out["url"] = f"{url}?exp={exp}&sig={_sig(conn, sha, exp)}"
        out["url_expires_at"] = _iso(datetime.fromtimestamp(exp, timezone.utc))
    return out


def verify(conn, sha: str, exp, sig) -> bool:
    try:
        exp = int(exp)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()) or not sig:
        return False
    return hmac.compare_digest(_sig(conn, sha, exp), str(sig))
