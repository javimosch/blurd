"""Instance defaults an admin may change at runtime, shared by every replica.

Layering (lowest first):  DEFAULTS < config.json < runtime (database) < env

* runtime values live in ONE settings row, "runtime" = {dotted.path: value},
  so N replicas see a change within RUNTIME_TTL seconds -- the dashboard used
  to write config.json, which on Kubernetes meant "this pod, until restart".
* an env var that sets a path LOCKS it: the env still wins, and the dashboard
  shows the field read-only instead of accepting a change that does nothing.
* only the keys in RUNTIME are writable, each with its own validation, so the
  dashboard can never reach infrastructure (backends, credentials, sizing).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List

from .errors import ValidationError

SETTING = "runtime"
RUNTIME_TTL = 3.0           # seconds a replica may serve a stale value


def _int_range(lo, hi, allow_null=False):
    def v(x):
        if x is None and allow_null:
            return None
        try:
            n = int(x)
        except (TypeError, ValueError):
            raise ValidationError("must be an integer", {"got": x})
        if not lo <= n <= hi:
            raise ValidationError(f"must be between {lo} and {hi}",
                                  {"got": n, "min": lo, "max": hi})
        return n
    return v


def _float_range(lo, hi, ndigits=3):
    def v(x):
        try:
            f = round(float(x), ndigits)
        except (TypeError, ValueError):
            raise ValidationError("must be a number", {"got": x})
        if not lo <= f <= hi:
            raise ValidationError(f"must be between {lo} and {hi}",
                                  {"got": f, "min": lo, "max": hi})
        return f
    return v


def _choice(*opts):
    def v(x):
        if x not in opts:
            raise ValidationError(f"must be one of {', '.join(opts)}", {"got": x})
        return x
    return v


def _optimize(x):
    from .config import _optimize_block
    if not x or not int((x or {}).get("above_bytes") or 0):
        return None                         # off
    if not isinstance(x, dict):
        raise ValidationError('must be an object like {"above_bytes": 1500000}')
    return _optimize_block(x, int(x["above_bytes"]))


def _modes(x):
    from .config import REDACT_MODES
    if not x:
        return None
    if not isinstance(x, dict) or set(x) - {"face", "plate"} \
            or any(m and m not in REDACT_MODES for m in x.values()):
        raise ValidationError('must look like {"face": "solid"}',
                              {"known_modes": list(REDACT_MODES)})
    x = {k: m for k, m in x.items() if m}
    return x or None


def _allowed(x):
    from .config import REDACT_MODES
    if not isinstance(x, list) or not x or any(m not in REDACT_MODES for m in x):
        raise ValidationError("must be a non-empty list of redact modes",
                              {"known": list(REDACT_MODES)})
    return list(dict.fromkeys(x))


# path -> validator. None from a validator means "unset" (fall back).
RUNTIME: Dict[str, Callable[[Any], Any]] = {
    # redaction
    "profile.redact.mode": lambda x: _choice("pixelate", "blur", "solid")(x),
    "profile.redact.modes": _modes,
    "allowed_redact_modes": _allowed,
    # detection thresholds (same bounds as before: a typo cannot switch a
    # detector off with 1.0 or blur every rectangle with 0)
    "profile.detect.face.min_score": _float_range(0.05, 0.99),
    "profile.detect.plate.min_score": _float_range(0.05, 0.99),
    "profile.detect.plate.min_aspect": _float_range(0.0, 5.0),
    "profile.detect.plate.max_area": _float_range(0.0, 1.0),
    # output & retention defaults (scope limits stay the per-tenant floor)
    "profile.output.optimize": _optimize,
    "profile.output.quality": _int_range(30, 95),
    "profile.storage.ttl": _int_range(60, 30 * 86400, allow_null=True),
    # ingest and storage
    "fetch.max_bytes": _int_range(64 * 1024, 64 * 1024 * 1024),
    "storage.max_bytes": _int_range(0, 1 << 50),
    "storage.evict": _choice("off", "oldest"),
    "storage.evict_high_pct": _int_range(10, 100),
    "storage.evict_low_pct": _int_range(5, 99),
    "storage.orphan_check_hours": _int_range(0, 24 * 30),
}


def _env_locked_paths() -> List[str]:
    """Runtime paths an env var currently sets (and therefore wins on)."""
    from .config import ENV_OVERRIDES
    locked = []
    for var, (path, _cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(var)
        if raw in (None, ""):
            continue
        if path == "profile":
            try:
                prof = json.loads(raw)
            except ValueError:
                continue
            for p in RUNTIME:
                if p.startswith("profile.") and _has(prof, p.split(".")[1:]):
                    locked.append(p)
        elif path in RUNTIME:
            locked.append(path)
    return sorted(set(locked))


def _has(d, parts) -> bool:
    for part in parts:
        if not isinstance(d, dict) or part not in d:
            return False
        d = d[part]
    return True


def load(conn) -> Dict[str, Any]:
    from . import db
    raw = db.settings_all(conn).get(SETTING) or {}
    return {k: v for k, v in raw.items() if k in RUNTIME}


def put(conn, changes: Dict[str, Any]) -> Dict[str, Any]:
    """Validate everything, then write once. A None value unsets the path."""
    from . import db
    if not isinstance(changes, dict) or not changes:
        raise ValidationError("Nothing to change")
    locked = set(_env_locked_paths())
    clean = {}
    for path, value in changes.items():
        if path not in RUNTIME:
            raise ValidationError(f"'{path}' is not a runtime setting",
                                  {"known": sorted(RUNTIME)})
        if path in locked:
            raise ValidationError(f"'{path}' is set by an environment variable "
                                  "and cannot be changed from the dashboard",
                                  {"path": path})
        try:
            clean[path] = None if value is None else RUNTIME[path](value)
        except ValidationError as e:
            raise ValidationError(f"{path} {e.message}", e.details)
    cur = load(conn)
    for k, v in clean.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    lo = cur.get("storage.evict_low_pct")
    hi = cur.get("storage.evict_high_pct")
    if lo is not None and hi is not None and lo >= hi:
        raise ValidationError("evict_low_pct must be below evict_high_pct",
                              {"low": lo, "high": hi})
    db.setting_put(conn, SETTING, cur)
    return cur


def overlay(data: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Write dotted paths into a (copied) config tree."""
    out = json.loads(json.dumps(data))
    for path, value in values.items():
        node = out
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return out


