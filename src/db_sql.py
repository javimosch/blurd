"""SQL access (SQLite and Postgres). Plain SQL against spec/schema.sql -- no
ORM, deliberately, so the Go/machin ports can reuse the exact same statements.

This is one of two metadata backends. `db.py` is the facade that dispatches to
this module or to `db_mongo.py`; nothing outside those three imports either
implementation directly."""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# The dialect owns connecting, placeholders, the schema script and
# introspection. Every statement below is written once, in `?` style, and works
# on both engines -- see src/dialect.py.
_DIALECT = {}


def dialect(cfg=None):
    from . import dialect as _d
    if cfg is not None:
        _DIALECT["d"] = _d.build(cfg)
    if "d" not in _DIALECT:
        from .config import Config
        _DIALECT["d"] = _d.build(Config())
    return _DIALECT["d"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(db_file: Path = None):
    """`db_file` is accepted for call-site compatibility; which database is
    opened is the dialect's business, not the caller's."""
    return dialect().connect()


def init(db_file: Path) -> None:
    """Migration runs BEFORE the schema script, not after.

    schema.sql declares `CREATE INDEX ... ON tags(tenant, tag)`, and on a
    pre-0.4 database that column does not exist yet -- so running the script
    first fails on exactly the column the migration is there to add.
    """
    d = dialect()
    conn = d.connect()
    # Serialised across replicas: three pods starting together would otherwise
    # migrate and apply the schema at the same moment.
    with d.migration_lock(conn):
        if _tables(conn):
            migrate(conn)
        conn.executescript(d.schema_sql())
        conn.commit()


def _tables(conn) -> set:
    return dialect().tables(conn)


def _columns(conn, table: str) -> set:
    return dialect().columns(conn, table)


def migrate(conn) -> None:
    """Bring a pre-0.4 database up to the tenant-aware schema.

    `CREATE TABLE IF NOT EXISTS` cannot widen a PRIMARY KEY, and tags,
    metadata and external_ids all need the tenant in theirs, so those three are
    rebuilt. Existing rows belong to the operator, i.e. tenant 'global'.
    """
    tables = _tables(conn)
    if "api_keys" in tables and "scope_json" not in _columns(conn, "api_keys"):
        conn.execute("ALTER TABLE api_keys ADD COLUMN scope_json TEXT")
    if "jobs" in tables and "tenant" not in _columns(conn, "jobs"):
        conn.execute("ALTER TABLE jobs ADD COLUMN tenant TEXT NOT NULL DEFAULT 'global'")
    if "jobs" in tables and "force" not in _columns(conn, "jobs"):
        conn.execute("ALTER TABLE jobs ADD COLUMN force INTEGER NOT NULL DEFAULT 0")
    if "jobs" in tables and "owner_id" not in _columns(conn, "jobs"):
        conn.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT")
    if "artifacts" in tables and "expires_at" not in _columns(conn, "artifacts"):
        conn.execute("ALTER TABLE artifacts ADD COLUMN expires_at TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_expires "
                     "ON artifacts(expires_at)")
    if "external_ids" in tables and "profile_hash" not in _columns(conn, "external_ids"):
        conn.execute("ALTER TABLE external_ids ADD COLUMN profile_hash TEXT")

    # The thumbnail move is LAZY, on purpose. Copying every blob into the new
    # table and dropping the column rewrites the whole of `artifacts` in one
    # transaction: on a 900 MB database that ran for minutes and grew the file
    # to 1.7 GB with a 1 GB WAL alongside it, because WAL holds the old pages
    # until the transaction commits. A startup migration must not do that.
    #
    # Instead: new thumbnails go to `thumbs`, reads fall back to the legacy
    # column while it exists, and `blurd migrate-thumbs` moves the backlog in
    # resumable batches when the operator chooses.
    if "tags" not in tables or "tenant" in _columns(conn, "tags"):
        conn.commit()
        return
    if not dialect().supports_legacy_migrations():
        # The rebuild below is SQLite executescript; a Postgres database never
        # predates the tenant columns, so reaching this means nothing to do.
        conn.commit()
        return

    dialect().set_foreign_keys(conn, False)
    conn.execute("BEGIN")
    try:
        conn.executescript("""
            CREATE TABLE tags_new (
              source_sha TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
              tenant TEXT NOT NULL DEFAULT 'global',
              tag TEXT NOT NULL,
              PRIMARY KEY (source_sha, tenant, tag));
            INSERT INTO tags_new (source_sha, tenant, tag)
              SELECT source_sha, 'global', tag FROM tags;
            DROP TABLE tags;
            ALTER TABLE tags_new RENAME TO tags;

            CREATE TABLE metadata_new (
              source_sha TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
              tenant TEXT NOT NULL DEFAULT 'global',
              key TEXT NOT NULL, value TEXT NOT NULL,
              PRIMARY KEY (source_sha, tenant, key));
            INSERT INTO metadata_new (source_sha, tenant, key, value)
              SELECT source_sha, 'global', key, value FROM metadata;
            DROP TABLE metadata;
            ALTER TABLE metadata_new RENAME TO metadata;

            CREATE TABLE external_ids_new (
              tenant TEXT NOT NULL DEFAULT 'global',
              external_id TEXT NOT NULL,
              source_sha TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
              first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
              PRIMARY KEY (tenant, external_id));
            INSERT INTO external_ids_new (tenant, external_id, source_sha, first_seen, last_seen)
              SELECT 'global', external_id, source_sha, first_seen, last_seen FROM external_ids;
            DROP TABLE external_ids;
            ALTER TABLE external_ids_new RENAME TO external_ids;

            CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tenant, tag);
            CREATE INDEX IF NOT EXISTS idx_metadata_kv ON metadata(tenant, key, value);
            CREATE INDEX IF NOT EXISTS idx_external_sha ON external_ids(source_sha);
        """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        dialect().set_foreign_keys(conn, True)


# --- images / tags / metadata -------------------------------------------------

def upsert_image(conn, sha: str, byte_size: int, width: int, height: int,
                 mime: str, kind: str, ref: Optional[str]) -> None:
    ts = now()
    conn.execute(
        """INSERT INTO images (source_sha, byte_size, width, height, mime,
                               source_kind, source_ref, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_sha) DO UPDATE SET last_seen=excluded.last_seen""",
        (sha, byte_size, width, height, mime, kind, ref, ts, ts),
    )


def merge_tags(conn, sha: str, tags: List[str], tenant: str = "global") -> None:
    for t in {str(t).strip() for t in (tags or []) if str(t).strip()}:
        conn.execute(
            "INSERT INTO tags (source_sha, tenant, tag) VALUES (?,?,?) "
            "ON CONFLICT DO NOTHING",
            (sha, tenant, t))


def merge_metadata(conn, sha: str, meta: Dict[str, Any], tenant: str = "global") -> None:
    for k, v in (meta or {}).items():
        conn.execute(
            """INSERT INTO metadata (source_sha, tenant, key, value) VALUES (?,?,?,?)
               ON CONFLICT(source_sha, tenant, key) DO UPDATE SET value=excluded.value""",
            (sha, tenant, str(k), v if isinstance(v, str) else json.dumps(v)),
        )


# A scoped reader sees only its own tenant's labels. The image row is shared
# because identical bytes are stored once; the labels on it are not.
def tags_of(conn, sha: str, tenant: str = None) -> List[str]:
    if tenant is None:
        return [r["tag"] for r in conn.execute(
            "SELECT DISTINCT tag FROM tags WHERE source_sha=? ORDER BY tag", (sha,))]
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM tags WHERE source_sha=? AND tenant=? ORDER BY tag",
        (sha, tenant))]


