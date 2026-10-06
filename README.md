# blurd <img src="docs/assets/logo.png" align="right" width="96" alt="blurd logo">

Redact faces and licence plates from images, for service-to-service use.
One CLI that runs the pipeline locally, hosts a REST API + dashboard as a
daemon, or drives a remote daemon — same commands, all three ways.

**Only the redacted image is stored.** The source is never written to disk; it
is reduced to a sha256 that links the artifact back to its origin and prevents
reprocessing the same bytes twice.

> **Status: proof of concept.** Everything described here is built, running and
> covered by 154 black-box conformance checks, and every performance number is
> measured rather than estimated. It has not been run in production.

---

## Scope

### What blurd does

| | |
|---|---|
| **Redacts** | faces and licence plates, detected with pretrained models — no training required |
| **Ingests** | raw image bytes (`POST` the file) or a URL it fetches itself |
| **Stores** | the redacted image only, keyed by the sha256 of the source |
| **Returns** | a job immediately; the producer polls, or long-polls with `?wait=` |
| **Serves** | the redacted image back by *your* identifier — a filename, an object key, whatever you already have |
| **Isolates** | several apps on one instance, via scoped API keys that define tenants |
| **Shows** | an operator dashboard: browse, filter, inspect detections, manage keys |

### What it deliberately does not do

- **Never stores the original.** Not on disk, not in the database, not even
  while a job is queued. That constraint drives several designs that would
  otherwise look odd — see *Async durability* below.
- **Does not guarantee complete redaction.** Detection recall is not 100%.
  Artifacts with no detections, or any detection below 0.55 confidence, are
  flagged `needs_review` for a human. blurd is a strong first pass, not a
  compliance guarantee.
- **Does not recognise anything.** No OCR, no face matching, no identity. It
  finds regions and destroys them.
- **No GPU.** It targets CPU-only VMs, so capacity is bought in cores and
  sizing is linear (~3.4 images/second per physical core).
