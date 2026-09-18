"""API key scopes: restricting a key to a slice of the instance.

A scope is a CONJUNCTION of constraints:

    {"tags": ["acme"], "metadata": {"appId": "acme"}}
      -> "images tagged acme AND carrying appId=acme"

All constraints must hold. There is no OR anywhere, deliberately: a scope has
to be unambiguous, because it does not merely filter reads -- it defines a
TENANT, and a tenant owns a namespace.

Why a tenant and not just a filter
----------------------------------
Two things break if a scope is only a read filter:

1. *Unique codes collide.* `external_id` is a primary key, and two apps will
   both happily submit "IMG_0042.jpg". Without namespacing, one app's code
   silently resolves to the other app's photo -- a correctness bug, not a leak.

2. *Labels leak through dedup.* blurd stores identical bytes once. If two
   tenants submit the same image, they share a row, so tenant A would see
   tenant B's tags, metadata and codes on it.

So tags, metadata and external_ids are all stored per tenant, and the tenant id
is derived from the scope. Keys with the same scope share data; keys with
different scopes see nothing of each other. An unscoped key is the operator:
tenant "global", sees everything.
"""

from typing import Any, Dict, List, Optional, Tuple

from .canonical import canonical_json, sha256_text
from .errors import BlurdError, ValidationError

GLOBAL = "global"


class ScopeViolation(BlurdError):
    """The caller asked for something outside its key's scope."""

    def __init__(self, message: str, details: Dict[str, Any] = None):
        super().__init__(86, "scope_violation", message, details,
                         suggestions=["Use a key scoped for this data",
                                      "Omit the field and let the scope stamp it"])


class Scope:
    def __init__(self, raw: Optional[Dict[str, Any]] = None):
        raw = raw or {}
        self.tags: List[str] = sorted({str(t).strip() for t in (raw.get("tags") or [])
                                       if str(t).strip()})
        self.metadata: Dict[str, str] = {str(k): str(v)
                                         for k, v in (raw.get("metadata") or {}).items()}

    # -- identity -------------------------------------------------------------
    @property
    def is_global(self) -> bool:
        return not self.tags and not self.metadata

    def as_dict(self) -> Dict[str, Any]:
        return {"tags": self.tags, "metadata": self.metadata}

    @property
    def tenant(self) -> str:
        if self.is_global:
            return GLOBAL
        return "t_" + sha256_text(canonical_json(self.as_dict()))[:16]

    def describe(self) -> str:
        if self.is_global:
            return "unrestricted"
        bits = [f"tag:{t}" for t in self.tags]
        bits += [f"{k}={v}" for k, v in sorted(self.metadata.items())]
        return " AND ".join(bits)

    def __bool__(self) -> bool:
        return not self.is_global

    # -- writes ---------------------------------------------------------------
    def stamp(self, tags: List[str], metadata: Dict[str, Any]) -> Tuple[List[str], Dict[str, Any]]:
        """Force a submission into the scope.

        The scope's labels are applied automatically, so a scoped app does not
        have to remember to tag its own uploads -- and, more importantly,
        cannot create an image it would then be unable to read. An explicit
        value that contradicts the scope is refused rather than overwritten,
        because silently rewriting a caller's metadata is worse than saying no.
        """
        tags = list(tags or [])
        metadata = dict(metadata or {})
        for k, v in self.metadata.items():
            if k in metadata and str(metadata[k]) != v:
                raise ScopeViolation(
                    f"This key may only write {k}={v!r}",
                    {"key": k, "required": v, "got": str(metadata[k])})
            metadata[k] = v
        for t in self.tags:
            if t not in tags:
                tags.append(t)
        return tags, metadata

    # -- reads ----------------------------------------------------------------
    def sql(self, sha_column: str = "a.source_sha") -> Tuple[List[str], List[Any]]:
        """WHERE fragments restricting a query to this scope's tenant.

        NOTE for the Postgres port (spec/distributed.md, step 1): these
        fragments hard-code SQLite's `?` placeholder. Postgres wants `%s`, so
        the placeholder style has to become a property of the backend rather
        than of this function.

        Every constraint is checked against labels owned by THIS tenant, so an
        identical image labelled by someone else stays invisible.
        """
        if self.is_global:
            return [], []
        where, params = [], []
        tenant = self.tenant
        for t in self.tags:
            where.append(f"EXISTS (SELECT 1 FROM tags st WHERE st.source_sha={sha_column} "
                         "AND st.tenant=? AND st.tag=?)")
            params += [tenant, t]
        for k, v in sorted(self.metadata.items()):
            where.append(f"EXISTS (SELECT 1 FROM metadata sm WHERE sm.source_sha={sha_column} "
                         "AND sm.tenant=? AND sm.key=? AND sm.value=?)")
            params += [tenant, k, v]
        if not where:                      # defensive: never degrade to allow-all
            where, params = ["0=1"], []
        return where, params

    def allows_sha(self, conn, sha: str) -> bool:
        """Can this key see that image at all? Used by every single-record read.

        The execution lives in db.py so that module stays the only one running
        SQL; this stays here because the predicate is the scope's own logic."""
        if self.is_global:
            return True
        from . import db
        return db.scope_allows_sha(conn, sha, self)


def parse(raw: Optional[str]) -> Scope:
    import json
    if not raw:
        return Scope()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ValidationError("Key scope is not valid JSON", {"raw": str(raw)[:120]})
    return Scope(data)


def from_cli(tags: List[str], metas: List[str]) -> Scope:
    meta = {}
    for item in metas or []:
        if "=" not in item:
            raise ValidationError(f"--scope-meta expects K=V, got '{item}'")
        k, _, v = item.partition("=")
        meta[k.strip()] = v
    return Scope({"tags": tags or [], "metadata": meta})
