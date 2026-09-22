"""MongoDB metadata backend.

This is not a translation of db_sql.py. A row-for-row port would put `images`,
`tags`, `metadata`, `external_ids` and `artifacts` in five collections and then
need `$lookup` on every listing -- and a `$lookup` cannot use an index for the
sort, so keyset pagination over a million artifacts degrades to a blocking
in-memory sort. That would be Mongo doing SQL's job badly.

So the document model is denormalised the way Mongo wants:

    artifacts      the listing collection. Carries its own copy of the parent
                   image's dimensions and of every tenant's labels, so a
                   filtered, sorted, cursor-paginated page is ONE indexed query
                   over ONE collection.
    images         one document per distinct sha: the canonical size/source
                   record, and the thing a dedup check looks at.
    labels         canonical tags/metadata per (source_sha, tenant).
    codes          canonical external ids, `_id` = tenant + the code, so the
                   uniqueness the API promises is enforced by the primary key
                   exactly as it is in SQL.
    thumbs, api_keys, jobs, instances, audit, counters, public_rules

`labels` and `codes` are canonical and `artifacts.lbl` is a cache of them. The
price is write amplification: changing a tag rewrites that image's artifacts.
That is the right side of the trade here, because labels are written once per
submission and read on every page of every listing -- and it is bounded, since
an image has as many artifacts as it has redaction profiles, typically one.

`_sync_labels` is the single place that projection is rebuilt. Every label
mutation ends by calling it; nothing else may write `artifacts.lbl`.
"""

import json
import time
from typing import Any, Dict, List, Optional

from .db_sql import (COUNT_CAP, SORTS, decode_cursor, encode_cursor, now)

# The NUL byte joins the two halves of a compound primary key. It cannot occur
# in a tenant id (hex) nor in an external id (the API rejects control
# characters), so the encoding is unambiguous.
_SEP = "\x00"

# Mongo equivalents of db_sql.SORTS. The SQL side sorts on `a.<col>`; here the
# field is top-level on the artifact document, so the mapping is only a rename.
_SORT_FIELD = {"created": "created_at", "faces": "n_faces", "plates": "n_plates",
               "size": "blob_size", "review": "needs_review"}
# `duration` is stored pre-coalesced as `duration_sort`, for the same reason
# db_sql.py coalesces it: a null sort key makes the keyset predicate silently
# unsatisfiable, and the page after it comes back empty.
_JOB_SORT_FIELD = {"created": "created_at", "duration": "duration_sort",
                   "status": "status"}

# Detections and the embedded label cache are large and almost never wanted by
# a caller that asked for "the artifact"; the two that do ask fetch them by name.
_NO_HEAVY = {"detections": 0, "lbl": 0}


# --- schema -------------------------------------------------------------------

def init(conn) -> None:
    """Create the indexes. Idempotent, which is what makes the lease-based
    migration lock in dialect.py sufficient rather than merely hopeful."""
    import pymongo
    db = conn.db

    db["images"].create_index([("last_seen", pymongo.DESCENDING)])

    arts = db["artifacts"]
    # One compound index per offered sort, each ending in `_id`, because the
    # keyset cursor orders by (sort field, _id) and a sort that is not fully
    # index-backed becomes a blocking in-memory sort at scale.
    for field in sorted(set(_SORT_FIELD.values())):
        arts.create_index([(field, pymongo.DESCENDING), ("_id", pymongo.DESCENDING)])
    # The dedup contract (source_sha, profile_hash) must be unique, matching
    # the SQL UNIQUE constraint. Existing deployments carry a non-unique
    # index on the same keys, which create_index(unique=True) refuses to
    # upgrade in place -- drop and rebuild it.
    existing = arts.index_information().get("source_sha_1_profile_hash_1")
    if existing and not existing.get("unique"):
        arts.drop_index("source_sha_1_profile_hash_1")
    arts.create_index([("source_sha", pymongo.ASCENDING),
                       ("profile_hash", pymongo.ASCENDING)], unique=True)
    arts.create_index([("source_sha", pymongo.ASCENDING),
                       ("created_at", pymongo.DESCENDING)])
    arts.create_index([("profile_hash", pymongo.ASCENDING)])
    # Multikey indexes over the denormalised labels. `lbl.mk` is the attribute
    # pattern -- metadata as [{k,v}] rather than an object -- because metadata
    # keys are caller-supplied, and you cannot have an index per unknown key.
    arts.create_index([("lbl.tenant", pymongo.ASCENDING),
                       ("lbl.tags", pymongo.ASCENDING)])
    arts.create_index([("lbl.mk.k", pymongo.ASCENDING),
                       ("lbl.mk.v", pymongo.ASCENDING)])
    arts.create_index([("lbl.codes", pymongo.ASCENDING)])

    labels = db["labels"]
    labels.create_index([("source_sha", pymongo.ASCENDING)])
    labels.create_index([("tenant", pymongo.ASCENDING), ("tags", pymongo.ASCENDING)])

    codes = db["codes"]
    codes.create_index([("source_sha", pymongo.ASCENDING)])
    codes.create_index([("external_id", pymongo.ASCENDING)])

    db["api_keys"].create_index([("key_sha", pymongo.ASCENDING)])

    jobs = db["jobs"]
    for field in sorted(set(_JOB_SORT_FIELD.values())):
        jobs.create_index([(field, pymongo.DESCENDING), ("_id", pymongo.DESCENDING)])
    jobs.create_index([("status", pymongo.ASCENDING)])
    jobs.create_index([("tenant", pymongo.ASCENDING)])
    jobs.create_index([("external_id", pymongo.ASCENDING)])

    db["instances"].create_index([("last_seen", pymongo.DESCENDING)])
    db["audit"].create_index([("seq", pymongo.DESCENDING)])


