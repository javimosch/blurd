# The distributed path: S3 + a shared metadata store

Scope for running blurd as more than one replica behind a load balancer, on
Docker or k8s. Written after the S3 work (`spec/storage.md`) landed, and before
the metadata store is chosen.

## Target

```
            ┌── blurd replica 1 ──┐
  LB ───────┼── blurd replica 2 ──┼──── object store  (redacted blobs)
            └── blurd replica 3 ──┘└──── metadata store (Postgres | Mongo)
```

No shared queue. **Each replica processes what it accepts**, and any replica can
serve any result, because both stores are shared. The load balancer already
distributes submissions; a distributed queue would only add work-stealing,
fleet-wide backpressure and cross-node retry — useful, not required.

This matters because it removes the thing that looked like the blocker. Upload
payloads live in the memory of the replica that received them (source images are
never written to disk), so a job cannot migrate to a peer. That makes uploads
*sticky*, which is not the same as *unscalable*: a sticky upload still only needs
its own replica to be alive for the few hundred milliseconds it takes.

**The line that stays fixed:** pending source bytes never go to shared storage.
That would make jobs migratable and would also make blurd a service that stores
originals. It is the one guarantee the product is built on.

## Where we are

S3 removed ~95% of the stateful footprint (~360 GB per million images → ~19 GB).
It did **not** enable a second replica: SQLite is a single-writer file. Two pods
on one PVC is silent corruption, not a degraded mode.

## Decision: PostgreSQL

Taken 2026-09-17. The schema is already relational and written, the port is
mechanical rather than a rewrite, and `SELECT … FOR UPDATE SKIP LOCKED` means
one component answers both the database and the queue question. Mongo was a
legitimate alternative — its denormalisation genuinely removes the `EXISTS`
filters that cost 7 ms — and lost on sequencing, not capability.

## Step 0 — close the metadata seam (before choosing anything)

> **Status: done** (0.9.0). All 40 statements moved; `db.py` is the only module
> that executes SQL, guarded by `tests/seam_check.py`. 112/112 conformance,
> zero behaviour change.

The S3 work taught this: swapping a backend is easy, *finding every caller* is
the job. Blob paths were built in 5 modules; metadata is worse.

| file | before | after |
|---|---:|---:|
| `jobs.py` | 19 | 0 |
| `client.py` | 9 | 0 |
| `auth.py` | 5 | 0 |
| `pipeline.py` | 4 | 0 |
| `main.py` | 2 | 0 |
| `server.py` | 1 | 0 |
| **total** | **40** | **0** |

They became ~25 named functions in `db.py` — `insert_job`, `mark_job_done`,
`find_code`, `release_tenant_labels`, `blob_ref_by_code`, `query_jobs` and so
on. The two that were more than a move: the job listing (dynamic SQL assembled
in `client.py`) became `db.query_jobs`, mirroring `query_artifacts`; and the
by-code blob join (assembled in `server.py`) became `db.blob_ref_by_code`.

`scope.py` is the one deliberate exception: it still *builds* WHERE fragments,
because the predicate is the scope's own logic, but it no longer executes them
(`db.scope_allows_sha` does). Those fragments hard-code SQLite's `?`
placeholder — **the one remaining backend coupling**, and the first thing step 1
has to parameterise.

Until those 40 move behind the same interface, "pluggable metadata store" means
"pluggable for the queries someone remembered". This step is backend-agnostic,
testable on its own against the existing 112 checks, and is the only part that
must happen regardless of which database wins.

Shape: mirror `store.py`. A `MetadataStore` with the ~25 methods `db.py` already
exposes, `build(cfg)` selecting by `BLURD_DB_BACKEND`, and nothing outside it
holding a cursor.

## Step 1 — the backend

### Postgres — **implemented in 0.10.0**

112/112 conformance on both backends. Details, and the two portability bugs it
turned up, in `spec/postgres.md`.

| | |
|---|---|
| Port surface | **~22 mechanical substitutions**: `PRAGMA`, `executescript`, `AUTOINCREMENT` → `GENERATED … AS IDENTITY`, `lastrowid` → `RETURNING id`, `rowid` → a real column, `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING` |
| Schema | `spec/schema.sql` stays the contract; foreign keys and `ON DELETE CASCADE` keep working |
| Queue | `SELECT … FOR UPDATE SKIP LOCKED` is a correct job queue for free — **one component answers both questions** |
| Listing | the `EXISTS` filters stay as written; keyset pagination is unchanged |
| Cost | another engine to operate if the shop runs Mongo |

### MongoDB

| | |
|---|---|
| Port surface | **rewrite `db.py`'s 504 lines** against a different data model — not a translation |
| Schema | fold `tags` / `metadata` / `external_ids` into the artifact document; multikey indexes |
| Listing | genuinely *better*: the denormalisation removes the `EXISTS` subqueries that cost 7 ms on a metadata filter |
| Cascades | no foreign keys — deleting an image becomes application-level cleanup across collections |
| Uniqueness | `(tenant, external_id)` and `(source_sha, profile_hash)` become unique compound indexes; the conflict logic must read integrity errors instead of pre-checking |
| Transactions | multi-document needs a replica set; blurd's writes are small and mostly single-document once denormalised |
| Queue | still to solve separately (`findAndModify` polling is fine at 13 img/s, but it is extra work) |
| Cost | none operationally if Mongo is already run in-house |

