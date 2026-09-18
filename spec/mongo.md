# The MongoDB backend

blurd stores metadata in one of three places: SQLite (default), Postgres, or
MongoDB. All three pass the same 113 conformance checks and the 77-check
cross-backend parity suite. This document is about the third one, because it is
the only one that is not a schema port.

## Why a second shared backend at all

Postgres already answers the "several replicas share one metadata store"
question, and answers it well. Mongo is here for a narrower reason: **if the
house already runs Mongo, adding Postgres is a new engine to operate** — backups,
failover, upgrades, someone on call for it. A team that runs Mongo well should
be able to deploy blurd without also becoming a Postgres shop.

That is the whole argument. It is not a performance claim, and blurd does not
recommend Mongo over Postgres on technical grounds.

## The model is denormalised, deliberately

A row-for-row port would keep `images`, `tags`, `metadata`, `external_ids` and
`artifacts` as five collections, and then need `$lookup` on every listing. That
fails for a specific reason rather than a stylistic one:

> **A `$lookup` cannot use an index to satisfy the sort.** Once the join is in
> the pipeline, ordering by `created_at` becomes a blocking in-memory sort over
> the whole matched set. Keyset pagination — the thing that makes the dashboard
> survive a million artifacts — stops working, and does so silently: every page
> still looks well-formed.

So the artifact document carries what a listing needs:

```
artifacts {
  _id: 41,                       // int, not ObjectId: it is public API
  source_sha, profile_hash, blob_path, blob_size, mime,
  n_faces, n_plates, needs_review, created_at,
  detections: [ {cls, x, y, w, h, score, detector}, ... ],
  img:  { width, height, byte_size, source_kind, source_ref },
  lbl:  [ { tenant: "t_ab12…",
            tags:  ["acme", "2026"],
            mk:    [ {k: "appId", v: "acme"} ],   // attribute pattern
            codes: ["IMG_0042.jpg"] } ]
}
```

A filtered, scoped, sorted, cursor-paginated page is then **one indexed query
over one collection** — structurally the same plan the SQL side gets, without
the join.

`lbl` and `img` are caches. The canonical records live in `labels`, `codes` and
`images`, and `_sync_labels()` is the only function that writes `lbl`. Every
label mutation ends by calling it, so there is no second path to forget.

### What that costs

**Write amplification.** Changing a tag rewrites that image's artifacts. It is
bounded — an image has as many artifacts as it has redaction profiles, usually
one — and labels are written once per submission but read on every page of every
listing. That is the right side of the trade for this workload, and it would be
the wrong side for one that re-tags constantly.

**No foreign keys.** `delete_image` performs the cascade by hand, across five
collections. Missing one leaves a label pointing at an image that no longer
exists: exactly the class of bug a foreign key exists to prevent. It is written
in one place and nowhere else.

**Authorisation does not read the cache.** `scope_allows_sha` consults
`labels`, not `artifacts.lbl`, because an image can be labelled before any
artifact exists — and an authorisation check that reads a cache is one that can
be stale.

### Metadata as an attribute pattern

Metadata keys are caller-supplied, so `{"meta": {"appId": "acme"}}` would
need an index per unknown key. Stored instead as `mk: [{k, v}]`, one compound
index on `lbl.mk.k, lbl.mk.v` serves every key. The query form is
`$elemMatch`, which keeps `k` and `v` bound to the *same* array element —
without it, `{"mk.k": "appId", "mk.v": "acme"}` would match a document that
has some other key with the value "acme".

### Scope filters are a single `$elemMatch`

A scoped key requires that *the same tenant* holds every constraint. That is one
`$elemMatch` over `lbl`, which is the structural equivalent of the SQL side
ANDing `tenant=?` into each `EXISTS`. Splitting it into independent conditions
would let two different tenants' labels jointly satisfy a scope — a
cross-tenant read, produced by a query that looks correct.

## Where the equivalences are exact

| concern | SQL | Mongo |
|---|---|---|
| code uniqueness | `PRIMARY KEY (tenant, external_id)` | `_id` = tenant + NUL + code |
| artifact id | `INTEGER PRIMARY KEY` | `counters` collection, `$inc` |
| keyset cursor | `(sort_col, a.id)` | `(sort field, _id)`, same encoding |
| capped count | `SELECT 1 … LIMIT 10001` | `count_documents(q, limit=10001)` |
| null sort key | `COALESCE(duration_ms, -1)` | stored field `duration_sort` |
| migration lock | `pg_advisory_lock` | a lease document in `locks` |
| out-of-scope read | 404, never 403 | identical |

`duration_sort` deserves its line. `duration_ms` is null until a job finishes,
and a keyset cursor whose value is null compares as null — `duration_ms < null`
is never true — so the page after that boundary comes back empty. The SQL side
coalesces in the ORDER BY; Mongo stores the coalesced value, which is the same
fix and keeps the sort index-backed.

## What Mongo does not do

- **No transactions.** blurd writes one document at a time and never needs a
  multi-document transaction. A standalone `mongod` could not offer one anyway —
  they require a replica set. `commit()` and `rollback()` are no-ops rather than
  errors, so the backend choice does not leak back into the call sites.
- **No `vacuum`.** Mongo reclaims space itself. `blurd vacuum` refuses on this
  backend rather than running nothing and reporting success.
- **No queue.** Postgres offers `SELECT … FOR UPDATE SKIP LOCKED`, which is a
  real work queue if blurd ever needs cross-replica work-stealing. Mongo's
  `findAndModify` can be made to do it, but it is not as good a fit. Today
  neither is used: each replica works what it accepts (spec/replicas.md).

## Running it

```bash
docker compose --profile mongo up -d mongo

export BLURD_DB_BACKEND=mongo
export BLURD_DB_DSN='mongodb://blurd:blurd-dev-secret@127.0.0.1:27017/?authSource=admin'
export BLURD_DB_DATABASE=blurd
./blurd-venv serve --port 8770
```

`pymongo` is an optional dependency: it lives in the venv (`./blurd-venv`) and
in images built with `--build-arg WITH_MONGO=1`. A SQLite-only deployment is not
made to install it.

## Testing it

```bash
python3 tests/seam_check.py          # SQL confined to db_sql.py; backend parity
python3 tests/conformance.py …       # the same 113 checks, against any backend
python3 tests/backend_parity.py …    # 77 checks: two backends, same answers
python3 tests/multi_replica.py       # ownership and reaping on a shared store
```

`backend_parity.py` is the one that earns its keep here. It pages through every
listing under every sort and both directions on both backends, and asserts the
union of pages is exactly the full set, with nothing seen twice and nothing
missed. A cursor that is not totally ordered produces a boundary row that
matches neither `<` nor `=`, so it lands on both pages or on neither — and
every page still looks perfectly well-formed on its own. Nothing else in the
suite would notice.

It compares by **content-derived keys** — the sha, or the caller's own code —
never by ids, which are minted per instance and are not comparable across two.
