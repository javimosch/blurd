"""Per-scope ingest policies: one blurd, several integrations, different rules.

A policy attaches to a SCOPE (the tenant a key's tags/metadata derive), not to
a single key -- keys sharing a scope share data, so they share its rules -- and
not to request tags, which the caller chooses. The unrestricted scope
("global") can carry one too; it then applies to every unscoped key.

    {"max_input_bytes": 2000000,
     "optimize": {"above_bytes": 1500000, "max_side": 2560, "quality": 82}}

* max_input_bytes -- a source larger than this is refused with 422, naming the
  limit and the scope, at submit for uploads and after the fetch for URLs.
  It can only be lower than the instance's fetch.max_bytes, never raise it.
* optimize -- a FLOOR on output.optimize: the request may tighten it (smaller
  threshold, side or quality) but never loosen or switch it off. Combined with
  any instance default the same way: the strictest value wins.
* ttl -- retention of the redacted blob, in seconds. Applied when the request
  sets none; a request may ask for LESS, never more (and never "forever").
* max_keep_original -- ceiling on a request's keep_original (opt-in source
  retention); a longer ask is clamped to it.

Stored in the shared database (settings key "scope_policies"), so every replica
enforces the same rules the moment one is changed.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import db
from .config import _optimize_block
from .errors import ValidationError

SETTING = "scope_policies"
RULES = ("max_input_bytes", "optimize", "ttl", "max_keep_original")
GLOBAL = "global"


def all_policies(conn) -> Dict[str, dict]:
    return dict(db.settings_all(conn).get(SETTING) or {})


def for_tenant(conn, tenant: str) -> Optional[dict]:
    return all_policies(conn).get(tenant or GLOBAL)


def validate(raw: Dict[str, Any]) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("A policy is an object like "
                              '{"max_input_bytes": 2000000}')
    unknown = set(raw) - {"max_input_bytes", "optimize", "ttl",
                          "max_keep_original", "label"}
    if unknown:
        raise ValidationError("Unknown policy key",
                              {"unknown": sorted(unknown),
                               "known": ["max_input_bytes", "optimize"]})
    out: Dict[str, Any] = {}
    if raw.get("max_input_bytes") not in (None, 0, ""):
        n = int(raw["max_input_bytes"])
        if not 1024 <= n <= 1 << 31:
            raise ValidationError("max_input_bytes must be between 1024 and 2^31",
                                  {"got": n})
        out["max_input_bytes"] = n
    opt = raw.get("optimize")
    if opt:
        if not isinstance(opt, dict):
            raise ValidationError("optimize must be an object")
        above = int(opt.get("above_bytes") or 0)
        if above > 0:
            out["optimize"] = _optimize_block(opt, above)
    for k in ("ttl", "max_keep_original"):
        if raw.get(k) not in (None, 0, ""):
            n = int(raw[k])
            if not 60 <= n <= 30 * 86400:
                raise ValidationError(f"{k} must be between 60 and 2592000 seconds",
                                      {"got": n})
            out[k] = n
    if raw.get("label"):
        out["label"] = str(raw["label"])[:120]
    return out


def put(conn, tenant: str, policy: Optional[dict]) -> Dict[str, dict]:
    """Set (or, with an empty policy, remove) the policy for a tenant."""
    pols = all_policies(conn)
    if policy and any(policy.get(k) for k in RULES):
        pols[tenant] = policy
    else:
        pols.pop(tenant, None)
    db.setting_put(conn, SETTING, pols)
    return pols


def tighten(cfg, policy: Optional[dict], overrides: Optional[dict]) -> Optional[dict]:
    """Request overrides clamped to the policy: per field the strictest of
    (request, instance default, policy) wins -- for output.optimize and for
    storage.ttl. A request can tighten, never loosen."""
    if not policy or not (policy.get("optimize") or policy.get("ttl")):
        return overrides
    over = dict(overrides or {})
    inst = cfg.resolve_profile(None)
    if policy.get("optimize"):
        out = dict(over.get("output") or {})
        candidates = [policy["optimize"]]
        req = out.get("optimize")
        if isinstance(req, dict) and int(req.get("above_bytes") or 0) > 0:
            candidates.append(_optimize_block(req, int(req["above_bytes"])))
        if inst.get("output", {}).get("optimize"):
            candidates.append(inst["output"]["optimize"])
        out["optimize"] = {k: min(c[k] for c in candidates)
                           for k in ("above_bytes", "max_side", "quality")}
        over["output"] = out
    if policy.get("ttl"):
        st = dict(over.get("storage") or {})
        ttls = [policy["ttl"]]
        for t in (st.get("ttl"), inst.get("storage", {}).get("ttl")):
            if t:
                ttls.append(int(t))
        st["ttl"] = min(ttls)
        over["storage"] = st
    return over


def clamp_keep(policy: Optional[dict], keep: Optional[int]) -> Optional[int]:
    """keep_original never longer than the scope allows."""
    cap = (policy or {}).get("max_keep_original")
    return min(int(keep), cap) if (keep and cap) else keep


def check_size(policy: Optional[dict], n: int, tenant: str) -> None:
    limit = (policy or {}).get("max_input_bytes")
    if limit and n > limit:
        raise ValidationError(
            f"Image is {_mb(n)}; this key's scope accepts at most {_mb(limit)}. "
            "Resize or recompress before sending, or ask the operator to raise "
            "the scope policy.",
            {"bytes": n, "max_input_bytes": limit, "tenant": tenant,
             "policy": "scope"},
            [f"Send images under {limit} bytes",
             f"blurd scope-policy set --tenant {tenant} --max-input-bytes N"])


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.2f} MB ({n} bytes)"