def codes_of(conn, sha: str, tenant: str = None) -> List[str]:
    if tenant is None:
        return [r["external_id"] for r in conn.execute(
            "SELECT DISTINCT external_id FROM external_ids WHERE source_sha=? "
            "ORDER BY external_id", (sha,))]
    return [r["external_id"] for r in conn.execute(
        "SELECT external_id FROM external_ids WHERE source_sha=? AND tenant=? "
        "ORDER BY external_id", (sha, tenant))]


def codes_detail(conn, sha: str) -> List[dict]:
    """Operator view: which tenant owns each code on this image."""
    return [{"tenant": r["tenant"], "external_id": r["external_id"]}
            for r in conn.execute(
                "SELECT tenant, external_id FROM external_ids WHERE source_sha=? "
                "ORDER BY tenant, external_id", (sha,))]


def metadata_of(conn, sha: str, tenant: str = None) -> Dict[str, str]:
    if tenant is None:
        return {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM metadata WHERE source_sha=?", (sha,))}
    return {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM metadata WHERE source_sha=? AND tenant=?",
        (sha, tenant))}


# --- artifacts ----------------------------------------------------------------

def find_artifact(conn, sha: str, profile_hash: str):
    return conn.execute(
        "SELECT * FROM artifacts WHERE source_sha=? AND profile_hash=?",
        (sha, profile_hash)).fetchone()


def insert_artifact(conn, row: dict, dets: List[dict]) -> int:
    thumb = row.pop("thumb", None)
    aid = conn.execute(
        """INSERT INTO artifacts (source_sha, profile_hash, profile_json, blob_path,
             blob_sha, blob_size, mime, n_faces, n_plates, min_score, needs_review,
             stats_json, created_at, expires_at)
           VALUES (:source_sha,:profile_hash,:profile_json,:blob_path,:blob_sha,
                   :blob_size,:mime,:n_faces,:n_plates,:min_score,:needs_review,
                   :stats_json,:created_at,:expires_at)
           RETURNING id""", row).fetchone()["id"]
    if thumb:
        conn.execute("INSERT INTO thumbs (artifact_id, jpeg) VALUES (?,?) "
                     "ON CONFLICT (artifact_id) DO UPDATE SET jpeg=excluded.jpeg",
                     (aid, thumb))
    for d in dets:
        x, y, w, h = d["box"]
        conn.execute(
            """INSERT INTO detections (artifact_id, cls, x, y, w, h, score, detector)
               VALUES (?,?,?,?,?,?,?,?)""",
            (aid, d["cls"], x, y, w, h, d["score"], d["detector"]))
    return aid


def detections_of(conn, artifact_id: int) -> List[dict]:
    return [{"cls": r["cls"], "box": [r["x"], r["y"], r["w"], r["h"]],
             "score": r["score"], "detector": r["detector"]}
            for r in conn.execute(
                "SELECT * FROM detections WHERE artifact_id=?", (artifact_id,))]