- **Not a cluster.** One instance owns its database, its blobs and its queue.
  Reads scale behind a cache and tenants shard cleanly; true clustering would
  need a storage-layer rewrite. See [Capacity](#capacity).

### Who talks to it

blurd sits **behind** its callers — no browser reaches it directly:

```
  photo service ───────────────────────▶ blurd   (producer: submits originals)
  user-facing app ──▶ its backend ─────▶ blurd   (consumer: fetches redacted)
  operator ────────────────────────────▶ blurd   (dashboard, basic auth)
```

Machine callers authenticate with API keys; the dashboard uses separate
credentials and authenticates no `/v1` endpoint.

### Requirements

Python 3.10+ with `sqlite3`, plus `opencv-python-headless`, `onnxruntime` and
`numpy`. One 8 MB model download. No Docker, no external database, no queue,
no object store — a single process and a directory.

---

## The shape of it

```
  photo service ──POST /v1/images──▶ blurd ──▶ job id, immediately
  (holds originals)                    │
                                       ▼ workers redact, off the request path
  user-facing app ──▶ its backend ──GET /v1/blobs/by-code/<your code>──▶ blurd
                                       (one indexed lookup, ~13 µs at 50k images)
```

Submission is **asynchronous**: an HTTP request never has to stay open while an
image is fetched and processed. Retrieval is keyed by **your own unique code**
— usually the filename — so the consuming app asks for what it already knows,
not for a hash it would have to store.

## Try it in one command

```bash
./demo.sh
```

Brings up blurd plus a Go **sidecar** that stands in for both external apps and
prints every URL and credential. Open the sidecar and you get:

1. **Producer panel** — drop a photo, get a job back instantly, watch it go
   `queued → running → done`. This is what the photo service experiences.
2. **Consumer panel** — fetch the redacted image by unique code, sha, job id or
   a metadata filter. This is what the user-facing app's backend experiences.
3. **Admin** — a link to blurd's own dashboard, credentials printed.

The sidecar holds the API key server-side; the browser never sees it and never
talks to blurd directly, mirroring the real topology
(`frontend → backend → blurd`).

![blurd dashboard — stats, filters, facet chips and redacted thumbnails](docs/assets/dashboard.png)

*The dashboard: live counters, filter facets, and a grid of redacted
thumbnails — original images never exist on disk.*

## Quick start (CLI)

```bash
./blurd models pull --all                          # ~8 MB, checksum-verified
./blurd blur photo.jpg --code cam3/IMG_0042.jpg --tag fleet --meta site=paris
./blurd get --code cam3/IMG_0042.jpg               # what a consumer does
./blurd download --code cam3/IMG_0042.jpg --out redacted.jpg

./blurd keys add ci-pipeline                       # API key, shown once
./blurd dashboard-password 'something-good'
./blurd serve --daemon                             # http://127.0.0.1:8770
```

Producer, over HTTP:

```bash
curl -H "Authorization: Bearer $BLURD_API_KEY" \
     -H "Content-Type: image/jpeg" --data-binary @photo.jpg \
     "http://127.0.0.1:8770/v1/images?code=cam3/IMG_0042.jpg&tags=fleet"
# -> 202 {"job_id":"job_b1a3704f6546ff53","status":"queued", ...}

curl -H "Authorization: Bearer $BLURD_API_KEY" \
     "http://127.0.0.1:8770/v1/jobs/job_b1a3704f6546ff53?wait=60"
```

Consumer, over HTTP:

```bash
curl -H "Authorization: Bearer $BLURD_API_KEY" \
     "http://127.0.0.1:8770/v1/blobs/by-code/cam3%2FIMG_0042.jpg" -o redacted.jpg
```

And the same CLI against that daemon, unchanged:

```bash
blurd --remote http://127.0.0.1:8770 --api-key $KEY blur photo.jpg --async
```

## Scoped keys: several apps, one instance

A key can be restricted to a slice of the instance, so `acme` and `fleet` can
share one blurd without seeing each other:

```bash
blurd keys add app-acme --scope-tag acme
blurd keys add app-fleet  --scope-meta appId=fleet
blurd keys add operator                          # unrestricted
```

A scope is a conjunction (all constraints must hold) and it defines a **tenant**,
not merely a read filter. That distinction is load-bearing:

- **Unique codes are namespaced per tenant.** Both apps can submit
  `IMG_0042.jpg` and each gets its own image back. Without this, one app's code
  resolves to another app's photo — a correctness bug, not just a leak.
- **Labels are owned per tenant.** blurd stores identical bytes once, so two
  tenants submitting the same photo share a row. Tags, metadata and codes carry
  a tenant, and a scoped reader sees only its own.
- **Submissions are stamped** with the scope automatically, so a scoped app
  cannot create an image it would then be unable to read. A value contradicting
  the scope is refused (`86 scope_violation`), never silently rewritten.
- **Out-of-scope reads are 404, not 403** — a scoped key should not be able to
  confirm that a sha or job exists.
- **`/v1/stats` is scoped**, so a tenant cannot learn the size of the instance
  it shares.
- **A scoped delete releases only that tenant's claim.** The bytes go once
  nobody references them.

Full reasoning, the operator's cross-tenant code lookup, and the known limits
(including the dedup timing side channel) are in `spec/scoped-keys.md`.

## Unique codes are the point

`--code` / `external_id` is the producer's own identifier. It is a real
PRIMARY KEY, not a metadata row:

- One code → exactly one source image. Several codes may point at the same one,
  because identical bytes are stored once.
- Re-submitting a **known** code returns a finished job immediately, without
  fetching or decoding anything. A re-ingesting producer pays an index lookup
  instead of a download.
- A known code with **different** bytes fails with `94 resource_conflict`
  unless you pass `on_conflict=replace`. Silently repointing it would make a
  consumer's cached URL start returning a different photo.

### Measured lookup cost, 50 000 stored images

| Lookup | Time | Use it for |
|---|---:|---|
| by unique code → blob | **13 µs** | the user-facing path |
| by full sha256 | 10 µs | when the caller kept the hash |
| by metadata `k=v` | 7 ms | browsing and admin, not the hot path |
| by sha prefix | 6 ms | a convenience scan |

Blob responses carry an `ETag` (the redacted blob's sha256) and answer
`If-None-Match` with `304`, so a backend that re-requests the same image pays
nothing.

## How it works

```
POST /v1/images ─► validate (scheme/DNS/IP) ─► enqueue ─► 202 + job id
                                                  │
  worker: source bytes ─► sha256 ─► cache lookup (sha, profile_hash)
                               │ hit → done in ~1 ms, merge new tags
                               ▼ miss
        decode ──► detect (YuNet faces + YOLOv9 plates, on a ≤1280px copy)
               ──► redact (mosaic, boxes expanded 18%, ellipse for faces)
               ──► encode (EXIF dropped) ──► blobs/ab/cd/<sha>-<profile>.jpg
               ──► SQLite: image, artifact, detections, tags, metadata, code
```

### Ephemeral outputs (TTL)

`--ttl 86400` (or `profile.storage.ttl` in the API) prunes the redacted blob
after the deadline — for callers who keep the output themselves and only need
blurd as a transform. The artifact record, codes, tags, detections **and
thumbnail** survive — the dashboard renders an expired card, not a hole — and a
blob fetch past the deadline answers **410
`resource_expired`** (distinct from 404), and resubmitting the same source
under the same code regenerates it. TTL is part of `profile_hash`, so an
expiring output can never collide with the permanent one. A sweeper on the
job reaper deletes expired objects; the read path also prunes lazily so a slow
sweep never serves stale bytes.

### Async durability, deliberately asymmetric

A queued **url** job survives a daemon restart — the daemon can simply re-fetch
it. A queued **raw-upload** job does not: its bytes live in memory and are never
spooled to disk, because "the source image is never written to disk" is the
whole product. Those jobs fail on restart with a recoverable error telling the
producer to resubmit, which it can — it has the originals. Cheap validation
(scheme, DNS, IP range) runs *synchronously at submit*, so a URL that was never
going to be fetched is rejected with a 4xx instead of a 202 the caller has to
poll to understand.

### No training required

Both detectors are pretrained and pinned by URL **and sha256**:

| Class | Model | Size | Licence |
|---|---|---|---|
| Faces | YuNet (`opencv_zoo`) | 233 KB | Apache-2.0 |
| Plates | YOLOv9-t 512 end2end (`ankandrew/open-image-models`) | 7.8 MB | GPL-3.0 lineage |

Fine-tuning is a data-collection project, not a prerequisite. Why these two
were picked — and where detection falls off at distance — is in
[docs/models.md](docs/models.md). Add a model by writing a class in
`src/detect.py` and an entry in `src/models.py`; nothing else in blurd needs
to know it exists.

> **Licensing.** The plate model derives from YOLOv9 (GPL-3.0). Fine for a POC,
> a real constraint if blurd is ever offered as a hosted product. The face path
> is clean. Swapping the plate detector is a one-file change by design.

### The cache key is not just the hash

`(sha256(source), profile_hash)`, where `profile_hash` covers the models,
thresholds, redaction mode and output settings. Upgrade a model and you get a
*new* artifact instead of silently serving a redaction made by the old one.
The algorithm is specified in `spec/profile-hash.md` with a reference vector,
so a future port computes the same value and inherits the existing cache.

### Redaction is mosaic by default

Gaussian blur is partially reversible; a mosaic throws the information away.
`--mode blur|solid` are available per request. Detector boxes are expanded 18%
because they crop tight, and EXIF — including GPS — is dropped on write.

**Recall is not 100%.** Artifacts with no detections, or with any detection
below 0.55 confidence, are flagged `needs_review` and filterable in the
dashboard — where a human can draw additional black rects/ellipses over what
the detectors missed. Manual regions are composited onto the stored redacted
blob (the source is gone, so they can only ever mask more, never reveal) and
saving clears the review flag. Treat blurd as a strong first pass, not a
guarantee.

## Capacity

Every number below is measured, not estimated. Reproduce on your own hardware:

```bash
./demo.sh                                    # or point at any running daemon
python3 bench/throughput.py \
    --url http://127.0.0.1:8771 \
    --api-key "$(cat /tmp/blurd-demo/sidecar.key)"
```

It reports per-image latency by resolution, sustained throughput against
producer concurrency, throughput by image size, and the consumer read path.
Submissions are made unique per run — re-sending the same file measures the
dedup cache (~1 ms), not the pipeline (~130 ms).

Measured on 4 physical cores (i5-11320H, 8 threads), SQLite on local NVMe, no
GPU:

| image | img/s | img/hour | img/day at 100% | core-seconds/image |
|---|---:|---:|---:|---:|
| 640 px | 17.4 | 62,700 | 1.5 M | 0.46 |
| **1280 px** | **13.4** | **48,300** | **1.16 M** | **0.60** |
| 1920 px | 11.1 | 39,900 | 958 k | 0.72 |
| 4000 px | 6.3 | 22,500 | 540 k | 1.28 |

**~3.4 images/second per physical core at 1280 px.** Detection is ~110–130 ms
and nearly size-independent (it runs on a 1280 px copy); only decode, redaction
and encoding grow with resolution.

The work is genuinely CPU-bound: throughput flattens after ~4 workers, and a
process pool beat the thread pool by only 14% — so the GIL is not the ceiling.
**GPU inference is out of scope** (blurd targets CPU-only VMs), which means
capacity is bought in cores and sizing is linear: ~3.4 img/s each, no
order-of-magnitude jump waiting behind a driver install.

Reads peak at ~800–1200/s; put a caching proxy in front for read-heavy use
(blobs already send `ETag`/304).

Storage runs **333 KB/image** for the blob plus ~19 KB of database: 1 M images
≈ 360 GB.

### When to add a second instance

1. **Storage, before CPU.** A saturated instance produces ~390 GB/day.
2. **Sustained ingest past ~70% of capacity** (~9.4 img/s at 1280 px). Watch
   for queue depth that never returns to zero between bursts.
3. **Blast radius** — the queue is shared and FIFO, so one tenant's backlog
   delays everyone. This can justify a second instance before any resource runs out.

### Can it scale horizontally?

**Not as a cluster today** — each instance owns its SQLite, its blobs and its
in-process queue. Two instances are two shards. But:

- **Reads scale now**, with a caching proxy in front of `/v1/blobs/by-code/*`.
- **Sharding by tenant is the natural next step**: scoped keys already partition
  the instance into tenants that share nothing, so routing tenant → instance
  needs no cross-instance coordination at all.
- **True clustering** needs object storage, Postgres and an external queue —
  each behind a narrow interface, which is what `spec/` exists to protect. The
  one thing that actively blocks it: upload payloads live in the accepting
  process's memory (because source images are never written to disk), so upload
  jobs are *sticky to their instance*. URL-submitted jobs distribute freely.

Full numbers, method and caveats in `spec/capacity.md`.

## Deploying

Full recipes, the decision table and the failure modes are in
**[`docs/deployment.md`](docs/deployment.md)**. The shape of it:

```bash
docker compose up -d                       # one instance: S3 blobs, SQLite metadata

BLURD_DB_BACKEND=postgres \
BLURD_DB_DSN=postgresql://blurd:blurd-dev-secret@postgres:5432/blurd \
docker compose --profile pg --profile scale up -d --scale blurd=3   # LB on :8780

helm install blurd ./deploy/helm/blurd --set replicaCount=3 \
  --set db.backend=postgres --set db.existingSecret=blurd-db \
  --set storage.backend=s3  --set storage.s3.existingSecret=blurd-s3
```

Two independent choices. **Metadata** decides whether you can run more than one
replica; **blobs** decide whether those replicas can serve each other's work.

| | one instance | several replicas |
|---|---|---|
| metadata | SQLite (a file) | Postgres **or** MongoDB |
| blobs | local disk **or** S3 | S3 **or** one shared (RWX) volume |

Every combination above passes the same 152 checks — on SQLite, Postgres and
MongoDB, on local and S3 blobs, single-instance and behind a load balancer.
The one configuration that fails is several replicas with a **volume each**:
the metadata read succeeds while the blob 404s on whichever replica did not
process that image. The Helm chart refuses it.

## Running on a cheap VM

blurd sizes itself to the machine. `workers` defaults to `auto`: the lower of
what CPU and **memory** allow, read from the **cgroup** when containerised —
because inside a container the host's figures are a fiction.

```
peak resident  ~=  120 MB  +  90 MB per worker  +  queue budget
```

| memory limit | workers | queue budget | modelled peak |
|---|---:|---:|---:|
| 512 MB | 3 | 22 MB | 412 MB |
| 1 GB | 7 | 60 MB | 810 MB |
| 2 GB | 7 | 495 MB | 1245 MB |

Measured with 30 × 10.7 MP images in a **512 MB container**: a fixed oversized
worker count is **OOM-killed** (exit 137), while `auto` completes **30/30**.

The queue is bounded by **bytes, not job count** — queued uploads live in RAM by
design, so a job-count bound promised nothing about memory. Overflow is HTTP 503
with `Retry-After`: backpressure, not failure. Sizing, the onnxruntime arena
finding and the untaken options: [`spec/resources.md`](spec/resources.md).

The disk gets a bound too: `storage.max_bytes` (`BLURD_STORAGE_MAX_BYTES`, 0 =
unlimited) caps the bytes held by **live** redacted blobs — thumbnails and
metadata stay outside the budget. A write that would exceed it first reclaims
expired TTL blobs, then fails `507 storage_full` if it still does not fit. On a
demo VM, `1 GB` + a 24 h TTL means the disk is bounded in both dimensions.

## Scaling the admin UI

Measured on **50 000 images and 50 000 jobs**. The number that matters is one
page view — the listing, all 24 thumbnails, and the header counters:

| | before | after |
|---|---:|---:|
| full grid page | ~700 ms | **34 ms** |
| SQL queries for it | ~250 | ~34 |
| page 1000 | 101 ms, rising | 4–9 ms, flat |
| header counters | 281 ms | 56 ms, then memoised |

Four things were wrong, none of them in the UI: an N+1 that issued three
queries per row (602 to render 200 rows), thumbnails served through the full
JSON record builder, thumbnails stored *inline* in `artifacts` so every
aggregate dragged 723 MB of blob through the page cache, and OFFSET pagination
— which is O(offset) and also wrong under concurrent inserts, since the window
shifts and the reader sees duplicates.

The dashboard now has **cursor pagination, sorting, and filtering by tag,
metadata, unique code, sha prefix, needs-review and date range**, on both the
images and jobs tabs, with a selectable page size.

Counting is capped at 10 000 (`10,000+`) and computed once per filter rather
than on every page; only index-backed sorts are offered. Full reasoning,
including why the thumbnail migration is lazy and why `VACUUM` is not
automatic, is in `spec/scaling.md`.

## Security posture

- **API keys** are stored as sha256 only; the plaintext is shown once at
  creation. A stolen `blurd.db` does not grant API access.
- **The dashboard uses separate basic-auth credentials**, and its JavaScript
  calls `/ui-api/*` under that session — no API key is ever embedded in a page.
  Dashboard credentials authenticate **no** `/v1` endpoint, and vice versa.
- **The dashboard can list and revoke API keys, but minting is opt-in.**
  Revocation is fail-safe — the worst a compromise achieves is denial of
  service — and it is what you need to do fast during an incident. Creation is
  different: a minted key outlives the dashboard password, so allowing it would
  make one shared, human-typed secret the root of trust for permanent machine
  access. Turn it on deliberately:

  ```bash
  blurd dashboard-keys enable --secret <admin-secret>   # must differ from the
                                                        # dashboard password
  ```

  Minting then also requires an `X-Blurd-Admin-Secret` header, so a compromised
  browser login alone is not enough.
- **Every `/ui-api` mutation carries a CSRF token** (`SameSite=Strict` cookie
  echoed in a header). Before key management existed, image deletion was
  protected only by the CORS preflight a cross-origin `DELETE` happens to
  trigger — an accident, not a defence, and one that a POST endpoint would have
  removed.
- **Privileged mutations are audited** (`blurd audit`, and the dashboard's keys
  tab): `key.create`, `key.revoke`, `image.delete`, with channel and source
  address. `actor` is the channel, not a person — one shared password cannot
  honestly support per-person attribution.
- **URL ingestion is fenced**: hostnames are resolved and checked against
  private/loopback/link-local ranges, redirects are followed manually with the
  IP re-validated **at every hop**, and byte/pixel/time/content-type caps apply.
  Without that, a URL-fetching API inside a VPC is a port scanner and a
  metadata-service reader.
- **Every path is rate-limited per IP**: 60/min on `/pub/*`, 300/min on
  everything else (`/_health` exempt). Excess gets a typed 429 `rate_limited`
  with `retry_after`, and bursts surface in the audit log as one grouped event
  per window — not one row per blocked request.
- **Public reads are admin-declared, never caller-declared.** A rule on the
  dashboard's public tab (one tag or one `k=v` metadata match, optionally bound
  to a tenant) lets `GET /pub/blobs/<sha>` serve the matching redacted bytes
  with no key. Rules are evaluated at read time so deletion revokes on the next
  request; the URL is sha-addressed only, because an enumerable code would be a
  guessing oracle. An image outside every rule — and a revoked one — is the
  same 404 as an image that does not exist.
- **Audit rows are pruned past 30 days.** A long-lived instance does not grow
  its home on traffic alone.

## Dashboard

`http://127.0.0.1:8770/` behind basic auth. Four tabs:

- **images** — thumbnail grid of every stored redaction, filters by unique code
  (exact or `prefix*`), tag, metadata `k=v`, sha prefix and needs-review; detail
  view with per-detection scores, boxes, detector name, codes, full stats and a
  delete action.
- **jobs** — recent jobs with status, unique code, duration and the failure
  reason for anything that did not make it, plus live queue depth.
- **keys** — every API key with its prefix, creation time, last use and status,
  a revoke button, the gated create form, and the audit trail underneath.
  See `spec/dashboard-auth.md` for the reasoning.
- **public** — the rules that make `GET /pub/blobs/<sha>` serve a redacted
  image with no API key (tag or metadata `k=v`, optional tenant pin), plus the
  grouped rate-limit events the limiter has emitted.

## Feedback

`blurd feedback "<message>" [-kind bug|idea|praise|note] [-context "…"]`
dual-writes a submission: to this deployment's `POST /v1/feedback` (open
intake, 16 KB cap, 30/min per IP, idempotent on a client-generated `id`) and,
best-effort, to a central relay — default `https://feedback.intrane.fr`, set
`FEEDBACK_RELAY=off` to disable. It never fails the caller. Reads are
admin-gated: `GET /v1/feedback` needs an operator (unscoped) key. When running
against a remote daemon, `--remote`/`BLURD_URL`/`BLURD_PUBLIC_URL` pick the app
endpoint in that order.

