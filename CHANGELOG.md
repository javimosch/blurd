# Changelog

blurd was developed privately and is published here from 0.16.0. This is the
condensed history — it keeps the decisions and the measurements, because several
of them are the reason the code looks the way it does.

## 0.19.0

**TTL profile binding.** `external_ids` now records the `profile_hash` a code
was submitted with, so `/v1/blobs/by-code/` resolves to that exact artifact
instead of "newest artifact for the sha". Previously a newer expired TTL
variant could shadow a live default-profile artifact (or the reverse) and
by-code fetches returned the wrong expiry status. Sha-level reads still
prefer live artifacts over expired variants; when every variant is expired
the newest still resolves, preserving the 410 `resource_expired` contract.
Existing bindings (NULL `profile_hash`) keep the live-first fallback. Additive
migration: `ALTER TABLE external_ids ADD COLUMN profile_hash`.

**Input size guard.** `fetch.max_bytes` now defaults to 5 MB (was 25 MB) and
applies to all three input paths -- url fetch, `blur <file>`, and raw
`POST /v1/images` uploads, which were previously bounded only by the 64 MB
body cap. Oversized input fails at submit with 422 `validation_error`, never
in the worker. Configurable via `blurd config set fetch.max_bytes` or
`BLURD_FETCH_MAX_BYTES`. The default was fitted to a 12-month production
sample (1.23 M images: p99 633 KB, max 1.43 MB, zero files over 5 MB).

**Broken-image rejection at submit.** Raw uploads whose bytes don't sniff to
a known image type (jpeg/png/webp/bmp/tiff magic) are refused with 422
`validation_error` instead of queueing and failing in the worker where only
a poll reveals it. Real prod data contains sub-10-byte "images" (broken
uploads); they now fail synchronously. Truncated files with valid magic still
fail typed at decode -- that check needs the decoder and stays in the worker.

**Dashboard filter chips.** `GET /ui-api/facets` feeds clickable tag and
metadata key=value chips in the dashboard; tag pills on cards filter too.

## 0.18.0

**Ephemeral outputs: `--ttl` / `profile.storage.ttl`.**

For deployments where the caller keeps the redacted file (the geored
integration writes it back to its own media store), blurd no longer has to be
the permanent store. `blurd blur img.jpg --ttl 86400` — or
`{"storage":{"ttl":N}}` in the profile overrides — records `expires_at` on the
artifact; the reaper sweep then deletes the blob and thumbnail, and the read
path prunes lazily so a slow sweep never serves stale bytes. The record,
codes, tags and detections survive: a late fetch answers **410
`resource_expired`** rather than 404, so a poller can tell "missed the window,
resubmit" from "never existed". TTL is part of `profile_hash`, so an expiring
output can never cache-collide with a permanent one; `expires_at` counts from
processing, not submission, so queue time does not eat the blob's life. Bounds:
60 s–30 days. The test sidecar exposes the field. New conformance section:
submit → fetch → expiry → 410 → record survives → resubmit revives.

## 0.17.0

**Portable API keys, and the CLI conformance specs end to end.**

Keys were only ever minted per instance, which meant a second deployment could
not reuse the `blk_` credentials already baked into running apps. `key_sha` is
portable even though plaintext is not, so `keys export` now dumps
`{id, name, prefix, key_sha, scope}` for active keys — never plaintext, never
revoked, `last_used` stays local — and `keys import` re-homes them
idempotently: existing hashes skip, `id` collisions remap, revoked keys do not
resurrect. `keys add --key` covers the one-off case where the plaintext is
still known. Import works across backends (SQLite → Mongo verified) and over
`kubectl exec` without a shell.

The CLI now passes `cli-spec-conformance` 28/28 (was 8/26). `help-json` is a
command alongside `--help-json`; argparse errors became typed `invalid_argument`
bodies exiting 85 instead of exit-2 usage text; `guide` JSON follows the
agent-skill schema; `daemon start|stop|status` is a command group with
idempotent start and no-op stop; `/_health` and `/_shutdown` (loopback-only)
join `/v1/health`. Two deliberate deviations: `/_shutdown` is loopback-only
rather than token-gated — strictly tighter — and `help-json`/`guide` emit
their documents at top level, outside the `data` envelope.