# Sortable columns, with the tiebreaker that makes a cursor stable. Only these
# are offered, because each one needs an index to avoid a temp b-tree sort.
SORTS = {
    "created":  "a.created_at",
    "faces":    "a.n_faces",
    "plates":   "a.n_plates",
    "size":     "a.blob_size",
    "review":   "a.needs_review",
}
# `width` lives on `images`, so sorting by it forces the join into a temp
# b-tree: 179 ms against 4 ms for every other option at 50k rows. Offering a
# sort that is 40x slower than its neighbours is a trap, so it is not offered.
COUNT_CAP = 10000


def encode_cursor(sort_value, row_id) -> str:
    raw = json.dumps([sort_value, row_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str):
    if not cursor:
        return None
    pad = "=" * (-len(cursor) % 4)
    try:
        value, row_id = json.loads(base64.urlsafe_b64decode(cursor + pad))
        # The tiebreaker is an int for artifacts (a real id) and a string for
        # jobs (a token). It only has to be unique and deterministic, never
        # monotonic, so both are fine -- do not coerce it.
        return value, row_id
    except Exception:
        raise ValueError("bad cursor")


def bulk_labels(conn, shas: List[str], tenant: str = None) -> Dict[str, dict]:
    """Load tags, metadata and codes for a whole page in 3 queries.

    The obvious per-row helpers (`tags_of`, `metadata_of`, `codes_of`) turn one
    page into 3N queries -- 602 of them for a 200-row page. They are fine for a
    single record and wrong for a listing.
    """
    out = {sha: {"tags": [], "metadata": {}, "codes": []} for sha in shas}
    if not shas:
        return out
    marks = ",".join("?" * len(shas))
    tfilter = " AND tenant=?" if tenant else ""
    extra = [tenant] if tenant else []

    for r in conn.execute(
            f"SELECT source_sha, tag FROM tags WHERE source_sha IN ({marks})"
            f"{tfilter} ORDER BY tag", shas + extra):
        out[r["source_sha"]]["tags"].append(r["tag"])
    for r in conn.execute(
            f"SELECT source_sha, key, value FROM metadata WHERE source_sha IN ({marks})"
            f"{tfilter}", shas + extra):
        out[r["source_sha"]]["metadata"][r["key"]] = r["value"]
    for r in conn.execute(
            f"SELECT source_sha, external_id FROM external_ids "
            f"WHERE source_sha IN ({marks}){tfilter} ORDER BY external_id",
            shas + extra):
        out[r["source_sha"]]["codes"].append(r["external_id"])
    return out


def facets(conn, tenant: str = "global", limit: int = 100) -> Dict[str, Any]:
    """Distinct tags and metadata key=values with usage counts, for the
    dashboard's clickable filter chips. Counts are over source_sha, not
    artifacts -- a reprocessed image still counts once."""
    tq = "WHERE tenant=?" if tenant else ""
    ex = [tenant] if tenant else []
    tags = [{"tag": r["tag"], "n": r["n"]} for r in conn.execute(
        f"SELECT tag, COUNT(DISTINCT source_sha) n FROM tags {tq} "
        f"GROUP BY tag ORDER BY n DESC, tag LIMIT ?", ex + [limit])]
    meta: Dict[str, list] = {}
    for r in conn.execute(
            f"SELECT key, value, COUNT(DISTINCT source_sha) n FROM metadata {tq} "
            f"GROUP BY key, value ORDER BY n DESC, key, value LIMIT ?",
            ex + [limit]):
        meta.setdefault(r["key"], []).append({"value": r["value"], "n": r["n"]})
    return {"tags": tags, "meta": meta}


_LEGACY_THUMB: Dict[int, bool] = {}


def has_legacy_thumb_column(conn) -> bool:
    """Cached per connection, keyed by id(): a sqlite3.Connection is a C type
    with no __dict__, so the flag cannot hang off the object itself."""
    key = id(conn)
    if key not in _LEGACY_THUMB:
        _LEGACY_THUMB[key] = "thumb" in _columns(conn, "artifacts")
    return _LEGACY_THUMB[key]


def legacy_thumb_backlog(conn) -> int:
    if not has_legacy_thumb_column(conn):
        return 0
    return conn.execute(
        "SELECT COUNT(*) c FROM artifacts a WHERE a.thumb IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM thumbs t WHERE t.artifact_id = a.id)").fetchone()["c"]


def migrate_thumbs_batch(conn, batch: int = 500) -> int:
    """Move one batch of legacy thumbnails. Returns how many moved."""
    if not has_legacy_thumb_column(conn):
        return 0
    rows = conn.execute(
        "SELECT a.id, a.thumb FROM artifacts a WHERE a.thumb IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM thumbs t WHERE t.artifact_id = a.id) LIMIT ?",
        (int(batch),)).fetchall()
    if not rows:
        return 0
    conn.executemany("INSERT INTO thumbs (artifact_id, jpeg) VALUES (?,?) "
                     "ON CONFLICT (artifact_id) DO UPDATE SET jpeg=excluded.jpeg",
                     [(r["id"], r["thumb"]) for r in rows])
    conn.executemany("UPDATE artifacts SET thumb=NULL WHERE id=?",
                     [(r["id"],) for r in rows])
    conn.commit()
    return len(rows)


def _live_first(sql: str, args: list):
    """Resolve to the newest LIVE artifact; expired ones rank last so a dead
    TTL'd variant can't shadow a live one for the same source. When every
    variant is expired the newest still resolves, preserving the 410
    `resource_expired` contract instead of collapsing into a 404."""
    return (sql + " ORDER BY (a.expires_at IS NULL OR a.expires_at > ?) DESC,"
                  " a.created_at DESC LIMIT 1", args + [now()])


def thumb_for(conn, sha: str, profile: str = None):
    """Just the thumbnail bytes, by one indexed lookup.

    Serving a thumbnail used to go through the full record builder -- 7 queries
    and a discarded JSON document per tile, 24 times a page."""
    # COALESCE so a database mid-migration serves thumbnails from either place.
    col = ("COALESCE(t.jpeg, a.thumb)" if has_legacy_thumb_column(conn) else "t.jpeg")
    sql = (f"SELECT {col} AS thumb, a.id, a.blob_sha, a.source_sha, a.expires_at FROM artifacts a "
           "LEFT JOIN thumbs t ON t.artifact_id = a.id WHERE a.source_sha=?")
    args = [sha]
    if profile:
        sql += " AND a.profile_hash=?"
        args.append(profile)
    sql, args = _live_first(sql, args)
    return conn.execute(sql, args).fetchone()


def blob_ref(conn, sha: str, profile: str = None):
    """Blob path + etag without building a record."""
    sql = ("SELECT a.id, a.blob_path, a.blob_sha, a.mime, a.source_sha, a.expires_at FROM artifacts a "
           "WHERE a.source_sha=?")
    args = [sha]
    if profile:
        sql += " AND a.profile_hash=?"
        args.append(profile)
    sql, args = _live_first(sql, args)
    return conn.execute(sql, args).fetchone()


def query_artifacts(conn, *, tag=None, meta=None, sha=None, needs_review=None,
                    profile_hash=None, since=None, until=None, code=None,
                    scope=None, limit=50, offset=0, sort="created",
                    direction="desc", cursor=None) -> Dict[str, Any]:
    """Filtered listing. Tag and metadata filters are ANDed via EXISTS
    subqueries so that multiple tags mean 'has all of them'."""
    where, params = ["1=1"], []
    if sha:
        where.append("a.source_sha LIKE ?")
        params.append(str(sha) + "%")
    if code:
        # Exact match on the indexed primary key, or a prefix scan if the caller
        # explicitly asks for one with a trailing '*'.
        if str(code).endswith("*"):
            where.append("EXISTS (SELECT 1 FROM external_ids e "
                         "WHERE e.source_sha=a.source_sha AND e.external_id LIKE ?"
                         + (" AND e.tenant=?" if _code_tenant(scope) else "") + ")")
            params.append(str(code)[:-1] + "%")
            if _code_tenant(scope):
                params.append(_code_tenant(scope))
        else:
            where.append("EXISTS (SELECT 1 FROM external_ids e "
                         "WHERE e.source_sha=a.source_sha AND e.external_id=?"
                         + (" AND e.tenant=?" if _code_tenant(scope) else "") + ")")
            params.append(str(code))
            if _code_tenant(scope):
                params.append(_code_tenant(scope))
    if needs_review is not None:
        where.append("a.needs_review = ?")
        params.append(1 if needs_review else 0)
    if profile_hash:
        where.append("a.profile_hash = ?")
        params.append(profile_hash)
    if since:
        where.append("a.created_at >= ?")
        params.append(since)
    if until:
        where.append("a.created_at <= ?")
        params.append(until)
    tenant = scope.tenant if scope is not None and not scope.is_global else None
    for t in (tag or []):
        if tenant:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.source_sha=a.source_sha "
                         "AND t.tenant=? AND t.tag=?)")
            params += [tenant, t]
        else:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.source_sha=a.source_sha "
                         "AND t.tag=?)")
            params.append(t)
    for k, v in (meta or {}).items():
        if tenant:
            where.append("EXISTS (SELECT 1 FROM metadata m WHERE m.source_sha=a.source_sha "
                         "AND m.tenant=? AND m.key=? AND m.value=?)")
            params += [tenant, k, v]
        else:
            where.append("EXISTS (SELECT 1 FROM metadata m WHERE m.source_sha=a.source_sha "
                         "AND m.key=? AND m.value=?)")
            params += [k, v]

    if scope is not None and not scope.is_global:
        sw, sp = scope.sql("a.source_sha")
        where += sw
        params += sp

    if sort not in SORTS:
        sort = "created"
    col = SORTS[sort]
    desc = str(direction).lower() != "asc"
    order = "DESC" if desc else "ASC"
    clause = " AND ".join(where)
    joins = ("FROM artifacts a JOIN images i ON i.source_sha = a.source_sha")

    # Counting is capped. An exact COUNT(*) over a filtered million-row set is a
    # full scan on every page view, to render a number nobody reads past the
    # first few digits. Stop at COUNT_CAP and say "10,000+".
    # Count over `artifacts` alone -- the images join is only needed for the
    # width sort and for fields in the rendered page, never for counting.
    # Count only on the first page. Paging deeper does not change the total, so
    # recomputing it on every page just pays the filter cost again -- and with a
    # selective-ish filter that count is the most expensive part of the request.
    total, capped = None, False
    if not cursor:
        counted = conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT 1 FROM artifacts a WHERE {clause} "
            f"LIMIT {COUNT_CAP + 1}) AS capped", params).fetchone()["c"]
        total = min(counted, COUNT_CAP)
        capped = counted > COUNT_CAP

    page_params = list(params)
    page_where = clause
    if cursor:
        # Keyset pagination. OFFSET is O(offset) -- it walks and discards every
        # skipped row -- and it is also WRONG under concurrent inserts, because
        # rows shift between pages and the reader sees duplicates or gaps.
        try:
            cv, cid = decode_cursor(cursor)
        except ValueError:
            raise ValueError("bad cursor")
        cmp_op = "<" if desc else ">"
        page_where += (f" AND ({col} {cmp_op} ? OR ({col} = ? AND a.id {cmp_op} ?))")
        page_params += [cv, cv, cid]
        offset = 0

    # Explicit columns, never `a.*`: on a database that still has the legacy
    # `thumb` column, `a.*` pulls a 14 kB blob for every row of every page.
    cols = ("a.id, a.source_sha, a.profile_hash, a.blob_path, a.blob_sha, a.blob_size, "
            "a.mime, a.n_faces, a.n_plates, a.min_score, a.needs_review, a.created_at, "
            "a.expires_at")
    rows = conn.execute(
        f"""SELECT {cols}, i.width, i.height, i.byte_size, i.source_kind, i.source_ref
            {joins} WHERE {page_where}
            ORDER BY {col} {order}, a.id {order} LIMIT ? OFFSET ?""",
        page_params + [int(limit) + 1, int(offset)]).fetchall()

    has_more = len(rows) > int(limit)
    rows = rows[:int(limit)]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        key = col.split(".")[-1]
        next_cursor = encode_cursor(last[key], last["id"])
    return {"total": total, "total_capped": capped, "rows": rows,
            "next_cursor": next_cursor, "has_more": has_more,
            "sort": sort, "direction": "desc" if desc else "asc"}


