---
title: Deployment
layout: default
nav_order: 2
---

# Deploying blurd

Every shape below has been run end to end and passes the same 152 black-box
conformance checks. Where a number appears it was measured, not estimated.

This is the operator's page: *which shape do I want, and what do I type*. The
reasoning behind the designs lives in `spec/` — that is written for someone
porting blurd, not running it.

---

## 1. Pick a shape

Two independent choices. **Metadata** decides whether you can run more than one
replica; **blobs** decide whether those replicas can serve each other's work.

| | one instance | several replicas |
|---|---|---|
| **metadata** | SQLite (a file) | Postgres **or** MongoDB |
| **blobs** | local disk **or** S3 | S3 **or** one shared (RWX) volume |

All four blob/scale combinations are supported and measured:

| | local blobs | S3 blobs |
|---|---|---|
| **one instance** | 152/152 | 152/152 |
| **several replicas** | 152/152 **on one shared volume** | 152/152 |

The only configuration that fails is several replicas with **a volume each** —
see [What breaks, and how it looks](#6-what-breaks-and-how-it-looks).

### A decision in three questions

1. **Do you need more than one replica?**
   No → SQLite. Yes → Postgres (recommended) or MongoDB (if you already run it).
2. **Do you have object storage, or want it?**
   Yes → `s3`. No, but you have NFS/CephFS/EFS → `local` on a shared volume.
   Neither, and one replica → `local`.
3. **Kubernetes?** → the Helm chart. Otherwise → Compose.

---

## 2. One instance

The simplest thing that works. Metadata in a SQLite file, blobs on disk, no
other moving parts.

```bash
./blurd serve --port 8770
```

Or as a container, with nothing else running anywhere:

```bash
docker run -d --name blurd \
  --memory 1g --cpus 4 -p 8770:8770 \
  -e BLURD_PULL_MODELS=1 \
  -e BLURD_DASHBOARD_PASSWORD=change-me \
  -v blurd-home:/var/lib/blurd \
  blurd:0.17.0
```

With Compose, which adds MinIO and puts the blobs in it:

```bash
docker compose up -d
```

For a single-instance deploy with **no MinIO** — everything (SQLite, blobs,
models) on one named volume, e.g. a Coolify demo. This file also starts the
demo **sidecar UI on :8790** (a bootstrap container first registers the demo
API key it carries) and defaults dashboard login + key-creation step-up to
`blurd-demo`:

```bash
docker compose -f docker-compose-local.yml up -d --build
```

**Everything in `BLURD_HOME` matters here** — it holds the database, the blobs
and the models. Back it up, or move the blobs to object storage (below).

---

## 3. Several replicas

Two things must become shared: the metadata store, and the blobs.

### Metadata: Postgres or MongoDB

Both pass the same 152 checks, plus a 77-check suite that runs them side by side
and asserts they answer *identically*. **Postgres is the recommendation**;
MongoDB exists so a shop that already operates MongoDB does not have to become a
Postgres shop. It is an operations argument, not a performance one.

```bash
# Postgres
BLURD_DB_BACKEND=postgres \
BLURD_DB_DSN=postgresql://blurd:blurd-dev-secret@postgres:5432/blurd \
docker compose --profile pg --profile scale up -d --scale blurd=3

# MongoDB
BLURD_DB_BACKEND=mongo \
BLURD_DB_DSN=mongodb://blurd:blurd-dev-secret@mongo:27017/?authSource=admin \
docker compose --profile mongo --profile scale up -d --scale blurd=3
```

The load balancer is the front door on **:8780**. Note the replica ports are a
*range* (`8770-8779`), because `--scale 3` cannot bind one port three times.

> **SQLite cannot be shared.** It is a single-writer file: two writers is silent
> corruption, not a slower mode. blurd refuses to start a second instance
> against one home (exit `94`), and the Helm chart refuses to render it.

### Blobs: S3, or one shared volume

```bash
BLURD_STORAGE_BACKEND=s3 \
BLURD_S3_ENDPOINT=http://minio:9000 BLURD_S3_BUCKET=blurd \
BLURD_S3_ACCESS_KEY=… BLURD_S3_SECRET_KEY=…
```

Object storage is the recommendation because there is nothing to share and
nothing to size. Blobs are ~95% of the stored bytes (~333 kB per image against
~19 kB of metadata), so this takes an instance's stateful footprint from
**~360 GB per million images to ~19 GB**.

If you already have a ReadWriteMany volume and would rather not run object
storage, `local` on **one shared volume** works — measured at 152/152 with three
replicas behind a load balancer, serving byte-identical blobs from a replica
that never processed the image.

Measured cost of S3 over local: **+1.2 ms** on the read path against MinIO on
localhost (1.31 → 2.49 ms p50). Budget 5–20 ms against real S3 over a network.
blurd relays the bytes rather than redirecting to a presigned URL, so key scope
stays enforced on every byte and the storage endpoint stays private.

### Switching without a migration

The storage key is identical in both backends (`ab/cd/<sha>-<profile>.jpg`), so
switching **rewrites no database rows**:

```bash
blurd migrate-blobs --to s3     # resumable; re-running skips what is present
```

> **There is no equivalent for metadata.** A SQLite instance cannot move its
> data to Postgres or Mongo. A new deployment picks its metadata backend at the
> start. This is the main open gap.

### What makes replicas safe, not merely possible

- **Job ownership.** Every job carries the instance that accepted it, and each
  instance heartbeats. A reaper reclaims only jobs whose owner has stopped, so a
  restart never seizes a live peer's work. `url` jobs are adopted; uploads fail
  with a recoverable "resubmit", because their bytes lived only in the dead
  replica's memory.
- **An advisory lock around schema migration**, so three pods starting together
  do not all migrate at once.
- **The unique index arbitrates code binding**, rather than a SELECT-then-INSERT
  that two replicas can both win.
- **Bounded HTTP threads**, so a burst cannot exhaust `max_connections` and take
  the database out for every replica at once.
- **Graceful drain.** `SIGTERM` makes `/v1/health` report 503 so the balancer
  drops the replica, then in-flight work finishes.

**There is no shared job queue and none is needed**: each replica processes what
it accepts, and any replica serves any result.

---

## 4. Kubernetes

```bash
helm install blurd ./deploy/helm/blurd \
  --set replicaCount=3 \
  --set db.backend=postgres --set db.existingSecret=blurd-db \
  --set storage.backend=s3 --set storage.s3.endpoint=http://minio:9000 \
  --set storage.s3.existingSecret=blurd-s3 \
  --set dashboard.existingSecret=blurd-dashboard
```

Or without object storage, on a ReadWriteMany volume:

```bash
helm install blurd ./deploy/helm/blurd \
  --set replicaCount=3 \
  --set db.backend=postgres --set db.existingSecret=blurd-db \
  --set storage.backend=local \
  --set persistence.enabled=true --set persistence.accessMode=ReadWriteMany \
  --set persistence.size=200Gi \
  --set dashboard.existingSecret=blurd-dashboard
```

The chart is deliberately opinionated about what it will **not** render — that
is most of its value, and each refusal encodes a failure that is silent or
misleading at runtime. Full list and the production recipes:
[`deploy/helm/blurd/README.md`](https://github.com/javimosch/blurd/blob/main/deploy/helm/blurd/README.md).

The one worth repeating here: **a pod with no `resources.limits` is refused**,
because blurd sizes its worker pool from the cgroup and a pod without limits
sizes itself from the *node*, then gets OOM-killed against a limit it never saw.

> **Status:** the chart lints, renders valid manifests for every supported
> combination (schema-checked against the Kubernetes 1.29 API), and the pod
> shape passes conformance under Docker. It has **not** been applied to a live
> cluster, so scheduling, CSI, ingress and HPA behaviour are unverified in situ.

### Key management on Kubernetes

Key commands are local-only (`keys add/list/revoke/export/import` — there is
no `/v1` endpoint for them), but the image's entrypoint *is* the CLI, so no
shell is needed:

```bash
kubectl exec deploy/blurd -- python3 run.py keys add my-app
kubectl exec deploy/blurd -- python3 run.py keys export > keys.json
```

With Postgres or Mongo, keys live in the shared metadata DB, so **any replica
works** and the rest see the change immediately. The pod's env already carries
`BLURD_HOME` and the `BLURD_DB_*` settings, so the CLI inside the container is
configured the same way the daemon is.

To move keys between clusters — the case where external apps keep their
existing key:

```bash
kubectl exec -i deploy/blurd -- python3 run.py keys import - < keys.json
```

An export file carries sha256 hashes and scopes only (see
`spec/scoped-keys.md`), never plaintext — the same secret then authenticates
on the new instance.

If `pods/exec` is unavailable, there is a second path for the shared-backend
case: `kubectl port-forward` the metadata database and run
`./blurd-venv keys export|import` locally with `BLURD_DB_*` pointing at it —
keys are DB rows, not pod files. This does not apply to the SQLite backend,
which is single-replica and whose state is the PVC.

---

## 5. Sizing

blurd sizes itself to the machine. `workers` defaults to `auto`: the lower of
what CPU and **memory** allow, read from the **cgroup** when containerised,
because inside a container the host's figures are a fiction.

```
peak resident  ~=  120 MB  +  90 MB per worker  +  queue budget
```

| memory limit | workers | queue budget | modelled peak |
|---|---:|---:|---:|
| 512 MB | 3 | 22 MB | 412 MB |
| 1 GB | 7 | 60 MB | 810 MB |
| 2 GB | 7 | 495 MB | 1245 MB |
| 8 GB | 7 | 512 MB | 1262 MB |

Throughput is **~3.4 images/s per physical core** — blurd is CPU-bound. Scale
CPU for speed, memory for concurrency and queue depth. Past ~7 workers a single
instance stops gaining; add a replica instead.

An explicit `BLURD_WORKERS=N` is always honoured — it is the operator's call —
but startup warns with the numbers rather than letting you find out from the OOM
killer mid-job. `blurd doctor` prints the whole budget; `bash bench/memory.sh`
re-measures the model.

**Set memory limits on containers.** Measured with 30 × 10.7 MP images in a
512 MB container: the old fixed default was OOM-killed at exit 137, while `auto`
completed 30/30.

---

## 6. What breaks, and how it looks

Each of these was reproduced deliberately, because none of them presents as the
problem it actually is.

| configuration | what you see | what it is |
|---|---|---|
| two instances on one SQLite home | the second exits `94` | a guard; sharing the file would corrupt it silently |
| several replicas, **a volume each**, local blobs | metadata `200`, blob **`404`** on a fraction of reads | blobs written by one replica are invisible to the others |
| container with no memory limit | random OOM kills | blurd sized itself from the *node*, not the pod |
| queue full | `503` + `Retry-After` | **backpressure, not failure** — the request was valid |
| pod SIGKILLed before draining | jobs fail with "resubmit" | queued upload bytes lived only in that replica's memory |
| ingress body limit below 64 MB | `413` blurd never sees | must match `MAX_BODY` in `src/server.py` |

The second row is the nasty one: the metadata read **succeeds**, because it is
in the shared database, while the blob 404s on whichever replica did not process
that image. Intermittent failures on a fraction of reads, not an outage.

### Under sustained overload

The queue is per-replica, bounded by **bytes, not job count** — queued uploads
are held in RAM by design, because blurd never writes source images to disk.
Overflow answers **503 with `Retry-After`** (code 108, `overloaded`). Sustained
503s mean the deployment is at capacity: add a replica, or slow the producer.

Redis or RabbitMQ is **not** recommended: Postgres already provides a correct
work queue if cross-replica work-stealing is ever needed, and putting source
images through a broker would store the originals in that broker — breaking the
guarantee the service exists to make.

---

## 7. Verify a deployment

```bash
curl -s localhost:8770/v1/health          # storage + database actually in use
blurd doctor                              # models, sizing, backends
helm test blurd                           # asserts healthy AND on the right backends

# the real check: 126 black-box checks against a binary and a URL
python3 tests/conformance.py --bin ./blurd --url http://127.0.0.1:8770 \
  --api-key "$K" --image small.jpg --image-b large.jpg \
  --scoped-key-a "$A" --scoped-key-b "$B" \
  --dashboard-user admin --dashboard-password "$P"
```

The scoped keys must carry the tags `conformance-a` / `conformance-b`, and the
dashboard password must be set **before** the daemon starts — otherwise whole
sections are skipped or fail for the wrong reason.

```bash
bash tests/helm_guardrails.sh             # 30 checks on the chart's refusals
python3 tests/backend_parity.py …         # two backends answer identically
python3 tests/queue_bytes.py …            # backpressure and the byte bound
```

---

## 8. Configuration

A container cannot run `blurd config set` before it starts, and a running daemon
reads its configuration **once**. So everything a deployment needs is an
environment variable:

| variable | what |
|---|---|
| `BLURD_HOME` | everything blurd stores locally |
| `BLURD_DB_BACKEND` | `sqlite` \| `postgres` \| `mongo` |
| `BLURD_DB_DSN` | connection string (carries a password — use a Secret) |
| `BLURD_DB_DATABASE` | mongo only |
| `BLURD_STORAGE_BACKEND` | `local` \| `s3` |
| `BLURD_S3_ENDPOINT` / `_BUCKET` / `_ACCESS_KEY` / `_SECRET_KEY` / `_REGION` / `_PREFIX` | object storage |
| `BLURD_WORKERS` / `BLURD_HTTP_THREADS` | `auto` unless you mean otherwise |
| `BLURD_QUEUE_MAX` / `BLURD_QUEUE_MAX_BYTES` | queue bounds; bytes is the one that matters |
| `BLURD_FETCH_MAX_BYTES` | input-size guard on every input path; default 5 MB |
| `BLURD_STORAGE_MAX_BYTES` | cap on live redacted blobs; 0 = unlimited. Over it: expired TTL blobs are reclaimed, then writes fail `507 storage_full` |
| `BLURD_DRAIN_SECONDS` | must be under the container's grace period |
| `BLURD_DASHBOARD_USER` / `BLURD_DASHBOARD_PASSWORD` | the human dashboard only — never `/v1` |
| `BLURD_PULL_MODELS` | fetch detector models at start |
| `BLURD_HOST` / `BLURD_PORT` | what to listen on |
| `BLURD_ORT_ARENA` | leave off; on costs ~40 MB/worker for no gain |
| `BLURD_ORT_THREADS` | onnxruntime intra-op threads; 1 is right with several workers |
| `BLURD_MALLOC_TRIM` | on by default; hands freed memory back between jobs |

The full list is `ENV_OVERRIDES` in `src/config.py`. Adding a setting without an
entry there makes it unreachable in every deployment shape that matters.

---

## Further reading

| | |
|---|---|
| chart reference and every guardrail | [`deploy/helm/blurd/README.md`](https://github.com/javimosch/blurd/blob/main/deploy/helm/blurd/README.md) |
| why the distributed design is what it is | [`spec/distributed.md`](https://github.com/javimosch/blurd/blob/main/spec/distributed.md), [`spec/replicas.md`](https://github.com/javimosch/blurd/blob/main/spec/replicas.md) |
| blob layout, S3 client, migration runbook | [`spec/storage.md`](https://github.com/javimosch/blurd/blob/main/spec/storage.md) |
| the metadata backends | [`spec/postgres.md`](https://github.com/javimosch/blurd/blob/main/spec/postgres.md), [`spec/mongo.md`](https://github.com/javimosch/blurd/blob/main/spec/mongo.md) |
| memory model and how it was measured | [`spec/resources.md`](https://github.com/javimosch/blurd/blob/main/spec/resources.md) |
| throughput and capacity planning | [`spec/capacity.md`](https://github.com/javimosch/blurd/blob/main/spec/capacity.md) |