def migrate(conn) -> None:
    """Nothing to do: a mongo deployment starts at the current model. The
    0.2-0.5 migrations rewrote SQLite tables that never existed here."""
    return


def vacuum(conn) -> None:
    """Mongo reclaims space itself. Saying so beats running nothing and
    reporting success, which is what a silent no-op would do."""
    from .errors import ValidationError
    raise ValidationError(
        "`blurd vacuum` does not apply to the mongo backend",
        {"backend": "mongo"},
        ["MongoDB reclaims space on its own; there is nothing to run"])


# --- helpers ------------------------------------------------------------------

def _next_id(conn, name: str) -> int:
    """A monotonic integer id.

    `artifacts.id` is an int in the public API and in `jobs.artifact_id`, so it
    stays an int here rather than becoming an ObjectId: the alternative is a
    backend-visible type change in a response body.
    """
    doc = conn.db["counters"].find_one_and_update(
        {"_id": name}, {"$inc": {"seq": 1}}, upsert=True, return_document=True)
    return int(doc["seq"])


def _row(doc, id_field: str = None):
    """Present a document the way a SQL row arrives: `_id` exposed under the
    name the rest of the code already uses."""
    if doc is None:
        return None
    out = dict(doc)
    if id_field:
        out[id_field] = out["_id"]
    out.pop("_id", None)
    return out


def _meta_pairs(meta: Dict[str, str]) -> List[dict]:
    return [{"k": k, "v": v} for k, v in sorted((meta or {}).items())]


def _label_id(tenant: str, sha: str) -> str:
    return tenant + _SEP + sha


def _code_id(tenant: str, external_id: str) -> str:
    """The compound primary key, spelled as one.

    Uniqueness of (tenant, external_id) is then enforced by `_id` itself --
    the same guarantee, from the same mechanism, as SQL's PRIMARY KEY.
    """
    return tenant + _SEP + external_id


def _escape(value: str) -> str:
    import re
    return re.escape(str(value))


def _sync_labels(conn, sha: str) -> None:
    """Rebuild one image's denormalised label projection on its artifacts.

    The ONLY writer of `artifacts.lbl`. Every label mutation ends here, which
    is why the projection cannot drift: there is no second path to forget.
    """
    db = conn.db
    by_tenant: Dict[str, dict] = {}
    for doc in db["labels"].find({"source_sha": sha}):
        by_tenant[doc["tenant"]] = {
            "tenant": doc["tenant"],
            "tags": sorted(doc.get("tags", [])),
            "mk": _meta_pairs(doc.get("meta", {})),
            "codes": [],
        }
    for doc in db["codes"].find({"source_sha": sha}):
        entry = by_tenant.setdefault(
            doc["tenant"],
            {"tenant": doc["tenant"], "tags": [], "mk": [], "codes": []})
        entry["codes"].append(doc["external_id"])
    for entry in by_tenant.values():
        entry["codes"].sort()
    db["artifacts"].update_many({"source_sha": sha},
                                {"$set": {"lbl": list(by_tenant.values())}})


def _image_projection(conn, sha: str) -> dict:
    img = conn.db["images"].find_one({"_id": sha}) or {}
    return {k: img.get(k) for k in
            ("width", "height", "byte_size", "source_kind", "source_ref")}


# --- images / tags / metadata -------------------------------------------------

def upsert_image(conn, sha: str, byte_size: int, width: int, height: int,
                 mime: str, kind: str, ref: Optional[str]) -> None:
    ts = now()
    conn.db["images"].update_one(
        {"_id": sha},
        {"$set": {"last_seen": ts},
         "$setOnInsert": {"source_sha": sha, "byte_size": byte_size,
                          "width": width, "height": height, "mime": mime,
                          "source_kind": kind, "source_ref": ref,
                          "first_seen": ts}},
        upsert=True)


def merge_tags(conn, sha: str, tags: List[str], tenant: str = "global") -> None:
    clean = sorted({str(t).strip() for t in (tags or []) if str(t).strip()})
    if not clean:
        return
    conn.db["labels"].update_one(
        {"_id": _label_id(tenant, sha)},
        {"$addToSet": {"tags": {"$each": clean}},
         "$setOnInsert": {"source_sha": sha, "tenant": tenant}},
        upsert=True)
    _sync_labels(conn, sha)


def merge_metadata(conn, sha: str, meta: Dict[str, Any], tenant: str = "global") -> None:
    if not meta:
        return
    sets = {"meta." + str(k): (v if isinstance(v, str) else json.dumps(v))
            for k, v in meta.items()}
    conn.db["labels"].update_one(
        {"_id": _label_id(tenant, sha)},
        {"$set": sets, "$setOnInsert": {"source_sha": sha, "tenant": tenant}},
        upsert=True)
    _sync_labels(conn, sha)


def tags_of(conn, sha: str, tenant: str = None) -> List[str]:
    q: Dict[str, Any] = {"source_sha": sha}
    if tenant is not None:
        q["tenant"] = tenant
    out = set()
    for doc in conn.db["labels"].find(q, {"tags": 1}):
        out.update(doc.get("tags", []))
    return sorted(out)


def codes_of(conn, sha: str, tenant: str = None) -> List[str]:
    q: Dict[str, Any] = {"source_sha": sha}
    if tenant is not None:
        q["tenant"] = tenant
    return sorted({d["external_id"] for d in
                   conn.db["codes"].find(q, {"external_id": 1})})


def codes_detail(conn, sha: str) -> List[dict]:
    """Operator view: which tenant owns each code on this image."""
    rows = [{"tenant": d["tenant"], "external_id": d["external_id"]}
            for d in conn.db["codes"].find({"source_sha": sha})]
    return sorted(rows, key=lambda r: (r["tenant"], r["external_id"]))


