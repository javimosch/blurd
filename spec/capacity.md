# Capacity and scaling

All numbers measured on one host: **Intel i5-11320H, 4 physical cores / 8
threads**, SQLite on local NVMe, no GPU. Reproduce with
`python3 bench/throughput.py --url <daemon> --api-key <key>`.

Per-core figures are the transferable ones; the absolute rates are for this box.

## Ingest throughput

Sustained, 8 concurrent producers, queue driven to saturation, all-unique bytes
(re-submitting the same image measures the dedup cache, not the pipeline — it
returns in ~1 ms).

| image | pixels | img/s | img/hour | img/day (100%) | p50 under load | core-seconds/image |
|---|---:|---:|---:|---:|---:|---:|
| 640 px | 0.27 MP | 17.4 | 62,700 | 1.5 M | 358 ms | 0.46 |
| **1280 px** | **1.09 MP** | **13.4** | **48,300** | **1.16 M** | **509 ms** | **0.60** |
| 1920 px | 2.46 MP | 11.1 | 39,900 | 958 k | 650 ms | 0.72 |
| 4000 px | 10.7 MP | 6.3 | 22,500 | 540 k | 1186 ms | 1.28 |

Idle latency for a single image (nothing else running): 115 / 153 / 172 / 272 ms
for the four sizes. Detection dominates at ~110–130 ms and is nearly
size-independent, because detection runs on a copy downscaled to 1280 px —
only decode, redaction and encoding grow with resolution.

**Rule of thumb: ~3.4 images/second per physical core at 1280 px.**

## This is CPU-bound, and already parallel

| workers | img/s |
|---:|---:|
| 2 | 10.0 |
| 4 | 12.3 |
| 6 | 12.5 |
| 8 | 13.5 |

Throughput flattens after ~4 workers and adding more only inflates latency
(p50 186 ms → 558 ms from 2 to 8 workers) — the classic signature of a
saturated resource absorbing concurrency as queueing. The daemon uses 400–500%
CPU of the 800% the box reports, which is consistent with 4 real cores.

A **process** pool was tested to see whether the GIL was the ceiling. It peaked
at 15.4 img/s against 13.5 for threads — about 14% better, not the 4x that a
GIL bottleneck would give. The models genuinely saturate the CPU, so there is
no easy vertical win left beyond more cores (or a GPU, untested).

`workers` and `ort_threads` are configurable. `ort_threads=1` is right for
throughput: workers already provide parallelism, so >1 oversubscribes.

## Read throughput (the consumer path)

`GET /v1/blobs/by-code/<code>`, the endpoint a user-facing backend hits:

| concurrent clients | reads/s | p50 | p95 |
|---:|---:|---:|---:|
| 1 | 472 | 1.9 ms | 3.0 ms |
| 4 | 824 | 4.0 ms | 11 ms |
| 16 | 241 | 17 ms | 43 ms |
| 32 | 244 | 28 ms | 125 ms |

Peak is ~800–1200 reads/s around 4 concurrent clients; beyond that the stdlib
`ThreadingHTTPServer` degrades — it spawns a thread per connection and the
tail grows badly (p95 > 1 s at 32 clients). **For a read-heavy deployment, put
a caching reverse proxy in front.** Blobs already carry an `ETag` and answer
`If-None-Match` with 304, so a cache is effective without any change to blurd.

## Storage

Measured on real redactions (1280–4000 px mixed):

| | per image |
|---|---:|
| redacted blob on disk | 333 KB |
| thumbnail (in SQLite) | 15 KB |
| metadata rows | 4 KB |

| images | blobs | database | total |
|---:|---:|---:|---:|
| 100 k | 34 GB | 1.9 GB | **36 GB** |
| 1 M | 341 GB | 19.5 GB | **360 GB** |
| 10 M | 3.4 TB | 195 GB | **3.6 TB** |

Blob size tracks `profile.output.quality` (90) and `output.max_side`; capping
the long edge is the cheapest lever if storage is the binding constraint.

## When to add a second instance

