# Blob storage backends

Redacted images are ~95% of what an instance stores: **333 kB per image**
against ~19 kB of metadata. Where they live decides how much state a container
carries, and therefore whether more than one replica can serve the same data.

| backend | blobs | metadata | for |
|---|---|---|---|
| `local` | filesystem under `$BLURD_HOME/blobs` | SQLite | dev, a single container |
| `s3` | any S3-compatible object store | SQLite | Docker / k8s |

**Two profiles, not a matrix.** `local`+SQLite and `s3`+SQLite are the supported
combinations; the conformance suite runs against both. When the metadata store
moves, `s3` + that store becomes the third — there is deliberately no intent to
support every permutation.

## The key is the same everywhere

```
<sha[0:2]>/<sha[2:4]>/<sha256>-<profile_hash>.jpg
```

`artifacts.blob_path` stores exactly this, relative, in every backend. Switching
backends therefore **rewrites no database rows** — which is what makes the
migration below resumable and reversible.

## Configuration

Environment beats the config file, because a container image is immutable and a
running daemon reads its config once, at startup:

| variable | meaning |
|---|---|
| `BLURD_STORAGE_BACKEND` | `local` \| `s3` |
| `BLURD_S3_ENDPOINT` | e.g. `http://minio:9000` |
| `BLURD_S3_BUCKET` | bucket name (must already exist) |
| `BLURD_S3_ACCESS_KEY` / `BLURD_S3_SECRET_KEY` | credentials |
| `BLURD_S3_PREFIX` | optional key prefix — **use one per instance** if a bucket is shared, or orphaned objects from different instances become indistinguishable |
| `BLURD_S3_REGION` | defaults to `us-east-1` (MinIO ignores it, the signature does not) |

Credentials are read from the environment at the point of use and never merged
into the config structure, so nothing can serialise them back to disk.

```bash
blurd storage show     # what is configured
blurd storage check    # round-trip a probe object; fails loudly if it cannot
```

`serve` runs `storage check` at startup and **refuses to start** if the backend
is unusable. Accepting work that cannot be stored is worse than not starting.

## Why SigV4 by hand and not boto3

The S3 client is ~130 lines of SigV4 over `urllib`. Only GET/PUT/DELETE/HEAD on
single objects are ever needed; boto3 plus botocore would add tens of megabytes
to an image whose entire argument is that it is small. The signing is exercised
against a real MinIO in the test suite — 2 MB binary round-trips, prefix
isolation, missing keys, wrong credentials, missing bucket, unreachable
endpoint — rather than mocked.

The deliberate limitation: **no query parameters are signed**, because blurd only
addresses whole objects. Anything that adds one (`ListObjectsV2`, presigned
URLs) must also put it in the canonical query string, or the signature silently
stops matching.

## Migrating an existing instance

Lazy and resumable, the same shape as the thumbnail migration — a blob copy is
not something to do inside a startup transaction:

```bash
export BLURD_S3_ENDPOINT=... BLURD_S3_BUCKET=... \
       BLURD_S3_ACCESS_KEY=... BLURD_S3_SECRET_KEY=...

blurd migrate-blobs --to s3            # copies; already-present blobs are skipped
blurd config set storage.backend s3    # flip only once the copy is complete
blurd stop && blurd serve --daemon     # config is read at startup
```

Re-running is free: every blob already present at the destination is skipped, so
an interrupted migration resumes where it stopped. `--delete-source` removes each
blob from the old backend once written, and is worth leaving off for the first
pass so the old copy remains a fallback. `--to local` reverses the whole thing.

## Ordering, and what can go wrong

Writes go **object first, database row second**. An orphaned object is garbage a
sweep can find; a row pointing at an object that was never written is a broken
record with no way back. There is currently **no orphan sweep** — if a process
dies between the two writes the object is simply never referenced. At 333 kB a
time this is a housekeeping task, not an incident, but it is not yet automated.

## Measured cost

Read latency for `GET /v1/blobs/by-code/<code>`, MinIO on localhost:

| backend | p50 | p95 |
|---|---:|---:|
| `local` | 1.31 ms | 2.19 ms |
| `s3` | 2.49 ms | 3.24 ms |

About **+1.2 ms**. That is the floor, not the expectation: a real S3 across a
network will be several times worse, and the honest planning number is 5–20 ms.
blurd relays the bytes rather than redirecting to a presigned URL, which keeps
key scope enforced on every byte served and keeps the storage endpoint private —
at the cost of blurd remaining in the data path. If read volume ever outgrows
that, a caching proxy in front is the first move: blobs already carry an `ETag`
and answer `If-None-Match` with `304`.

## What this does and does not unlock

**Does:** the stateful footprint of an instance drops from ~360 GB per million
images to ~19 GB. Several replicas can serve the same blobs. Retention and
lifecycle become the object store's job.

**Does not, on its own:** SQLite is still a single-writer file on one volume, so
multiple replicas are not yet possible. That is the next backend to replace — and
once it is, replicas work without a shared queue, because each replica processes
what it accepts and any replica can serve the result. See `spec/capacity.md`.

## Bounding the disk: `storage.max_bytes`

`storage.max_bytes` (env `BLURD_STORAGE_MAX_BYTES`, default 0 = unlimited) is a
hard cap on **live** blob bytes, counted as `SUM(artifacts.blob_size)` over
rows whose `blob_path` is still set — the metadata DB is the meter, so the cap
works identically on `local` and `s3` and needs no `du` or bucket listing.
Thumbnails and metadata stay outside the budget: they are small, and they are
what let the dashboard show an expired artifact rather than a hole.

When a write would exceed the cap the pipeline first reclaims overdue TTL
blobs (the same bounded, idempotent sweep the reaper runs), and only if the
blob still does not fit fails the job with `507 storage_full` — recoverable,
with `retry_after`. A cap plus a TTL therefore bound the disk in both
dimensions: how much, and for how long.

`/v1/stats` exposes `bytes_live` and `storage_max_bytes` on the unrestricted
view; scoped keys still cannot read instance size.

## TTL expiry semantics

An artifact past `expires_at` keeps its row, its thumbnail, its codes and its
detections; only the blob object is deleted and `blob_path` cleared. Blob
reads answer `410 resource_expired`; thumbnail reads keep answering 200.