# --- api keys -----------------------------------------------------------------
#
# Everything below exists so that db_sql.py is the ONLY module that writes SQL.
# Before this, 40 statements were scattered across jobs/client/auth/pipeline/
# main/server -- which made "pluggable metadata store" mean "pluggable for the
# call sites someone remembered". See spec/distributed.md, step 0.

def insert_api_key(conn, key_id: str, name: str, prefix: str, key_sha: str,
                   scope_json: Optional[str]) -> None:
    conn.execute(
        """INSERT INTO api_keys (id, name, prefix, key_sha, scope_json, created_at)
           VALUES (?,?,?,?,?,?)""",
        (key_id, name, prefix, key_sha, scope_json, now()))
    conn.commit()


def find_api_key_by_hash(conn, key_sha: str):
    return conn.execute(
        "SELECT * FROM api_keys WHERE key_sha=? AND revoked_at IS NULL",
        (key_sha,)).fetchone()


def touch_api_key(conn, key_id: str) -> None:
    conn.execute("UPDATE api_keys SET last_used=? WHERE id=?", (now(), key_id))
    conn.commit()


def revoke_api_key(conn, key_id: str) -> int:
    cur = conn.execute(
        "UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now(), key_id))
    conn.commit()
    return cur.rowcount


def all_api_keys(conn):
    return conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()


def insert_feedback(conn, fb_id: str, kind: str, message: str, context: str,
                    reporter: str, version: str, ip: str) -> bool:
    """cli-feedback-spec: id is the idempotency key; a duplicate submit is a
    success, not an error. Returns True when a row was actually inserted."""
    cur = conn.execute(
        """INSERT INTO feedback (id, version, kind, message, context, reporter,
                                 ip, created_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT (id) DO NOTHING""",
        (fb_id, version, kind, message, context, reporter, ip, now()))
    conn.commit()
    return cur.rowcount > 0


def list_feedback(conn, limit: int = 50):
    return conn.execute(
        """SELECT id, version, kind, message, context, reporter, created_at
           FROM feedback ORDER BY created_at DESC LIMIT ?""",
        (limit,)).fetchall()


# --- images and artifacts -----------------------------------------------------

def touch_image(conn, sha: str) -> None:
    conn.execute("UPDATE images SET last_seen=? WHERE source_sha=?", (now(), sha))


def find_image(conn, sha: str):
    return conn.execute("SELECT * FROM images WHERE source_sha=?", (sha,)).fetchone()


def delete_image(conn, sha: str) -> None:
    conn.execute("DELETE FROM images WHERE source_sha=?", (sha,))   # cascades
    conn.commit()


def find_artifact_by_id(conn, artifact_id: int):
    return conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()


def find_artifact_by_sha_prefix(conn, sha: str):
    return conn.execute(
        """SELECT * FROM artifacts WHERE source_sha LIKE ?
           ORDER BY created_at DESC LIMIT 1""", (sha + "%",)).fetchone()


def delete_artifact(conn, artifact_id: int) -> None:
    conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))