def metadata_of(conn, sha: str, tenant: str = None) -> Dict[str, str]:
    q: Dict[str, Any] = {"source_sha": sha}
    if tenant is not None:
        q["tenant"] = tenant
    out: Dict[str, str] = {}
    for doc in conn.db["labels"].find(q, {"meta": 1}):
        out.update(doc.get("meta", {}))
    return out


def bulk_labels(conn, shas: List[str], tenant: str = None) -> Dict[str, dict]:
    """Labels for a whole page in two queries, for the same reason the SQL side
    does it in three: the per-row helpers turn one page into 3N round trips."""
    out = {sha: {"tags": [], "metadata": {}, "codes": []} for sha in shas}
    if not shas:
        return out
    q: Dict[str, Any] = {"source_sha": {"$in": list(shas)}}
    if tenant:
        q["tenant"] = tenant
    for doc in conn.db["labels"].find(q):
        entry = out[doc["source_sha"]]
        entry["tags"].extend(doc.get("tags", []))
        entry["metadata"].update(doc.get("meta", {}))
    for doc in conn.db["codes"].find(q):
        out[doc["source_sha"]]["codes"].append(doc["external_id"])
    for entry in out.values():
        entry["tags"] = sorted(set(entry["tags"]))
        entry["codes"].sort()
    return out


def facets(conn, tenant: str = "global", limit: int = 100) -> Dict[str, Any]:
    """Distinct tags and metadata key=values with usage counts, for the
    dashboard's clickable filter chips. One label doc per (source_sha,
    tenant), so a plain count is the same count SQL gets from DISTINCT."""
    match = {"$match": {"tenant": tenant} if tenant else {}}
    tags = [{"tag": d["_id"], "n": d["n"]} for d in conn.db["labels"].aggregate([
        match, {"$unwind": "$tags"},
        {"$group": {"_id": "$tags", "n": {"$sum": 1}}},
        {"$sort": {"n": -1, "_id": 1}}, {"$limit": limit}])]
    meta: Dict[str, list] = {}
    for d in conn.db["labels"].aggregate([
            match, {"$project": {"kv": {"$objectToArray": "$meta"}}},
            {"$unwind": "$kv"},
            {"$group": {"_id": {"k": "$kv.k", "v": "$kv.v"}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1, "_id.k": 1, "_id.v": 1}}, {"$limit": limit}]):
        meta.setdefault(d["_id"]["k"], []).append(
            {"value": d["_id"]["v"], "n": d["n"]})
    return {"tags": tags, "meta": meta}


def touch_image(conn, sha: str) -> None:
    conn.db["images"].update_one({"_id": sha}, {"$set": {"last_seen": now()}})


def find_image(conn, sha: str):
    return _row(conn.db["images"].find_one({"_id": sha}))


def delete_image(conn, sha: str) -> None:
    """Mongo has no ON DELETE CASCADE, so the cascade is explicit.

    Missing one of these leaves a label pointing at an image that no longer
    exists -- precisely the class of bug a foreign key exists to prevent, and
    the standing cost of the document model.
    """
    db = conn.db
    ids = [d["_id"] for d in db["artifacts"].find({"source_sha": sha}, {"_id": 1})]
    db["artifacts"].delete_many({"source_sha": sha})
    db["thumbs"].delete_many({"_id": {"$in": ids}})
    db["labels"].delete_many({"source_sha": sha})
    db["codes"].delete_many({"source_sha": sha})
    db["images"].delete_one({"_id": sha})


def release_tenant_labels(conn, sha: str, tenant: str) -> None:
    """A scoped delete drops only that tenant's claim on shared bytes."""
    conn.db["labels"].delete_many({"source_sha": sha, "tenant": tenant})
    conn.db["codes"].delete_many({"source_sha": sha, "tenant": tenant})
    _sync_labels(conn, sha)


def has_any_labels(conn, sha: str) -> bool:
    """Is this image still referenced by anybody? Decides whether the bytes go."""
    if conn.db["labels"].find_one({"source_sha": sha}, {"_id": 1}):
        return True
    return conn.db["codes"].find_one({"source_sha": sha}, {"_id": 1}) is not None


# --- artifacts ----------------------------------------------------------------

def find_artifact(conn, sha: str, profile_hash: str):
    return _row(conn.db["artifacts"].find_one(
        {"source_sha": sha, "profile_hash": profile_hash}, _NO_HEAVY), "id")


def insert_artifact(conn, row: dict, dets: List[dict]) -> Optional[int]:
    """Insert the artifact; return None when another worker already wrote the
    same (source_sha, profile_hash) -- see db_sql.insert_artifact."""
    thumb = row.pop("thumb", None)
    aid = _next_id(conn, "artifacts")
    doc = dict(row)
    doc["_id"] = aid
    doc["detections"] = [
        {"cls": d["cls"], "x": d["box"][0], "y": d["box"][1],
         "w": d["box"][2], "h": d["box"][3], "score": d["score"],
         "detector": d["detector"]} for d in dets]
    doc["img"] = _image_projection(conn, row["source_sha"])
    doc["lbl"] = []
    doc.setdefault("manual_regions", [])
    try:
        conn.db["artifacts"].insert_one(doc)
    except Exception as exc:
        if type(exc).__name__ == "DuplicateKeyError":
            return None
        raise
    if thumb:
        from bson.binary import Binary
        conn.db["thumbs"].update_one({"_id": aid},
                                     {"$set": {"jpeg": Binary(thumb)}},
                                     upsert=True)
    # The submission wrote its labels before the artifact existed, so the
    # projection has to be built now, not only on later mutations.
    _sync_labels(conn, row["source_sha"])
    return aid