`blurd feedback "<msg>"` dual-writes a submission to `POST /v1/feedback` and a
central relay under one client-generated `id`, never fails the caller, and
works without an API key. The endpoint is open for intake (16 KB, 30/min/IP);
reads require an operator key.

Verified: conformance 126/126 on SQLite, Postgres and MongoDB; seam, schema
drift and Helm guardrails clean; key export/import verified between isolated
homes and cross-backend; feedback relay smoke-tested live.

## 0.16.0

**A Helm chart, a non-root image, and a stale-artifact bug.**

Proving the deployment paths rather than asserting them turned up two defects,
both only reachable through a real container.

A `done` job could report `result: null` for an image that was still there:
`?force=1` deletes the artifact for a `(sha, profile_hash)` and inserts a new
one, leaving earlier jobs pointing at a row id that no longer exists. A job's
output is identified by the **cache key**, not the row id, so the lookup now
falls back to it. It hid because the conformance check sampled *one* job from an
ordered set — it passed on 7 workers and failed on 3.

The image ran as root with a root-owned home, while any sensible Kubernetes
`securityContext` demands non-root. It now creates uid 65532 and chowns the home
in the Dockerfile rather than relying on the pod's `fsGroup`, since not every CSI
driver applies it.

The chart is opinionated about what it will **not** render — sqlite with several
replicas, unshared local blobs with several replicas, a grace period shorter
than the drain, and above all a pod with no `resources.limits`.

## 0.15.0

**The queue is bounded by bytes, not job count.**

Queued uploads are held in RAM by design — blurd never spools source bytes to
disk. A job-count bound therefore promised nothing about memory: 1000 jobs ×
~1.5 MB is ~1.5 GB on a box the sizing model budgeted at 750 MB, and the first
symptom would have been an OOM kill.

Overflow became proper backpressure: **HTTP 503 with `Retry-After`** (code 108).
The previous code mapped to 502, which means "upstream returned garbage" — load
balancers eject a backend on repeated 502s, the opposite of what a queue
shedding load wants.

## 0.14.0

**MongoDB as a third metadata backend.**

Not a schema port. A row-for-row translation would need `$lookup` on every
listing, and a `$lookup` cannot use an index to satisfy the sort — keyset
pagination over a million artifacts would degrade to a blocking in-memory sort,
silently. So the artifact document carries its own copy of the image's
dimensions and every tenant's labels.

The seam built for the Postgres port paid off here: this was a new module beside
`db_sql.py`, not a rewrite of it.

Verified at 113/113 on all three backends, plus a new 77-check suite that runs
two backends side by side and asserts they answer *identically*.

## 0.13.0

**The onnxruntime arena, not the copies, was the memory.**

The obvious suspect was the pipeline's buffers. Redaction now writes in place,
removing a ~32 MB copy per 10 MP image — and it produced **no measurable
change**. `tracemalloc` accounts for only 45 MB of a job because numpy and cv2
allocate outside Python's allocator.

The cost was onnxruntime's CPU tensor arena, which retains freed tensors: right
on a dedicated inference box, wrong on a small VM. Turning it off: 961 → 672 MB
at 7 workers, with throughput differences landing on both sides across runs. The
decisive test was a 512 MB container — arena on was OOM-killed at 3/30 images,
arena off completed 30/30.

## 0.12.0

**Size to the machine, and hand memory back between jobs.**

`workers` defaulted to `os.cpu_count() - 1`, which ignores memory and reports
the *host's* cores inside a container. Sizing is now cgroup-aware.
`malloc_trim()` between jobs returns freed pages to the OS — deliberately not
the same as lowering glibc's global trim threshold, which was measured at −37%
memory for −18% throughput because it trims inside the hot loop.

## 0.11.0

**Several replicas, safely.** Job ownership with heartbeats so a restart never
seizes a live peer's work, an advisory lock around schema migration, the unique
index arbitrating code binding, bounded HTTP threads, and graceful drain.

## 0.10.0

**The PostgreSQL backend.** Two portability bugs worth recording: a SQLite
`REAL` is `DOUBLE PRECISION` in Postgres (8-byte vs 4-byte — getting it wrong
breaks keyset pagination in a way that looks like a logic bug), and NULL
ordering differs between engines, so sortable nullable columns need an
expression rather than a bare column.