def adopt_file(cfg, conn) -> List[str]:
    """One-time carry-over: values an older dashboard wrote to config.json
    move into the shared row (only where the row has none), so upgrading a
    single-pod instance -- or copying its database to a cluster -- keeps them."""
    if not cfg.config_file.exists():
        return []
    try:
        raw = json.loads(cfg.config_file.read_text())
    except (OSError, ValueError):
        return []
    cur = load(conn)
    moved = []
    for path in RUNTIME:
        node, ok = raw, True
        for p in path.split("."):
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                ok = False
                break
        if ok and path not in cur and node is not None:
            try:
                cur[path] = RUNTIME[path](node)
                moved.append(path)
            except ValidationError:
                pass
    if moved:
        from . import db
        db.setting_put(conn, SETTING, cur)
    return moved


def describe(cfg, conn) -> Dict[str, Any]:
    """What the dashboard renders: effective value, where it comes from."""
    from .config import DEFAULTS
    stored = load(conn)
    locked = _env_locked_paths()
    out = {}
    for path in RUNTIME:
        default = DEFAULTS
        for p in path.split("."):
            default = default.get(p) if isinstance(default, dict) else None
        out[path] = {"value": cfg.get(path), "default": default,
                     "source": ("env" if path in locked else
                                "dashboard" if path in stored else "default/file")}
    return {"settings": out, "locked": locked, "ttl_seconds": RUNTIME_TTL}