def detections_of(conn, artifact_id: int) -> List[dict]:
    doc = conn.db["artifacts"].find_one({"_id": int(artifact_id)}, {"detections": 1})
    return [{"cls": d["cls"], "box": [d["x"], d["y"], d["w"], d["h"]],
             "score": d["score"], "detector": d["detector"]}
            for d in (doc or {}).get("detections", [])]


def find_artifact_by_id(conn, artifact_id: int):
    return _row(conn.db["artifacts"].find_one({"_id": int(artifact_id)}, _NO_HEAVY),
                "id")


def find_artifact_by_sha_prefix(conn, sha: str):
    doc = conn.db["artifacts"].find_one(
        {"source_sha": {"$regex": "^" + _escape(sha)}}, _NO_HEAVY,
        sort=[("created_at", -1)])
    return _row(doc, "id")


def delete_artifact(conn, artifact_id: int) -> None:
    conn.db["artifacts"].delete_one({"_id": int(artifact_id)})
    conn.db["thumbs"].delete_one({"_id": int(artifact_id)})


def artifact_blob_paths(conn, sha: str) -> List[str]:
    return [d["blob_path"] for d in
            conn.db["artifacts"].find(
                {"source_sha": sha, "blob_path": {"$ne": ""}}, {"blob_path": 1})]


def all_blob_paths(conn) -> List[str]:
    return [d["blob_path"] for d in conn.db["artifacts"].find(
        {"blob_path": {"$ne": ""}}, {"blob_path": 1})]


def expired_blobs(conn, limit: int = 200) -> List[dict]:
    """Artifacts past their TTL whose object may still be in the store."""
    cur = conn.db["artifacts"].find(
        {"expires_at": {"$ne": None, "$lt": now()}, "blob_path": {"$ne": ""}},
        {"blob_path": 1}).limit(limit)
    return [{"id": d["_id"], "blob_path": d["blob_path"]} for d in cur]


def live_blob_bytes(conn) -> int:
    """Bytes held by live blobs -- see db_sql.live_blob_bytes."""
    agg = list(conn.db["artifacts"].aggregate([
        {"$match": {"blob_path": {"$ne": ""}}},
        {"$group": {"_id": None, "b": {"$sum": "$blob_size"}}}]))
    return int(agg[0]["b"]) if agg else 0


def expire_artifact(conn, artifact_id: int) -> None:
    """TTL passed: drop the blob reference; keep the record AND the thumbnail
    -- see db_sql.expire_artifact."""
    conn.db["artifacts"].update_one({"_id": int(artifact_id)},
                                    {"$set": {"blob_path": ""}})


def has_legacy_thumb_column(conn) -> bool:
    return False          # the legacy column is a SQLite-only artefact of 0.6


def legacy_thumb_backlog(conn) -> int:
    return 0


def migrate_thumbs_batch(conn, batch: int = 500) -> int:
    return 0


def _live_first(cursor):
    """Newest live artifact wins; expired variants rank last so a dead TTL'd
    variant cannot shadow a live one for the same source. When every variant
    is expired the newest still resolves, preserving 410 resource_expired.
    Callers must have ordered the cursor by created_at DESC already."""
    docs = list(cursor)
    if not docs:
        return None
    n = now()
    for d in docs:
        exp = d.get("expires_at")
        if not exp or exp > n:
            return d
    return docs[0]


def thumb_for(conn, sha: str, profile: str = None):
    q: Dict[str, Any] = {"source_sha": sha}
    if profile:
        q["profile_hash"] = profile
    art = _live_first(conn.db["artifacts"].find(
        q, {"blob_sha": 1, "source_sha": 1, "expires_at": 1}
        ).sort("created_at", -1).limit(8))
    if not art:
        return None
    t = conn.db["thumbs"].find_one({"_id": art["_id"]})
    return {"thumb": (bytes(t["jpeg"]) if t and t.get("jpeg") is not None else None),
            "blob_sha": art.get("blob_sha"), "source_sha": art.get("source_sha"),
            "expires_at": art.get("expires_at"), "id": art["_id"]}


def blob_ref(conn, sha: str, profile: str = None):
    q: Dict[str, Any] = {"source_sha": sha}
    if profile:
        q["profile_hash"] = profile
    doc = _live_first(conn.db["artifacts"].find(
        q, {"blob_path": 1, "blob_sha": 1, "mime": 1, "source_sha": 1,
            "expires_at": 1}).sort("created_at", -1).limit(8))
    return _row(doc, "id")


def artifact_for_sha(conn, sha: str, profile: str = None):
    """The artifact a sha-level read resolves to (live-first), with the
    fields a manual-region edit needs."""
    q: Dict[str, Any] = {"source_sha": sha}
    if profile:
        q["profile_hash"] = profile
    doc = _live_first(conn.db["artifacts"].find(q).sort("created_at", -1).limit(8))
    return _row(doc, "id")


def apply_manual_regions(conn, artifact_id: int, *, regions: List[dict],
                         blob_sha: str, blob_size: int, thumb: bytes = None):
    """Persist operator-drawn regions plus the re-encoded blob's identity, and
    clear needs_review -- a human has looked at this artifact now."""
    conn.db["artifacts"].update_one(
        {"_id": int(artifact_id)},
        {"$set": {"manual_regions": regions, "blob_sha": blob_sha,
                  "blob_size": int(blob_size), "needs_review": 0}})
    if thumb:
        conn.db["thumbs"].update_one(
            {"_id": int(artifact_id)}, {"$set": {"jpeg": thumb}}, upsert=True)


