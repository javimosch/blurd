---
name: blurd-backends
description: Working across blurd's three metadata backends (SQLite, Postgres, MongoDB) and two blob backends (local, S3). Read before touching src/db.py, db_sql.py, db_mongo.py, dialect.py, store.py or any query. Covers the dispatch seam, the cross-engine SQL rules, the Mongo document model and the portability bugs that look like logic bugs.
---

# Backends

blurd has two pluggable seams. Both exist so a deployment choice never leaks
into business logic — and both are guarded, because "pluggable" silently means
"pluggable where someone remembered" otherwise.

```
metadata   src/db.py  ──▶  db_sql.py   (SQLite | Postgres, via dialect.py)
                      └─▶  db_mongo.py (MongoDB)

blobs      src/store.py ──▶ LocalStore | S3Store
```

## The dispatch seam

`db.py` is a **facade with no queries in it**. It dispatches on the *connection
object*, not on configuration — a process can hold connections to different
backends at once, and a global "which backend are we" flag would silently send
those queries to the wrong place. The connection knows what it is; ask it.

Wrappers are **generated**, not hand-written. Sixty near-identical three-line
wrappers are sixty chances to route one function to the wrong backend, and that
bug surfaces as "this one filter ignores the tenant" months later.

Two invariants, both enforced by `tests/seam_check.py` (one second, run it):

1. **`db_sql.py` is the only module that executes SQL.** `scope.py` may *build*
   WHERE fragments — the predicate is the scope's own logic — but must never
   execute them. `dialect.py` may touch engine metadata (`PRAGMA`,
   `information_schema`) but never a blurd table.
2. **`db_mongo.py` implements exactly the set `db.py` dispatches.** A backend
   missing one function fails at the call, in production, on whichever endpoint
   happens to need it.

> This seam was built for the Postgres port and looked like overhead at the
> time. Its real payoff was the **second** backend: `spec/distributed.md`
> predicted MongoDB would mean rewriting `db.py`'s 504 lines, and instead it was
> a new module beside it. Do not weaken the guard to save a line.

## Adding a query

Add it to **both** `db_sql.py` and `db_mongo.py`, with the same name, arguments
and return shape. Return plain dicts with the same keys; `_row()` in the Mongo
module exposes `_id` under whatever name the SQL side uses (`id`, `source_sha`).

If a test or a CLI path needs raw SQL, that is the signal to add a named
function instead — `multi_replica.py` learned this when its cleanup ran
`DELETE FROM jobs` and the Mongo connection refused it by design.

## Cross-engine SQL rules (SQLite ↔ Postgres)

Every statement in `db_sql.py` must run on both. Only placeholders and engine
metadata belong in `dialect.py`.

- `ON CONFLICT`, not `INSERT OR IGNORE`
- `RETURNING id`, not `lastrowid`
- alias every subquery
- `jobs.id`, never `rowid`

Two portability bugs that present as logic bugs, both worth knowing by heart:

- **A SQLite `REAL` is `DOUBLE PRECISION` in Postgres, never `REAL`.** 8-byte vs
  4-byte. Get it wrong and a keyset boundary row matches neither `<` nor `=`, so
  it appears on both pages or neither. `tests/schema_drift.py` fails on this
  specifically.
- **NULL ordering is not portable.** SQLite treats NULL as smallest; Postgres
  defaults to NULLS FIRST on DESC. Worse, a cursor value of NULL compares as
  NULL, so `duration_ms < NULL` is never true and the next page comes back
  empty. Sortable columns that can be NULL need an **expression**
  (`COALESCE(duration_ms, -1)`), not a bare column. See `JOB_SORTS`.

## The Mongo model is denormalised on purpose

Not a schema port. A five-collection translation would need `$lookup` on every
listing, and **a `$lookup` cannot use an index to satisfy the sort** — keyset
pagination over a million artifacts becomes a blocking in-memory sort, and it
fails silently because every page still looks well-formed.

So each artifact document carries its own copy of the parent image's dimensions
(`img`) and of every tenant's labels (`lbl`), making a filtered, scoped, sorted,
cursor-paginated page **one indexed query over one collection**.

What that costs, and the rules that follow:

- **`_sync_labels()` is the only writer of `artifacts.lbl`.** Every label
  mutation ends by calling it. Adding a second path is how the projection
  drifts from the canonical `labels`/`codes` collections.
- **`delete_image` cascades by hand across five collections.** There are no
  foreign keys. Missing one leaves a label pointing at a deleted image.
- **`scope_allows_sha` reads the canonical `labels`, never `artifacts.lbl`.** An
  image can be labelled before any artifact exists, and an authorisation check
  that consults a cache is one that can be stale.
- **Metadata uses the attribute pattern** (`mk: [{k, v}]`) because metadata keys
  are caller-supplied and you cannot index an unknown key. Query it with
  `$elemMatch` so `k` and `v` stay bound to the *same* array element —
  `{"mk.k": "appId", "mk.v": "acme"}` matches a document with some *other* key
  whose value is "acme".
- **A scope filter is a single `$elemMatch`.** Every constraint must be
  satisfied by the same tenant's labels. Splitting it into independent
  conditions lets two tenants' labels jointly satisfy a scope — a cross-tenant
  read, produced by a query that looks correct.

Mongo has no transactions here (blurd writes one document at a time, and a
standalone `mongod` could not offer them anyway), so `commit()`/`rollback()` are
no-ops rather than errors — making them errors would push backend knowledge back
out into the callers.

## Blob seam

**Nothing outside `src/store.py` may build a blob path.** One
`Path(blobs_dir) / rel` elsewhere breaks `local`↔`s3` interchangeability for
exactly one code path, silently. The key is identical in both backends, so
switching never rewrites `artifacts.blob_path`.

**Write the object before the database row.** An orphaned object is garbage; a
row pointing at a missing object is a broken record.

## Running against each backend

```bash
# SQLite (default)
./blurd serve --port 8770

# Postgres / Mongo need ./blurd-venv: the drivers are optional deps in the venv
docker compose --profile pg up -d postgres
BLURD_DB_BACKEND=postgres \
BLURD_DB_DSN=postgresql://blurd:blurd-dev-secret@127.0.0.1:5432/blurd \
  ./blurd-venv serve --port 8770

docker compose --profile mongo up -d mongo
BLURD_DB_BACKEND=mongo \
BLURD_DB_DSN='mongodb://blurd:blurd-dev-secret@127.0.0.1:27017/?authSource=admin' \
BLURD_DB_DATABASE=blurd \
  ./blurd-venv serve --port 8770
```

A container cannot run `blurd config set` before it starts, and a running daemon
reads its config once — so **anything a deployment must set needs an entry in
`config.ENV_OVERRIDES`**.

## Before you call a backend change done

```bash
python3 tests/seam_check.py      # SQL confined; backend parity
python3 tests/schema_drift.py    # the two SQL schemas still agree
# conformance on all three, and backend_parity across two — see blurd-testing
```

**Known gap:** there is no data migration between backends. A new deployment
picks its backend at the start; an existing SQLite instance cannot move its data
to Postgres or Mongo.
