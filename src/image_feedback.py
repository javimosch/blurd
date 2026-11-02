"""Per-image quality feedback: "this redaction is wrong, and how".

Written by people (dashboard, API) and read back by developer agents, so the
listing is shaped for a quick, filterable read: newest first, open by default,
with the sha and the links needed to look at the image.

Tenancy follows tags (AGENTS.md rule 12): a scoped key writes under its tenant
and reads only its own feedback; an unrestricted key reads everything. Adding
feedback needs read access to the image, and an out-of-scope sha is a 404.
"""

import json
import secrets
from typing import Any, Dict, Optional

from . import db
from .errors import NotFound, ValidationError
from .scope import GLOBAL, Scope

KINDS = ("missed", "false_positive", "bad_box", "ok", "other")
STATUSES = ("open", "resolved")
MAX_COMMENT = 4000


def _tenant(scope: Optional[Scope]) -> str:
    return scope.tenant if scope is not None and not scope.is_global else GLOBAL


def _read_tenant(scope: Optional[Scope]) -> Optional[str]:
    return None if scope is None or scope.is_global else scope.tenant


def _resolve_sha(conn, sha: str, scope: Optional[Scope]) -> Dict[str, Any]:
    row = db.find_artifact_by_sha_prefix(conn, sha)
    if not row:
        raise NotFound("image", sha)
    if scope is not None and not scope.is_global \
            and not scope.allows_sha(conn, row["source_sha"]):
        raise NotFound("image", sha)
    return row


def _box(raw):
    if raw is None:
        return None
    if (not isinstance(raw, (list, tuple)) or len(raw) != 4
            or not all(isinstance(v, (int, float)) and v >= 0 for v in raw)):
        raise ValidationError("box must be [x, y, w, h] in pixels of the "
                              "redacted image", {"got": raw})
    return [int(v) for v in raw]


def to_dict(row) -> Dict[str, Any]:
    r = dict(row)
    box = r.pop("box_json", None)
    r["box"] = json.loads(box) if box else None
    r.pop("tenant", None)
    sha = r["source_sha"]
    r["links"] = {"image": f"/v1/images/{sha}", "blob": f"/v1/blobs/{sha}"}
    return r


def add(conn, sha: str, payload: Dict[str, Any], scope: Optional[Scope],
        author: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object")
    kind = payload.get("kind")
    if kind not in KINDS:
        raise ValidationError("kind must be one of " + ", ".join(KINDS),
                              {"got": kind, "known": list(KINDS)})
    comment = str(payload.get("comment") or "")
    if len(comment) > MAX_COMMENT:
        raise ValidationError(f"comment is limited to {MAX_COMMENT} characters")
    row = _resolve_sha(conn, sha, scope)
    box = _box(payload.get("box"))
    rec = {"id": "fb_" + secrets.token_hex(8), "source_sha": row["source_sha"],
           "tenant": _tenant(scope),
           "profile_hash": payload.get("profile_hash") or row["profile_hash"],
           "kind": kind, "comment": comment,
           "box_json": json.dumps(box) if box else None,
           "author": str(author)[:120], "status": "open",
           "created_at": db.now()}
    db.feedback_insert(conn, rec)
    return to_dict(db.feedback_get(conn, rec["id"]))


def query(conn, scope: Optional[Scope], sha: Optional[str] = None,
          status: Optional[str] = "open", kind: Optional[str] = None,
          since: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    if status in ("", "all"):
        status = None
    if status is not None and status not in STATUSES:
        raise ValidationError("status must be open, resolved or all",
                              {"got": status})
    if kind is not None and kind not in KINDS:
        raise ValidationError("kind must be one of " + ", ".join(KINDS),
                              {"got": kind})
    if sha:
        sha = _resolve_sha(conn, sha, scope)["source_sha"]
    limit = max(1, min(int(limit or 100), 500))
    rows = db.feedback_query(conn, tenant=_read_tenant(scope), sha=sha,
                             status=status, kind=kind, since=since, limit=limit)
    items = [to_dict(r) for r in rows]
    return {"items": items, "count": len(items), "limit": limit}


def resolve(conn, fid: str, resolution: str, scope: Optional[Scope]) -> Dict[str, Any]:
    row = db.feedback_get(conn, fid)
    tenant = _read_tenant(scope)
    if row is None or (tenant is not None and row["tenant"] != tenant):
        raise NotFound("feedback", fid)
    db.feedback_resolve(conn, fid, str(resolution or "")[:MAX_COMMENT])
    return to_dict(db.feedback_get(conn, fid))