Ingest is rarely the first thing to run out. In order of likelihood:

1. **Storage before CPU.** One instance saturated at 1280 px produces ~1.16 M
   images/day, which is ~390 GB/day. Disk is the first wall on any normal host.
2. **Sustained ingest above ~70% of capacity.** At 1280 px that is ~9.4 img/s
   (≈810 k/day). Past that, a burst has nowhere to go and queue depth grows
   without recovering. Watch `queued` on the dashboard's jobs tab, or
   `queue.queued` in `/v1/stats`: a depth that never returns to zero between
   bursts is the signal, not a single spike.
3. **Read tail latency.** p95 on the consumer path climbing past your SLA, if a
   cache in front has not already fixed it.
4. **Blast radius.** One tenant's backlog delays every other tenant's jobs —
   the queue is shared and FIFO, with no per-tenant fairness. That can justify
   a second instance long before any resource is exhausted.

## Can blurd scale horizontally?

**Not as a cluster today.** Each instance owns its own SQLite database, its own
blob directory, and its own in-process queue. Two instances are two independent
shards, not one system. Three paths, in increasing order of work:

### 1. Scale reads — available now, no code change

The consumer path is read-only, keyed by a stable identifier, and already
ETag-tagged. A caching reverse proxy or CDN in front of `/v1/blobs/by-code/*`
scales reads out arbitrarily. Redacted images are also, by construction, the
safe-to-cache version.

### 2. Shard by tenant — small work, and the design already fits

Scoped keys (`spec/scoped-keys.md`) already partition the instance into tenants
with their own code namespaces and their own labels. Tenants share nothing by
design, so routing tenant → instance at a gateway needs no cross-instance
coordination at all: no distributed transactions, no shared cache, no
consistency problem. This is the recommended second step, and it also fixes the
blast-radius issue above.

What it costs: a router that maps an API key or tenant to a backend, and
per-instance operations. What you lose: deduplication no longer spans tenants —
but it already does not, deliberately.

### 3. True clustering — a storage-layer rewrite

Would require:

| concern | today | needed |
|---|---|---|
| blobs | local filesystem (`src/store.py`) | object store (S3/MinIO) |
| metadata | SQLite (`src/db.py`, plain SQL) | Postgres |
| queue | in-process, in-memory (`src/jobs.py`) | Redis / NATS / SQS |

Each is behind a narrow interface, and `spec/schema.sql` is deliberately plain
SQL with no ORM, so a Postgres port is mechanical rather than a redesign.

**The one design decision that actively blocks it** is documented in
`spec/jobs-and-codes.md`: raw-upload payloads live only in the memory of the
process that accepted them, because blurd never writes source images to disk.
That makes an upload job *sticky to its instance* — it cannot be picked up by a
peer. URL-submitted jobs carry no payload and would distribute immediately.

Making uploads distributable means putting pending source bytes in shared
storage, which contradicts the guarantee the whole service is built on. That is
a product decision, not a refactor, and it should be made deliberately:
either accept sticky uploads (route a producer to one instance and let its
retries land there), or prefer URL submission for the multi-instance path.

## Out of scope: GPU

blurd targets **CPU-only VMs**. GPU inference is therefore not a planned lever,
and every number here is the number that matters — there is no faster
configuration waiting behind a driver install.

The practical consequence: capacity is bought in cores. Detection is ~80% of
the per-image budget and scales with physical cores at roughly 3.4 images per
second each, so sizing is linear and predictable. If throughput is short, add
cores or add instances (see above); there is no order-of-magnitude jump
available.

If that assumption ever changes, `src/detect.py` is where it would be made:
the ONNX models already run through onnxruntime, so a provider swap is the
whole change. It is not implemented or tested.

## What has not been measured

- Behaviour above ~1 M rows; the listing figures in `spec/scaling.md` stop at
  50 k.
- Sustained multi-hour runs — thermal throttling on a laptop CPU is not
  representative of a server.
- Concurrent ingest and heavy reads at the same time; they were measured
  separately.