def blob_ref_by_code(conn, code: str, profile: str = None, tenant: str = None,
                     with_thumb: bool = False):
    """The consumer hot path: external_id -> blob.

    Two indexed lookups where SQL does one join. Denormalising the blob path
    onto the code document would save the second, but that copy would have to
    be rewritten whenever a profile changes -- and this lookup is by `_id`.
    """
    q: Dict[str, Any] = {"external_id": code}
    if tenant is not None:
        q["tenant"] = tenant
    cdocs = list(conn.db["codes"].find(q, {"source_sha": 1, "profile_hash": 1}))
    if not cdocs:
        return None
    aq: Dict[str, Any] = {"source_sha": {"$in": [d["source_sha"] for d in cdocs]}}
    if profile:
        aq["profile_hash"] = profile
    else:
        # A code names the processing it was submitted with; bindings that
        # predate the column carry no profile_hash and fall back to live-first.
        bound = [d["profile_hash"] for d in cdocs if d.get("profile_hash")]
        if bound:
            aq["profile_hash"] = {"$in": bound}
    art = _live_first(conn.db["artifacts"].find(
        aq, {"blob_path": 1, "blob_sha": 1, "mime": 1, "source_sha": 1,
             "expires_at": 1}).sort("created_at", -1).limit(8))
    if not art:
        return None
    out = _row(art)
    out["id"] = art["_id"]
    out["thumb"] = None
    if with_thumb:
        t = conn.db["thumbs"].find_one({"_id": art["_id"]})
        if t and t.get("jpeg") is not None:
            out["thumb"] = bytes(t["jpeg"])
    return out


# --- listing ------------------------------------------------------------------

def _scope_filter(scope) -> dict:
    """The mongo form of Scope.sql().

    Every constraint must be satisfied by labels owned by the SAME tenant, so
    it is one `$elemMatch` over the projection -- the structural equivalent of
    the SQL side ANDing `tenant=?` into each EXISTS.
    """
    if scope is None or scope.is_global:
        return {}
    inner: Dict[str, Any] = {"tenant": scope.tenant}
    if scope.tags:
        inner["tags"] = {"$all": sorted(scope.tags)}
    if scope.metadata:
        inner["mk"] = {"$all": [{"$elemMatch": {"k": k, "v": v}}
                                for k, v in sorted(scope.metadata.items())]}
    if len(inner) == 1:          # defensive: never degrade to allow-all
        return {"_id": {"$exists": False}}
    return {"lbl": {"$elemMatch": inner}}


def query_artifacts(conn, *, tag=None, meta=None, sha=None, needs_review=None,
                    profile_hash=None, since=None, until=None, code=None,
                    scope=None, limit=50, offset=0, sort="created",
                    direction="desc", cursor=None) -> Dict[str, Any]:
    """Filtered listing: one indexed query over one collection, which is the
    whole reason the labels are denormalised onto the artifact."""
    q: Dict[str, Any] = {}
    and_terms: List[dict] = []

    if sha:
        q["source_sha"] = {"$regex": "^" + _escape(sha)}
    if needs_review is not None:
        q["needs_review"] = 1 if needs_review else 0
    if profile_hash:
        q["profile_hash"] = profile_hash
    if since or until:
        rng: Dict[str, Any] = {}
        if since:
            rng["$gte"] = since
        if until:
            rng["$lte"] = until
        q["created_at"] = rng

    tenant = scope.tenant if scope is not None and not scope.is_global else None

    if code:
        pattern: Any = ({"$regex": "^" + _escape(str(code)[:-1])}
                        if str(code).endswith("*") else str(code))
        and_terms.append(
            {"lbl": {"$elemMatch": {"tenant": tenant, "codes": pattern}}}
            if tenant else {"lbl.codes": pattern})

    # Scoped: the label must belong to the caller's own tenant. Unscoped: any
    # tenant may supply it, which is what the SQL EXISTS does too.
    for t in (tag or []):
        and_terms.append({"lbl": {"$elemMatch": {"tenant": tenant, "tags": t}}}
                         if tenant else {"lbl.tags": t})
    for k, v in (meta or {}).items():
        and_terms.append(
            {"lbl": {"$elemMatch": {"tenant": tenant,
                                    "mk": {"$elemMatch": {"k": k, "v": v}}}}}
            if tenant else {"lbl.mk": {"$elemMatch": {"k": k, "v": v}}})

    sf = _scope_filter(scope)
    if sf:
        and_terms.append(sf)
    if and_terms:
        q["$and"] = and_terms

    if sort not in SORTS:
        sort = "created"
    field = _SORT_FIELD[sort]
    desc = str(direction).lower() != "asc"
    order = -1 if desc else 1

    # Counting is capped and only done on the first page, for the reasons
    # db_sql.py documents: an exact count over a filtered million-row set is a
    # full scan on every page view, to render a number nobody reads.
    total, capped = None, False
    if not cursor:
        counted = conn.db["artifacts"].count_documents(q, limit=COUNT_CAP + 1)
        total = min(counted, COUNT_CAP)
        capped = counted > COUNT_CAP

    page_q = q
    if cursor:
        try:
            cv, cid = decode_cursor(cursor)
        except ValueError:
            raise ValueError("bad cursor")
        op = "$lt" if desc else "$gt"
        page_q = {"$and": [q, {"$or": [{field: {op: cv}},
                                       {field: cv, "_id": {op: cid}}]}]}
        offset = 0

    docs = list(conn.db["artifacts"]
                .find(page_q, _NO_HEAVY)
                .sort([(field, order), ("_id", order)])
                .skip(int(offset)).limit(int(limit) + 1))
    has_more = len(docs) > int(limit)
    docs = docs[:int(limit)]

    rows = []
    for d in docs:
        row = _row(d, "id")
        # The image fields the SQL side gets from its join.
        row.update(row.pop("img", None) or {})
        rows.append(row)
    next_cursor = (encode_cursor(rows[-1][field], rows[-1]["id"])
                   if has_more and rows else None)
    return {"total": total, "total_capped": capped, "rows": rows,
            "next_cursor": next_cursor, "has_more": has_more,
            "sort": sort, "direction": "desc" if desc else "asc"}