## 0.9.0

**`db.py` became the only module that executes SQL.** Forty statements were
scattered across six modules, which made "pluggable metadata store" mean
"pluggable for the call sites someone remembered". Enforced by
`tests/seam_check.py`.

## 0.8.0

Scoped the distributed path, and made the compose setup honest about what it
did and did not yet support.

## 0.7.0

**Optional S3 blob storage.** Blobs are ~95% of stored bytes, so this takes an
instance's stateful footprint from ~360 GB per million images to ~19 GB. The S3
client is ~130 lines of SigV4 over `urllib` rather than boto3, tested against a
real MinIO.

## 0.6.0

**Measured capacity**, and fixed the two bugs the measurement exposed — notably
that `cv2.FaceDetectorYN` is stateful and was being shared across threads, which
failed 39 of 40 concurrent jobs while every single-image test passed.

## 0.5.0

**Made the admin UI survive scale.** Keyset pagination rather than OFFSET
(which is both O(offset) and *wrong* under concurrent inserts), capped counts,
and index-backed sorts only.

## 0.4.0

**Scoped API keys — a scope is a tenant, not a `WHERE` clause.** Tags, metadata
and external ids carry a tenant, and an out-of-scope read returns 404 rather
than 403.

## 0.3.0

API key management in the dashboard, split by blast radius: revocation is
always available, creation is off by default behind a second secret.

## 0.2.0

Async jobs, unique codes as a primary key, and a sidecar standing in for the
producer and consumer applications.

## Unreleased

**Public blob rules + rate limiting.** The dashboard gains a `public` tab where
an admin declares read rules -- one tag, or one metadata `k=v`, optionally
bound to a tenant. `GET /pub/blobs/<sha>` then serves matching redacted images
with no API key; the URL is sha-addressed only (codes are enumerable, a sha256
is not) and rules are evaluated per request so deletion revokes immediately.
All endpoints are now rate-limited per IP: 60/min on `/pub/*`, 300/min
elsewhere, 429 `rate_limited` with `retry_after` on excess. Blocked bursts
flush to the audit log as one grouped row per window, surfaced in the public
tab and the audit trail. Audit rows are pruned past 30 days so a long-lived
instance does not grow its home on traffic alone.

**Dedup race fixed.** Two workers processing the same
`(source_sha, profile_hash)` both reached the artifact insert, and the loser
died on the unique constraint (seen as `internal_error` jobs in a 1k-image
real-data batch). `insert_artifact` now returns None on the conflict -- the
SQL path wraps the insert in a SAVEPOINT so Postgres keeps the transaction
valid -- and the pipeline resolves to the winner's row as a cache hit. Two
more of the same shape fixed alongside: the local store's `.part` temp file
was shared between racing writers (second rename failed ENOENT; the name is
now per-writer), and Mongo's `(source_sha, profile_hash)` index was not
unique, so it enforced nothing -- `init` now upgrades it in place.

**Blob storage cap.** `storage.max_bytes` (default 0 = unlimited,
`BLURD_STORAGE_MAX_BYTES`) bounds the bytes held by live redacted blobs --
counted from `artifacts.blob_size`, so it works identically on local and s3
storage. When a write would exceed the cap, overdue TTL blobs are reclaimed
first; if it still does not fit the job fails with 507 `storage_full`
(recoverable, `retry_after`). Thumbnails and metadata stay outside the
budget. `/v1/stats` gains `bytes_live` and `storage_max_bytes` on the
unrestricted view. For demo boxes and shared VMs where disk exhaustion is
the failure to prevent.

**Job listing gains `sha` + `tag` filters.** `GET /v1/jobs` (and the jobs
dashboard tab) now filter by `sha` -- a `source_sha` prefix served by
`idx_jobs_sha` -- and by `tag`, a quoted-substring match on the job's
`tags_json` submission snapshot. The tag filter answers "the jobs of batch X"
without joining the live labels tables; it is intentionally a snapshot, not
live labels. Both are additive parameters, identical across sqlite/pg/mongo,
and the jobs tab resets keyset pagination when they change.