## Storage

Three metadata backends, one environment variable, the same 152 conformance
checks on each:

| `BLURD_DB_BACKEND` | store | for |
|---|---|---|
| `sqlite` (default) | a WAL file at `~/.blurd/blurd.db` | one instance |
| `postgres` | shared, normalised | several replicas — **the recommendation** |
| `mongo` | shared, denormalised documents | several replicas, if you already run Mongo |

Postgres and Mongo are alternatives, not layers. Mongo exists so a shop that
runs Mongo well does not have to become a Postgres shop to deploy blurd; it is
not a performance claim. Its document model is denormalised on purpose, because
a `$lookup` cannot use an index for the sort and keyset pagination would quietly
degrade to an in-memory sort — `spec/mongo.md` has the model and the trade-offs
it costs (write amplification, manual cascades).

- SQL schema in `spec/schema.sql` / `spec/schema.postgres.sql` — plain SQL,
  no ORM, no migration framework. `src/db_sql.py` is the only module that
  executes it; `tests/seam_check.py` enforces that, and is the reason a second
  backend was a new file rather than an excavation.
- Blobs at `~/.blurd/blobs/ab/cd/<sha>-<profile>.jpg` — see `spec/blob-layout.md`.
- `BLURD_HOME` relocates everything.

Which combination to run, and how: [`docs/deployment.md`](docs/deployment.md).