def scope_allows_sha(conn, sha: str, scope) -> bool:
    """Does this scope's tenant hold a qualifying label on that image?

    Read from the canonical `labels`, not from `artifacts.lbl`: an image can be
    labelled before any artifact exists, and an authorisation check that
    consults a cache is an authorisation check that can be stale.
    """
    if scope is None or scope.is_global:
        return True
    if not (scope.tags or scope.metadata):
        return False             # defensive: never degrade to allow-all
    doc = conn.db["labels"].find_one({"source_sha": sha, "tenant": scope.tenant})
    if not doc:
        return False
    tags = set(doc.get("tags", []))
    metadata = doc.get("meta", {})
    if any(t not in tags for t in scope.tags):
        return False
    return all(metadata.get(k) == v for k, v in scope.metadata.items())


# --- api keys -----------------------------------------------------------------

def insert_api_key(conn, key_id: str, name: str, prefix: str, key_sha: str,
                   scope_json: Optional[str]) -> None:
    conn.db["api_keys"].insert_one(
        {"_id": key_id, "name": name, "prefix": prefix, "key_sha": key_sha,
         "scope_json": scope_json, "created_at": now(),
         "last_used": None, "revoked_at": None})


def find_api_key_by_hash(conn, key_sha: str):
    return _row(conn.db["api_keys"].find_one({"key_sha": key_sha,
                                              "revoked_at": None}), "id")


def touch_api_key(conn, key_id: str) -> None:
    conn.db["api_keys"].update_one({"_id": key_id}, {"$set": {"last_used": now()}})


def revoke_api_key(conn, key_id: str) -> int:
    res = conn.db["api_keys"].update_one({"_id": key_id, "revoked_at": None},
                                         {"$set": {"revoked_at": now()}})
    return res.modified_count


def all_api_keys(conn):
    return [_row(d, "id") for d in
            conn.db["api_keys"].find({}).sort([("created_at", -1)])]


def insert_feedback(conn, fb_id: str, kind: str, message: str, context: str,
                    reporter: str, version: str, ip: str) -> bool:
    """cli-feedback-spec: _id is the idempotency key; a duplicate submit is a
    success, not an error. Returns True when a doc was actually inserted."""
    try:
        conn.db["feedback"].insert_one(
            {"_id": fb_id, "version": version, "kind": kind, "message": message,
             "context": context, "reporter": reporter, "ip": ip,
             "created_at": now()})
        return True
    except Exception as exc:
        if type(exc).__name__ == "DuplicateKeyError":
            return False
        raise


def list_feedback(conn, limit: int = 50):
    return [_row(d, "id") for d in
            conn.db["feedback"].find(
                {}, {"ip": 0}).sort([("created_at", -1)]).limit(limit)]


# --- external ids -------------------------------------------------------------

def find_code(conn, tenant: str, external_id: str):
    return _row(conn.db["codes"].find_one({"_id": _code_id(tenant, external_id)}))


def find_code_any_tenant(conn, external_id: str):
    return [{"tenant": d["tenant"], "source_sha": d["source_sha"]}
            for d in conn.db["codes"].find({"external_id": external_id})]


def insert_code(conn, tenant: str, external_id: str, sha: str,
                profile_hash: str = None) -> None:
    ts = now()
    conn.db["codes"].insert_one(
        {"_id": _code_id(tenant, external_id), "tenant": tenant,
         "external_id": external_id, "source_sha": sha,
         "profile_hash": profile_hash,
         "first_seen": ts, "last_seen": ts})
    _sync_labels(conn, sha)


def touch_code(conn, tenant: str, external_id: str,
               profile_hash: str = None) -> None:
    upd = {"last_seen": now()}
    if profile_hash:
        upd["profile_hash"] = profile_hash
    conn.db["codes"].update_one({"_id": _code_id(tenant, external_id)},
                                {"$set": upd})


def repoint_code(conn, tenant: str, external_id: str, sha: str,
                 profile_hash: str = None) -> None:
    prev = conn.db["codes"].find_one({"_id": _code_id(tenant, external_id)})
    conn.db["codes"].update_one(
        {"_id": _code_id(tenant, external_id)},
        {"$set": {"source_sha": sha, "profile_hash": profile_hash,
                  "last_seen": now()}})
    # Both projections change: the old image loses the code, the new one gains
    # it. Forgetting the first is how a code goes on matching its old image.
    if prev and prev.get("source_sha") and prev["source_sha"] != sha:
        _sync_labels(conn, prev["source_sha"])
    _sync_labels(conn, sha)


# --- jobs ---------------------------------------------------------------------

def _with_sort_key(doc: dict) -> dict:
    d = doc.get("duration_ms")
    doc["duration_sort"] = -1 if d is None else d
    return doc


def insert_job(conn, row: dict) -> None:
    doc = dict(row)
    doc["_id"] = doc.pop("id")
    doc.setdefault("attempts", 0)
    doc.setdefault("error_json", None)
    conn.db["jobs"].insert_one(_with_sort_key(doc))


def find_job(conn, job_id: str):
    return _row(conn.db["jobs"].find_one({"_id": job_id}), "id")


def delete_job(conn, job_id: str) -> None:
    conn.db["jobs"].delete_one({"_id": job_id})


