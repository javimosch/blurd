# The PostgreSQL backend

`BLURD_DB_BACKEND=sqlite|postgres`. Both pass the same 112 conformance checks;
the backend is meant to be invisible in behaviour.

```bash
export BLURD_DB_BACKEND=postgres
export BLURD_DB_DSN="postgresql://blurd:secret@postgres:5432/blurd"
```

`psycopg` is an **optional dependency** — only the Postgres backend needs it, and
the SQLite profile installs nothing extra.

## How it is put together

`db.py` owns every statement (`tests/seam_check.py`), and those statements are
written to be accepted by both engines rather than translated:

| instead of | it writes | why |
|---|---|---|
| `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` | SQLite ≥ 3.24 and Postgres both take it |
| `cur.lastrowid` | `RETURNING id` | SQLite ≥ 3.35 and Postgres both take it |
| `FROM (SELECT …)` | `FROM (SELECT …) AS capped` | Postgres requires the alias |
| `rowid` | `jobs.id` | a cursor tiebreaker only has to be unique and deterministic, never monotonic |

What genuinely differs lives in `src/dialect.py`: the placeholder style
(`?` → `%s`), connecting, running the schema script, introspection, and
`VACUUM` — which Postgres refuses inside a transaction. `dialect.py` may touch
the *engine* (PRAGMA, `sqlite_master`, `information_schema`) but never a blurd
table; the seam check enforces that distinction, because the moment it does,
business logic has leaked into the driver layer.

## Two schemas, checked against each other

`spec/schema.sql` is the reference; `spec/schema.postgres.sql` is its
counterpart. `tests/schema_drift.py` asserts they declare the same tables,
columns and indexes — two hand-maintained schemas diverge silently otherwise,
and the failure surfaces as a query that works on one backend and not the other.

Deliberate choices in the translation:

- **Timestamps stay `TEXT`** (RFC3339, UTC). Keeping them textual means stored
  values, comparisons and cursors are byte-identical across engines, and the
  portable SQL needs no date handling at all.
- **Flags stay `INTEGER`** (`needs_review`, `cached`, `force`) rather than
  becoming `BOOLEAN`, so the same statements and values work on both.
- **`REAL` becomes `DOUBLE PRECISION`, not Postgres `REAL`.** See below.

## The bug worth remembering

SQLite's `REAL` is 8-byte IEEE. Postgres's `REAL` is **4-byte**. Translating the
name rather than the meaning silently halved the precision — and it broke keyset
pagination in a way that looked like a logic error:

```
page 1 ends at duration_ms = 695.48
cursor carries 695.48 (float8)
page 2 predicate: COALESCE(duration_ms,-1) < 695.48  ->  TRUE for that same row
```

The stored float4 promotes to `695.47998…`, which is less than the float8
`695.48` the cursor carried. So the boundary row was excluded by neither the
`<` branch nor the `=` branch, and appeared on both pages. Nothing about the
symptom pointed at a type.

Only running against a real Postgres finds this. `tests/schema_drift.py` now
fails specifically if a SQLite `REAL` column is anything but `DOUBLE PRECISION`
on the Postgres side.

Related: `duration_ms` is NULL for a job that has not finished, and NULL
ordering is not portable either — SQLite treats NULL as smallest (DESC puts them
last), Postgres defaults to NULLS FIRST on DESC. A cursor value of NULL compares
as NULL, so the page after it comes back empty. The job sort is therefore an
*expression*, `COALESCE(duration_ms, -1)`, selected under an alias.

## Not done yet

- **Connection pooling.** Both dialects keep one connection per thread. N
  replicas × M workers against a default `max_connections` of 100 is an outage
  waiting to happen. This is step 2 in `spec/distributed.md`, along with job
  leases, the migration lock and graceful drain.
- **No migration path from SQLite to Postgres.** A fresh Postgres database
  starts at the current schema; the 0.2–0.5 migrations only ever ran against
  SQLite files and are skipped. Moving an existing instance means exporting and
  reimporting, which is not written.
- **Still one replica.** The instance guard only refuses a second process on a
  *SQLite* home. Postgres makes several replicas possible, but the six
  correctness items in step 2 have to land before it is safe.
