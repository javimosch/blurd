"""API keys and dashboard basic auth.

Keys are stored as sha256 only. A dump of blurd.db must not let anyone call
the API, so the plaintext key is shown exactly once, at creation.
"""

import base64
import hmac
import secrets
from typing import Optional, Tuple

from . import db, scope as scope_mod
from .canonical import canonical_json, sha256_text
from .errors import AuthFailed, Conflict, NotFound, ValidationError

KEY_PREFIX = "blk_"


def generate(conn, name: str, actor: str = "cli", source_ip: str = None,
             scope: "scope_mod.Scope" = None, key: str = None) -> dict:
    provided = key is not None
    if provided:
        if not key:
            raise ValidationError("key must not be empty")
        key_sha = sha256_text(key)
        if any(r["key_sha"] == key_sha for r in db.all_api_keys(conn)):
            raise Conflict("a key with this secret already exists")
    else:
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        key_sha = sha256_text(key)
    key_id = secrets.token_hex(8)
    scope = scope or scope_mod.Scope()
    scope_json = None if scope.is_global else canonical_json(scope.as_dict())
    db.insert_api_key(conn, key_id, name, key[:12], key_sha, scope_json)
    db.audit(conn, actor, "key.create", key_id, source_ip,
             {"name": name, "prefix": key[:12], "scope": scope.describe(),
              "provided": provided})
    # `key` is returned once and never stored.
    return {"id": key_id, "name": name, "prefix": key[:12], "key": key,
            "scope": scope.as_dict() if not scope.is_global else None,
            "scope_description": scope.describe(), "tenant": scope.tenant,
            "warning": "Store this key now; it cannot be retrieved again."}


def verify(conn, key: str) -> Optional[dict]:
    if not key:
        return None
    row = db.find_api_key_by_hash(conn, sha256_text(key))
    if not row:
        return None
    db.touch_api_key(conn, row["id"])
    sc = scope_mod.parse(row["scope_json"])
    return {"id": row["id"], "name": row["name"], "scope": sc,
            "tenant": sc.tenant}


def revoke(conn, key_id: str, actor: str = "cli", source_ip: str = None) -> dict:
    if db.revoke_api_key(conn, key_id) == 0:
        raise NotFound("api_key", key_id)
    db.audit(conn, actor, "key.revoke", key_id, source_ip)
    return {"id": key_id, "revoked": True}


def listing(conn) -> list:
    out = []
    for r in db.all_api_keys(conn):
        sc = scope_mod.parse(r["scope_json"])
        out.append({"id": r["id"], "name": r["name"], "prefix": r["prefix"],
                    "created_at": r["created_at"], "last_used": r["last_used"],
                    "revoked": bool(r["revoked_at"]),
                    "scope": sc.as_dict() if not sc.is_global else None,
                    "scope_description": sc.describe(),
                    "tenant": sc.tenant})
    return out


def export_keys(conn) -> dict:
    """Portable key records: sha256 hashes and scopes, never plaintext. The
    same key then works on another instance without ever being re-shown."""
    keys = []
    for r in db.all_api_keys(conn):
        if r["revoked_at"]:
            continue
        sc = scope_mod.parse(r["scope_json"])
        keys.append({"id": r["id"], "name": r["name"], "prefix": r["prefix"],
                     "key_sha": r["key_sha"],
                     "scope": sc.as_dict() if not sc.is_global else None})
    return {"format": "blurd-keys/1", "keys": keys}


def import_keys(conn, payload: dict, actor: str = "cli",
                source_ip: str = None) -> dict:
    """Insert exported key records. Idempotent: a key_sha already present is
    skipped; an id already taken by a different key is remapped."""
    rows = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValidationError("not a key export: expected {\"keys\": [...]}")
    existing_ids = set()
    existing_shas = set()
    for r in db.all_api_keys(conn):
        existing_ids.add(r["id"])
        existing_shas.add(r["key_sha"])
    imported, skipped, remapped = [], [], {}
    for row in rows:
        sha = row.get("key_sha") if isinstance(row, dict) else None
        if not sha or not isinstance(row.get("name"), str):
            skipped.append({"name": (row or {}).get("name") if isinstance(row, dict) else None,
                            "reason": "malformed record"})
            continue
        if sha in existing_shas:
            skipped.append({"name": row["name"], "reason": "key_sha already present"})
            continue
        kid = row.get("id") or secrets.token_hex(8)
        if kid in existing_ids:
            new_id = secrets.token_hex(8)
            remapped[kid] = new_id
            kid = new_id
        sc = row.get("scope")
        scope_json = canonical_json(sc) if isinstance(sc, dict) else None
        db.insert_api_key(conn, kid, row["name"],
                          row.get("prefix") or sha[:12], sha, scope_json)
        db.audit(conn, actor, "key.import", kid, source_ip,
                 {"name": row["name"], "imported_id": kid,
                  "source_id": row.get("id")})
        existing_ids.add(kid)
        existing_shas.add(sha)
        imported.append({"id": kid, "name": row["name"]})
    return {"imported": imported, "skipped": skipped,
            "remapped_ids": remapped, "count": len(imported)}


def bearer_from_headers(headers) -> Optional[str]:
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return headers.get("X-API-Key")


def require_api_key(conn, headers) -> dict:
    principal = verify(conn, bearer_from_headers(headers))
    if not principal:
        raise AuthFailed()
    return principal


CSRF_COOKIE = "blurd_csrf"
CSRF_HEADER = "X-Blurd-CSRF"


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_from_cookie(headers) -> Optional[str]:
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == CSRF_COOKIE:
            return value
    return None


def check_csrf(headers) -> bool:
    """Double-submit cookie. The cookie is SameSite=Strict, so a cross-site
    request never carries it in the first place; the header echo means a
    request that somehow does carry it still cannot be forged by a page that
    cannot read it."""
    cookie = csrf_from_cookie(headers)
    sent = headers.get(CSRF_HEADER)
    return bool(cookie) and bool(sent) and hmac.compare_digest(cookie, sent)


def check_step_up(headers, secret: str) -> bool:
    """A second secret for minting keys, separate from the dashboard password.
    Without this, compromising one shared browser password yields permanent
    API access that survives changing that password."""
    if not secret:
        return False
    sent = headers.get("X-Blurd-Admin-Secret") or ""
    return hmac.compare_digest(sent, secret)


def check_basic(headers, user: str, password: str) -> bool:
    """Dashboard auth. Deliberately separate from API keys: a browser session
    and a machine-to-machine credential should never be the same secret."""
    auth = headers.get("Authorization") or ""
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
        got_user, _, got_pass = decoded.partition(":")
    except Exception:
        return False
    return (hmac.compare_digest(got_user, user or "")
            and hmac.compare_digest(got_pass, password or ""))