def orphaned_jobs(conn, statuses, live_ids: List[str]):
    """Unfinished jobs whose owner is no longer alive. A job is only orphaned
    once its owner has stopped heartbeating -- otherwise a restarting replica
    seizes work another replica is actively running."""
    q: Dict[str, Any] = {"status": {"$in": list(statuses)}}
    if live_ids:
        q["$or"] = [{"owner_id": None}, {"owner_id": {"$nin": list(live_ids)}}]
    return [{"id": d["_id"], "source_kind": d.get("source_kind"),
             "owner_id": d.get("owner_id")}
            for d in conn.db["jobs"].find(q, {"source_kind": 1, "owner_id": 1})]


def claim_job(conn, job_id: str, owner_id: str) -> None:
    conn.db["jobs"].update_one({"_id": job_id}, {"$set": {"owner_id": owner_id}})


def requeue_job(conn, job_id: str, status: str, owner_id: str = None) -> None:
    conn.db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {"status": status, "started_at": None, "owner_id": owner_id}})


def mark_job_running(conn, job_id: str, status: str) -> None:
    conn.db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {"status": status, "started_at": now()},
         "$inc": {"attempts": 1}})


def mark_job_done(conn, job_id: str, status: str, sha: str, artifact_id: int,
                  cached: bool, duration_ms: float) -> None:
    conn.db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {"status": status, "source_sha": sha,
                  "artifact_id": artifact_id, "cached": 1 if cached else 0,
                  "finished_at": now(), "duration_ms": duration_ms,
                  "duration_sort": -1 if duration_ms is None else duration_ms,
                  "error_json": None}})


def mark_job_failed(conn, job_id: str, status: str, error_json: str,
                    duration_ms: float = None) -> None:
    conn.db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {"status": status, "error_json": error_json,
                  "finished_at": now(), "duration_ms": duration_ms,
                  "duration_sort": -1 if duration_ms is None else duration_ms}})


def job_status_counts(conn, tenant: str = None) -> Dict[str, int]:
    pipeline: List[dict] = []
    if tenant:
        pipeline.append({"$match": {"tenant": tenant}})
    pipeline.append({"$group": {"_id": "$status", "n": {"$sum": 1}}})
    return {d["_id"]: d["n"] for d in conn.db["jobs"].aggregate(pipeline)}


def query_jobs(conn, *, status=None, code=None, sha=None, tag=None,
               since=None, until=None, tenant=None,
               sort="created", direction="desc", cursor=None,
               limit=50, offset=0) -> Dict[str, Any]:
    q: Dict[str, Any] = {}
    if status:
        q["status"] = status
    if code:
        q["external_id"] = ({"$regex": "^" + _escape(str(code)[:-1])}
                            if str(code).endswith("*") else code)
    if sha:
        q["source_sha"] = {"$regex": "^" + _escape(str(sha))}
    if tag:
        # Same snapshot-LIKE semantics as the SQL backend -- see
        # db_sql.query_jobs.
        q["tags_json"] = {"$regex": '"' + _escape(str(tag)) + '"'}
    if since or until:
        rng: Dict[str, Any] = {}
        if since:
            rng["$gte"] = since
        if until:
            rng["$lte"] = until
        q["created_at"] = rng
    if tenant:
        q["tenant"] = tenant

    field = _JOB_SORT_FIELD.get(sort, "created_at")
    desc = str(direction).lower() != "asc"
    order = -1 if desc else 1

    counted = None
    if not cursor:
        counted = conn.db["jobs"].count_documents(q, limit=COUNT_CAP + 1)

    page_q = q
    if cursor:
        cv, cid = decode_cursor(cursor)
        op = "$lt" if desc else "$gt"
        page_q = {"$and": [q, {"$or": [{field: {op: cv}},
                                       {field: cv, "_id": {op: cid}}]}]}
        offset = 0

    docs = list(conn.db["jobs"].find(page_q)
                .sort([(field, order), ("_id", order)])
                .skip(int(offset)).limit(int(limit) + 1))
    has_more = len(docs) > int(limit)
    docs = docs[:int(limit)]
    rows = []
    for d in docs:
        row = _row(d, "id")
        row["_sortval"] = row.get(field)
        rows.append(row)
    next_cursor = (encode_cursor(rows[-1]["_sortval"], rows[-1]["id"])
                   if has_more and rows else None)
    return {"total": None if counted is None else min(counted, COUNT_CAP),
            "total_capped": bool(counted is not None and counted > COUNT_CAP),
            "rows": rows, "next_cursor": next_cursor, "has_more": has_more,
            "sort": sort, "direction": "desc" if desc else "asc"}


# --- live instances -----------------------------------------------------------

def register_instance(conn, instance_id: str, version: str,
                      storage_backend: str) -> None:
    import os as _os
    import socket
    ts = now()
    conn.db["instances"].update_one(
        {"_id": instance_id},
        {"$set": {"last_seen": ts},
         "$setOnInsert": {"host": socket.gethostname(), "pid": _os.getpid(),
                          "version": version, "storage_backend": storage_backend,
                          "started_at": ts}},
        upsert=True)


def heartbeat_instance(conn, instance_id: str) -> None:
    conn.db["instances"].update_one({"_id": instance_id},
                                    {"$set": {"last_seen": now()}})


def unregister_instance(conn, instance_id: str) -> None:
    conn.db["instances"].delete_one({"_id": instance_id})


def live_instances(conn, exclude: str = None) -> List[dict]:
    """Instances that have heartbeated recently. A stale row is a process that
    died without cleaning up, and must not block a restart."""
    import datetime as _dt

    from .db_sql import INSTANCE_STALE_SECONDS
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(seconds=INSTANCE_STALE_SECONDS))
    cutoff_s = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = conn.db["instances"].find(
        {"last_seen": {"$gte": cutoff_s}}).sort([("started_at", 1)])
    return [_row(d, "id") for d in rows if d["_id"] != exclude]


# --- audit --------------------------------------------------------------------