def expired_blobs(conn, limit: int = 200) -> List[dict]:
    """Artifacts past their TTL whose object may still be in the store."""
    return conn.execute(
        "SELECT id, blob_path FROM artifacts WHERE expires_at IS NOT NULL "
        "AND expires_at < ? AND blob_path != '' LIMIT ?",
        (now(), limit)).fetchall()


def expire_artifact(conn, artifact_id: int) -> None:
    """TTL passed: drop the blob reference and thumbnail; keep the record."""
    conn.execute("UPDATE artifacts SET blob_path='' WHERE id=?", (artifact_id,))
    conn.execute("DELETE FROM thumbs WHERE artifact_id=?", (artifact_id,))


def artifact_blob_paths(conn, sha: str) -> List[str]:
    return [r["blob_path"] for r in conn.execute(
        "SELECT blob_path FROM artifacts WHERE source_sha=? AND blob_path!=''",
        (sha,))]


def all_blob_paths(conn) -> List[str]:
    return [r["blob_path"] for r in conn.execute(
        "SELECT blob_path FROM artifacts WHERE blob_path!=''")]


def release_tenant_labels(conn, sha: str, tenant: str) -> None:
    """A scoped delete drops only that tenant's claim on shared bytes."""
    conn.execute("DELETE FROM tags WHERE source_sha=? AND tenant=?", (sha, tenant))
    conn.execute("DELETE FROM metadata WHERE source_sha=? AND tenant=?", (sha, tenant))
    conn.execute("DELETE FROM external_ids WHERE source_sha=? AND tenant=?",
                 (sha, tenant))
    conn.commit()