## Why Python, and what comes next

The entire risk here is detection quality, and OpenCV + onnxruntime make that a
solved problem today. The project is nevertheless structured so a **Go-only**
or **machin-only** implementation is a rewrite of the *daemon*, not of the
*contract*:

- `spec/` holds the wire format, the SQL schema, the blob layout and the
  `profile_hash` algorithm, language-neutrally.
- `tests/conformance.py` tests *a binary and a base URL*. A port is done when
  it passes those 152 checks unchanged.
- The HTTP surface uses stdlib `http.server` and raw-body uploads rather than a
  framework and multipart, so nothing in the contract is Python-shaped.

## Status

POC. Verified end to end by `tests/conformance.py` (152 checks, all passing on SQLite, Postgres, MongoDB, and a three-replica cluster) and `tests/backend_parity.py` (77 checks that two backends answer identically)
plus a cold start from an empty home: CLI, API, async jobs, unique-code
resolution and short-circuiting, conflict policy, ETag/304, dashboard, sidecar,
cache behaviour, SSRF guards, CSRF guards, dashboard/API credential separation,
cross-tenant isolation, cursor pagination and sorting at 50k rows, schema
migration from an existing database, EXIF stripping and remote mode.

Not yet: changing a key's scope in place (issue a new key), per-user dashboard logins (today it is one shared password, so the
audit trail records a channel rather than a person), key expiry, login rate
limiting, retention policies, multi-tenant key scoping, per-key rate limits,
webhook callbacks on job completion (today the producer polls), batch
submission in one request, HEIC/animated inputs.

## Running it

`./blurd` is a launcher that picks the first Python with `sqlite3`, `cv2`,
`numpy` and `onnxruntime` — on a machine with several interpreters the default
`python3` is often missing one of them, and the resulting ImportError names
none of the candidates. Set `BLURD_PYTHON` to override. See `AGENTS.md`.

`BLURD_HOME` (default `~/.blurd`) holds the database, the blobs, the models and
the config; moving it moves everything.