def audit(conn, actor: str, action: str, target: str = None,
          source_ip: str = None, detail: Dict[str, Any] = None) -> None:
    conn.db["audit"].insert_one(
        {"seq": _next_id(conn, "audit"), "at": now(), "actor": actor,
         "action": action, "target": target, "source_ip": source_ip,
         "detail_json": json.dumps(detail or {}, sort_keys=True)})


def audit_list(conn, limit: int = 50) -> List[dict]:
    return [{"at": d["at"], "actor": d["actor"], "action": d["action"],
             "target": d.get("target"), "source_ip": d.get("source_ip"),
             "detail": json.loads(d.get("detail_json") or "{}")}
            for d in conn.db["audit"].find({}).sort([("seq", -1)]).limit(int(limit))]


AUDIT_RETENTION_DAYS = 30


def audit_prune(conn, days: int = AUDIT_RETENTION_DAYS) -> int:
    import datetime as _dt
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=days)).isoformat(timespec="seconds")
    return conn.db["audit"].delete_many({"at": {"$lt": cutoff}}).deleted_count


# --- public read rules ---------------------------------------------------------

def public_rule_add(conn, rule_id: str, name: str, tenant: str = None,
                    tag: str = None, meta_key: str = None,
                    meta_value: str = None) -> dict:
    conn.db["public_rules"].insert_one(
        {"_id": rule_id, "name": name, "tenant": tenant, "tag": tag,
         "meta_key": meta_key, "meta_value": meta_value,
         "created_at": now()})
    return {"id": rule_id, "name": name, "tenant": tenant, "tag": tag,
            "meta_key": meta_key, "meta_value": meta_value}


def public_rule_del(conn, rule_id: str) -> bool:
    return conn.db["public_rules"].delete_one({"_id": rule_id}).deleted_count > 0


def public_rules(conn) -> List[dict]:
    return [_row(d) for d in conn.db["public_rules"].find({}).sort(
        [("created_at", 1)])]


def image_is_public(conn, sha: str) -> bool:
    """See db_sql.image_is_public: read-time evaluation so deleting a rule
    revokes on the next request."""
    for r in conn.db["public_rules"].find({}):
        tq: Dict[str, Any] = {"source_sha": sha}
        if r.get("tenant"):
            tq["tenant"] = r["tenant"]
        if r.get("tag"):
            q = dict(tq); q["tags"] = r["tag"]
            if conn.db["labels"].find_one(q):
                return True
        if r.get("meta_key"):
            q = dict(tq); q[f"meta.{r['meta_key']}"] = r.get("meta_value")
            if conn.db["labels"].find_one(q):
                return True
    return False


# --- stats --------------------------------------------------------------------

_STATS_CACHE: Dict[str, Any] = {}
STATS_TTL = 3.0


def stats(conn, scope=None) -> Dict[str, Any]:
    """Header counters, memoised for a few seconds -- see db_sql.stats."""
    key = "global" if scope is None or scope.is_global else scope.tenant
    hit = _STATS_CACHE.get(key)
    if hit and time.time() - hit[0] < STATS_TTL:
        return dict(hit[1],
                    cached_for_seconds=round(STATS_TTL - (time.time() - hit[0]), 1))
    value = _stats_uncached(conn, scope)
    _STATS_CACHE[key] = (time.time(), value)
    return value


def _stats_uncached(conn, scope=None) -> Dict[str, Any]:
    db = conn.db
    unrestricted = scope is None or scope.is_global
    match = {} if unrestricted else _scope_filter(scope)

    group: Dict[str, Any] = {
        "_id": None, "artifacts": {"$sum": 1},
        "faces": {"$sum": "$n_faces"}, "plates": {"$sum": "$n_plates"},
        "bytes_stored": {"$sum": "$blob_size"},
        "bytes_live": {"$sum": {"$cond": [{"$ne": ["$blob_path", ""]},
                                        "$blob_size", 0]}},
        "needs_review": {"$sum": "$needs_review"}}
    if not unrestricted:
        # Only the scoped view needs distinct images; the unrestricted one has
        # a one-document-per-image collection to count instead.
        group["shas"] = {"$addToSet": "$source_sha"}
    pipeline = ([{"$match": match}] if match else []) + [{"$group": group}]
    agg = list(db["artifacts"].aggregate(pipeline))
    row = agg[0] if agg else {}
    totals = {"artifacts": row.get("artifacts", 0),
              "faces": row.get("faces", 0), "plates": row.get("plates", 0),
              "bytes_stored": row.get("bytes_stored", 0),
              "needs_review": row.get("needs_review", 0)}

    if unrestricted:
        return {"scope": "unrestricted",
                "images": db["images"].estimated_document_count(),
                **totals, "bytes_live": row.get("bytes_live", 0),
                "codes": db["codes"].estimated_document_count(),
                "jobs": job_status_counts(conn),
                "tags": _top_tags(conn, None)}

    # A scoped key must not learn the size of the instance it shares.
    tenant = scope.tenant
    return {"scope": scope.describe(),
            "images": len(row.get("shas", [])),
            **totals,
            "codes": db["codes"].count_documents({"tenant": tenant}),
            "jobs": job_status_counts(conn, tenant),
            "tags": _top_tags(conn, tenant)}


def _top_tags(conn, tenant: Optional[str]) -> List[dict]:
    pipeline: List[dict] = []
    if tenant:
        pipeline.append({"$match": {"tenant": tenant}})
    pipeline += [{"$unwind": "$tags"},
                 {"$group": {"_id": "$tags", "n": {"$sum": 1}}},
                 {"$sort": {"n": -1}},
                 {"$limit": 50}]
    return [{"tag": d["_id"], "n": d["n"]}
            for d in conn.db["labels"].aggregate(pipeline)]