def has_any_labels(conn, sha: str) -> bool:
    """Is this image still referenced by anybody? Decides whether the bytes go."""
    return conn.execute(
        """SELECT 1 FROM tags WHERE source_sha=? UNION ALL
           SELECT 1 FROM metadata WHERE source_sha=? UNION ALL
           SELECT 1 FROM external_ids WHERE source_sha=? LIMIT 1""",
        (sha, sha, sha)).fetchone() is not None


def blob_ref_by_code(conn, code: str, profile: str = None, tenant: str = None,
                     with_thumb: bool = False):
    """The consumer hot path: external_id -> blob, in one indexed join.

    `tenant=None` means an unscoped caller and searches across tenants."""
    tcol = ("COALESCE(t.jpeg, a.thumb)" if (with_thumb and has_legacy_thumb_column(conn))
            else "t.jpeg")
    sql = (f"""SELECT a.id, a.blob_path, a.blob_sha, a.mime, {tcol} AS thumb, a.source_sha, a.expires_at
               FROM external_ids e
               JOIN artifacts a ON a.source_sha = e.source_sha
               LEFT JOIN thumbs t ON t.artifact_id = a.id
               WHERE e.external_id = ?""")
    args = [code]
    if tenant is not None:
        sql += " AND e.tenant = ?"
        args.append(tenant)
    if profile:
        sql += " AND a.profile_hash = ?"
        args.append(profile)
    else:
        # A code names the processing it was submitted with, not any variant
        # of the source: without this, a newer expired artifact under another
        # profile can shadow (or be shadowed by) the bound one. NULL bindings
        # predate the column and keep the live-first fallback.
        sql += " AND (e.profile_hash IS NULL OR a.profile_hash = e.profile_hash)"
    sql, args = _live_first(sql, args)
    return conn.execute(sql, args).fetchone()


# --- external ids -------------------------------------------------------------

def find_code(conn, tenant: str, external_id: str):
    return conn.execute(
        "SELECT * FROM external_ids WHERE tenant=? AND external_id=?",
        (tenant, external_id)).fetchone()


def find_code_any_tenant(conn, external_id: str):
    return conn.execute(
        "SELECT tenant, source_sha FROM external_ids WHERE external_id=?",
        (external_id,)).fetchall()


def insert_code(conn, tenant: str, external_id: str, sha: str,
                profile_hash: str = None) -> None:
    ts = now()
    conn.execute(
        """INSERT INTO external_ids (tenant, external_id, source_sha,
             profile_hash, first_seen, last_seen) VALUES (?,?,?,?,?,?)""",
        (tenant, external_id, sha, profile_hash, ts, ts))


def touch_code(conn, tenant: str, external_id: str,
               profile_hash: str = None) -> None:
    conn.execute("UPDATE external_ids SET last_seen=?, profile_hash=COALESCE(?, profile_hash) "
                 "WHERE tenant=? AND external_id=?",
                 (now(), profile_hash, tenant, external_id))


def repoint_code(conn, tenant: str, external_id: str, sha: str,
                 profile_hash: str = None) -> None:
    conn.execute(
        "UPDATE external_ids SET source_sha=?, profile_hash=?, last_seen=? WHERE tenant=? AND external_id=?",
        (sha, profile_hash, now(), tenant, external_id))


# --- jobs ---------------------------------------------------------------------