**Recommendation: Postgres**, because the schema is already relational and
written, and because `SKIP LOCKED` answers the queue question that Mongo leaves
open. **But** if the house runs Mongo well, the denormalised document model is a
legitimate fit and the listing queries get simpler, not harder. This is a
sequencing and operations decision more than a technical one.

> **Both were built** — Postgres in 0.11.0, MongoDB in 0.14.0. The
> recommendation above stands: pick Postgres unless you already run Mongo. What
> changed is that "already run Mongo" is now a supported answer rather than a
> roadmap item.
>
> The estimate in this table was close but wrong in one direction. Mongo was
> *not* a rewrite of `db.py`; it was a new module beside it, because the seam
> `tests/seam_check.py` guards had already confined every SQL statement to one
> file. The listing did get simpler. The cascade did become manual. The measured
> outcome is in `spec/mongo.md`: 113/113 conformance on all three backends, and
> 77/77 on a cross-backend parity suite that pages through every listing on both
> and compares.

## Step 2 — what breaks with more than one replica

> **Status: done** (0.11.0). Verified with three replicas on one Postgres and
> one MinIO: 113/113 conformance through a load balancer, surviving a SIGKILL.
> Details and what is still open in `spec/replicas.md`.

None of these was visible before, because there had only ever been one process.

1. **Crash recovery steals live jobs.** `JobQueue._recover()` requeues every
   `queued`/`running` job at startup. With peers, a restarting replica would
   seize jobs another replica is actively running. Needs an `instance_id` on the
   job, a lease with a heartbeat, and a reaper that only reclaims jobs whose
   lease has expired.
2. **Concurrent schema migration.** `db.init()` migrates on startup. Three
   replicas starting together would run it simultaneously. Needs an advisory
   lock (`pg_advisory_lock`) or a separate migration Job that runs to completion
   before the Deployment rolls.
3. **`external_id` binding races.** Two replicas binding the same code at once
   must be arbitrated by the unique index, and the integrity error handled as a
   conflict — not pre-checked and assumed.
4. **Connection handling.** `db.connect()` keeps one SQLite connection per
   thread. Postgres/Mongo need a pool with a bounded size, or N replicas ×
   M workers exhausts `max_connections`.
5. **Per-process caches drift.** The 3-second stats memo, the detector cache and
   the legacy-thumb flag are per-process. Harmless, but header counters will
   differ slightly between replicas.
6. **Graceful shutdown.** SIGTERM currently stops the server; in-flight jobs in
   memory are lost and the producer must resubmit. For k8s this wants a drain:
   stop accepting, finish what is running, then exit inside
   `terminationGracePeriodSeconds`.

## Step 3 — k8s specifics

- **Readiness ≠ liveness.** Readiness must fail when the object store or the
  database is unreachable, so the replica leaves the LB pool. Liveness must
  *not* — restarting a pod does not fix a dead database, it just adds churn.
  `/v1/health` is unauthenticated by design, which is what lets the kubelet use
  it without a key.
- **Models.** ~8 MB of weights, deliberately not baked into the image (licence,
  and rebuild-as-redistribution). `BLURD_PULL_MODELS=1` fetches on boot; an
  init container writing to a shared volume is better once there are replicas.
- **Autoscaling** on queue depth (`/v1/stats` → `queue.queued`), not CPU. CPU
  sits high by design; a backlog that never returns to zero is the real signal.
- **Config** comes from env (`config.ENV_OVERRIDES`); secrets stay out of the
  config file and are read from the environment at point of use.

## What does not change

- `spec/openapi.yaml` — the wire contract is untouched.
- `tests/conformance.py` — 112 black-box checks against a binary and a URL, so
  it is already the acceptance test for every backend. "Done" is defined.
- The storage key layout and `profile_hash` — unchanged, so no data is rewritten.
- The local profile. SQLite + filesystem stays the dev and single-container
  story; it is a supported profile, not a legacy one.

## Effort, honestly

| step | size | can ship alone |
|---|---|---|
| 0 — close the metadata seam | **done**; 40 call sites, no behaviour change | yes |
| 1 — Postgres backend | **done** (0.10.0); see `spec/postgres.md` | yes |
| 2 — leases, migration lock, connection bounding, drain | **done** (0.11.0); see `spec/replicas.md` | no, needed together for >1 replica |
| 3 — k8s manifests | mostly configuration | yes |

Steps 0 and 1 are worth doing even if the fleet never grows past one replica:
they are what makes the database a *service* rather than a file on a volume,
which is what rolling deploys need.

## The reason to leave SQLite

Not throughput. One replica handles **1.16 M images/day**. It is availability: a
single-replica StatefulSet has an outage on every node drain and every rolling
deploy. Worth naming, because it changes what to optimise — if availability is
the driver, two replicas suffice and the distributed queue never enters the
picture.