def insert_job(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO jobs (id, status, external_id, on_conflict, source_kind,
             source_ref, source_sha, profile_hash, profile_json, tags_json,
             metadata_json, artifact_id, cached, tenant, force, owner_id,
             created_at, started_at, finished_at, duration_ms)
           VALUES (:id,:status,:external_id,:on_conflict,:source_kind,:source_ref,
                   :source_sha,:profile_hash,:profile_json,:tags_json,
                   :metadata_json,:artifact_id,:cached,:tenant,:force,:owner_id,
                   :created_at,:started_at,:finished_at,:duration_ms)""", row)
    conn.commit()


def find_job(conn, job_id: str):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def delete_job(conn, job_id: str) -> None:
    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()


def orphaned_jobs(conn, statuses, live_ids: List[str]):
    """Unfinished jobs whose owner is no longer alive.

    This replaces "everything unfinished at startup", which was correct with
    one process and catastrophic with peers: a restarting replica would seize
    jobs another replica was actively running. A job is only orphaned once its
    owner has stopped heartbeating.
    """
    marks = ",".join("?" * len(statuses))
    sql = f"SELECT id, source_kind, owner_id FROM jobs WHERE status IN ({marks})"
    params = list(statuses)
    if live_ids:
        sql += " AND (owner_id IS NULL OR owner_id NOT IN (%s))" % \
               ",".join("?" * len(live_ids))
        params += list(live_ids)
    return conn.execute(sql, params).fetchall()


def claim_job(conn, job_id: str, owner_id: str) -> None:
    conn.execute("UPDATE jobs SET owner_id=? WHERE id=?", (owner_id, job_id))


def requeue_job(conn, job_id: str, status: str, owner_id: str = None) -> None:
    conn.execute("UPDATE jobs SET status=?, started_at=NULL, owner_id=? WHERE id=?",
                 (status, owner_id, job_id))


def mark_job_running(conn, job_id: str, status: str) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, started_at=?, attempts=attempts+1 WHERE id=?",
        (status, now(), job_id))
    conn.commit()


def mark_job_done(conn, job_id: str, status: str, sha: str, artifact_id: int,
                  cached: bool, duration_ms: float) -> None:
    conn.execute(
        """UPDATE jobs SET status=?, source_sha=?, artifact_id=?, cached=?,
             finished_at=?, duration_ms=?, error_json=NULL WHERE id=?""",
        (status, sha, artifact_id, 1 if cached else 0, now(), duration_ms, job_id))
    conn.commit()


def mark_job_failed(conn, job_id: str, status: str, error_json: str,
                    duration_ms: float = None) -> None:
    conn.execute(
        """UPDATE jobs SET status=?, error_json=?, finished_at=?, duration_ms=?
           WHERE id=?""", (status, error_json, now(), duration_ms, job_id))


def job_status_counts(conn, tenant: str = None) -> Dict[str, int]:
    if tenant:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM jobs WHERE tenant=? GROUP BY status",
            (tenant,))
    else:
        rows = conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


# Sort EXPRESSIONS, not bare columns.
#
# `duration_ms` is NULL for a job that has not finished, and NULL ordering is
# not portable: SQLite treats NULL as smallest (so DESC puts them last),
# Postgres defaults to NULLS FIRST on DESC. Worse, a keyset cursor whose value
# is NULL compares as NULL -- `duration_ms < NULL` is never true -- so the page
# after it comes back empty or repeats. Coalescing makes the order total and
# the predicate honest on both engines.
JOB_SORTS = {"created": "created_at",
             "duration": "COALESCE(duration_ms, -1)",
             "status": "status"}


def query_jobs(conn, *, status=None, code=None, since=None, until=None, tenant=None,
               sort="created", direction="desc", cursor=None,
               limit=50, offset=0) -> Dict[str, Any]:
    """Job listing, mirroring query_artifacts: capped count on the first page
    only, keyset cursor, index-backed sorts."""
    where, params = ["1=1"], []
    if status:
        where.append("status=?"); params.append(status)
    if code:
        if str(code).endswith("*"):
            where.append("external_id LIKE ?"); params.append(str(code)[:-1] + "%")
        else:
            where.append("external_id=?"); params.append(code)
    if since:
        where.append("created_at >= ?"); params.append(since)
    if until:
        where.append("created_at <= ?"); params.append(until)
    if tenant:
        where.append("tenant=?"); params.append(tenant)

    col = JOB_SORTS.get(sort, "created_at")
    desc = str(direction).lower() != "asc"
    order = "DESC" if desc else "ASC"
    clause = " AND ".join(where)

    counted = None
    if not cursor:
        counted = conn.execute(
            f"SELECT COUNT(*) c FROM (SELECT 1 FROM jobs WHERE {clause} "
            f"LIMIT {COUNT_CAP + 1}) AS capped", params).fetchone()["c"]

    page_where, page_params = clause, list(params)
    if cursor:
        cv, cid = decode_cursor(cursor)
        op = "<" if desc else ">"
        page_where += f" AND ({col} {op} ? OR ({col} = ? AND id {op} ?))"
        page_params += [cv, cv, cid]
        offset = 0
    # The sort value is selected under an alias because it may be an
    # expression; a bare `rows[-1][col]` would not find "COALESCE(...)".
    rows = conn.execute(
        f"SELECT *, {col} AS _sortval FROM jobs WHERE {page_where} "
        f"ORDER BY _sortval {order}, id {order} LIMIT ? OFFSET ?",
        page_params + [int(limit) + 1, int(offset)]).fetchall()
    has_more = len(rows) > int(limit)
    rows = rows[:int(limit)]
    next_cursor = (encode_cursor(rows[-1]["_sortval"], rows[-1]["id"])
                   if has_more and rows else None)
    return {"total": None if counted is None else min(counted, COUNT_CAP),
            "total_capped": bool(counted is not None and counted > COUNT_CAP),
            "rows": rows, "next_cursor": next_cursor, "has_more": has_more,
            "sort": sort, "direction": "desc" if desc else "asc"}


def vacuum(conn) -> None:
    dialect().vacuum(conn)


def scope_allows_sha(conn, sha: str, scope) -> bool:
    """Does this scope's tenant hold a label on that image? The membership test
    behind every single-record read by a scoped key."""
    where, params = scope.sql("i.source_sha")
    clause = " AND ".join(where)
    return conn.execute(
        f"SELECT 1 FROM images i WHERE i.source_sha=? AND {clause}",
        [sha] + params).fetchone() is not None


# --- live instances -----------------------------------------------------------

INSTANCE_STALE_SECONDS = 45


def register_instance(conn, instance_id: str, version: str, storage_backend: str) -> None:
    import os as _os, socket
    ts = now()
    conn.execute(
        """INSERT INTO instances (id, host, pid, version, storage_backend,
             started_at, last_seen) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen""",
        (instance_id, socket.gethostname(), _os.getpid(), version,
         storage_backend, ts, ts))
    conn.commit()


def heartbeat_instance(conn, instance_id: str) -> None:
    conn.execute("UPDATE instances SET last_seen=? WHERE id=?", (now(), instance_id))
    conn.commit()


def unregister_instance(conn, instance_id: str) -> None:
    conn.execute("DELETE FROM instances WHERE id=?", (instance_id,))
    conn.commit()


def live_instances(conn, exclude: str = None) -> List[dict]:
    """Instances that have heartbeated recently. A stale row is a process that
    died without cleaning up, and must not block a restart."""
    import datetime as _dt
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(seconds=INSTANCE_STALE_SECONDS))
    cutoff_s = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = conn.execute(
        "SELECT * FROM instances WHERE last_seen >= ? ORDER BY started_at", (cutoff_s,))
    return [dict(r) for r in rows if r["id"] != exclude]


# --- audit --------------------------------------------------------------------

def audit(conn, actor: str, action: str, target: str = None,
          source_ip: str = None, detail: Dict[str, Any] = None) -> None:
    conn.execute(
        """INSERT INTO audit (at, actor, action, target, source_ip, detail_json)
           VALUES (?,?,?,?,?,?)""",
        (now(), actor, action, target, source_ip,
         json.dumps(detail or {}, sort_keys=True)))
    conn.commit()


def audit_list(conn, limit: int = 50) -> List[dict]:
    return [{"at": r["at"], "actor": r["actor"], "action": r["action"],
             "target": r["target"], "source_ip": r["source_ip"],
             "detail": json.loads(r["detail_json"] or "{}")}
            for r in conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))]


def _code_tenant(scope):
    return None if scope is None or scope.is_global else scope.tenant


_STATS_CACHE = {}
STATS_TTL = 3.0


def stats(conn, scope=None) -> Dict[str, Any]:
    """Header counters, memoised for a few seconds.

    They aggregate the whole instance, and the dashboard asks for them on every
    tab switch. A counter that is three seconds stale is indistinguishable from
    a fresh one to a human, and the query is the most expensive thing the page
    does."""
    import time as _t
    key = "global" if scope is None or scope.is_global else scope.tenant
    hit = _STATS_CACHE.get(key)
    if hit and _t.time() - hit[0] < STATS_TTL:
        return dict(hit[1], cached_for_seconds=round(STATS_TTL - (_t.time() - hit[0]), 1))
    value = _stats_uncached(conn, scope)
    _STATS_CACHE[key] = (_t.time(), value)
    return value


def _stats_uncached(conn, scope=None) -> Dict[str, Any]:
    if scope is None or scope.is_global:
        # One pass over `artifacts` for every counter it can supply, instead of
        # six separate scans. `detections` is no longer counted here at all: it
        # is the largest table and nothing in the header needed it.
        # No COUNT(DISTINCT source_sha): it cost 145 ms of the 227 here, and
        # `images` is a one-row-per-image table with a cheap PK count.
        row = conn.execute(
            """SELECT COUNT(*) artifacts,
                      COALESCE(SUM(n_faces),0) faces,
                      COALESCE(SUM(n_plates),0) plates,
                      COALESCE(SUM(blob_size),0) bytes_stored,
                      COALESCE(SUM(needs_review),0) needs_review
               FROM artifacts""").fetchone()
        return {
            "scope": "unrestricted",
            "images": conn.execute("SELECT COUNT(*) c FROM images").fetchone()["c"],
            "artifacts": row["artifacts"],
            "faces": row["faces"], "plates": row["plates"],
            "needs_review": row["needs_review"],
            "bytes_stored": row["bytes_stored"],
            "codes": conn.execute("SELECT COUNT(*) c FROM external_ids").fetchone()["c"],
            "jobs": {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM jobs GROUP BY status")},
            "tags": [dict(r) for r in conn.execute(
                "SELECT tag, COUNT(*) n FROM tags GROUP BY tag ORDER BY n DESC LIMIT 50")],
        }

    # A scoped key must not learn the size of the instance it shares.
    tenant = scope.tenant
    sw, sp = scope.sql("a.source_sha")
    clause = " AND ".join(sw)
    row = conn.execute(
        f"""SELECT COUNT(*) artifacts, COALESCE(SUM(a.n_faces),0) faces,
                   COALESCE(SUM(a.n_plates),0) plates,
                   COALESCE(SUM(a.blob_size),0) bytes_stored,
                   SUM(CASE WHEN a.needs_review=1 THEN 1 ELSE 0 END) needs_review,
                   COUNT(DISTINCT a.source_sha) images
            FROM artifacts a WHERE {clause}""", sp).fetchone()
    return {
        "scope": scope.describe(),
        "images": row["images"], "artifacts": row["artifacts"],
        "faces": row["faces"], "plates": row["plates"],
        "needs_review": row["needs_review"] or 0,
        "bytes_stored": row["bytes_stored"],
        "codes": conn.execute("SELECT COUNT(*) c FROM external_ids WHERE tenant=?",
                              (tenant,)).fetchone()["c"],
        "jobs": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM jobs WHERE tenant=? GROUP BY status",
            (tenant,))},
        "tags": [dict(r) for r in conn.execute(
            "SELECT tag, COUNT(*) n FROM tags WHERE tenant=? GROUP BY tag "
            "ORDER BY n DESC LIMIT 50", (tenant,))],
    }
