---
name: blurd-deploying
description: Deploying blurd with Docker Compose or Kubernetes. Read before changing the Dockerfile, docker-compose.yml, deploy/nginx.conf or deploy/helm/. Covers the three compose profiles and what each is proven to do, the Helm chart's guardrails and why each exists, and the container gotchas (non-root, cgroup sizing, model fetching) that only appear in a real deployment.
---

# Deploying blurd

`docs/deployment.md` is the operator-facing page: the decision table, the exact
commands for each shape, and the failure modes. **Keep it current** — it is the
one place a human looks, and the deployment facts were previously spread over
five files, which is exactly how the README came to carry a stale memory model
and a "blurd does not use Postgres yet" line months after it did.

This skill is the part an agent needs that does not belong in operator docs.

Two supported shapes, both exercised end to end:

```
local        one instance, SQLite, blobs on disk OR in S3
distributed  N replicas, Postgres OR Mongo, LB in front,
             blobs in object storage OR on one shared (RWX) volume
```

All four storage/scale combinations are supported and measured. The only one
that fails is several replicas with a volume EACH -- see below.

## Docker Compose

```bash
docker compose up -d                      # 1 replica, S3 blobs, SQLite metadata
docker compose --profile pg up -d         # + Postgres
docker compose --profile mongo up -d      # + MongoDB
```

Three replicas behind nginx, on either shared backend:

```bash
BLURD_DB_BACKEND=postgres \
BLURD_DB_DSN=postgresql://blurd:blurd-dev-secret@postgres:5432/blurd \
docker compose --profile pg --profile scale up -d --scale blurd=3
# the load balancer is the front door on :8780
```

**Proven, not assumed** — all of the following were run:

| shape | result |
|---|---|
| default profile (S3 + SQLite) | 113/113 conformance |
| 3 replicas on Postgres, through the LB | 113/113; 20/20 images completed with a replica SIGKILLed mid-flight |
| 3 replicas on Mongo, through the LB | 113/113; same kill test; clean SIGTERM drain |
| 512Mi non-root container, 3 workers | 113/113, no OOM |
| 1 instance, local blobs, no S3 at all | 113/113 |
| 3 replicas + Postgres + **local blobs on one shared volume** | 113/113 through the LB |
| 2 replicas + Postgres + local blobs, **a volume each** | blob 404s on the replica that did not process the image |

### Compose gotchas

- **The host port is a RANGE (`8770-8779:8770`)**, because `--scale blurd=3`
  cannot bind one fixed port three times. The first replica does not reliably
  land on 8770 — check `docker port blurd-blurd-1`. A `curl localhost:8770`
  that returns nothing is usually this, not a broken container.
- **`curl` exits 0 on a 502.** A readiness loop written as
  `curl -s -o /dev/null … && break` breaks immediately while nginx is still
  returning 502 from before the replicas were listening. Compare the status
  code instead.
- **nginx must re-resolve per request.** `upstream { server blurd:8770; }`
  resolves once at startup and pins to a replica that may later die. The config
  uses `resolver 127.0.0.11 valid=5s` and a variable `proxy_pass`; do not
  "simplify" it back.
- **`client_max_body_size` must match `MAX_BODY`** (64 MB in `src/server.py`),
  or large uploads are rejected by the proxy with a 413 blurd never sees.

## Kubernetes (Helm)

`deploy/helm/blurd/`. Read its README for the production invocation.

The chart's main value is **what it refuses to render**. Every refusal encodes a
failure that is silent at runtime or presents as something else entirely:

- `sqlite` + more than one replica, or + autoscaling — SQLite is a single-writer
  file; two writers corrupt it silently.
- `storage.backend=local` + more than one replica **without a shared volume** —
  blobs written by one pod are invisible to the others. Measured: the metadata
  read returns 200 (it is in the shared database) while the blob returns 404 on
  whichever replica did not process that image. Intermittent failures on a
  fraction of reads, which is worse to diagnose than an outage.

  A **shared** volume is a legitimate configuration and the chart accepts it
  (`persistence.accessMode=ReadWriteMany`, or `persistence.shared=true` for an
  existingClaim it cannot inspect). Three replicas on one shared volume pass
  113/113. The chart cannot inspect a claim's access mode, so it makes the
  operator declare it rather than assuming — guessing wrong puts the silent
  failure back.
- **no `resources.limits`** — see below; this is the big one.
- `terminationGracePeriodSeconds <= drainSeconds` — a pod SIGKILLed mid-drain
  loses the uploads queued in its memory.
- a dashboard with no password, a backend with no DSN, no source of models.

`bash tests/helm_guardrails.sh` asserts all of them still fire, and that every
valid combination still renders (30 checks). Run it after touching the chart: a
guardrail that stops firing is a promise the chart has quietly stopped keeping.

### Container gotchas — each cost real debugging

1. **No cgroup limits means blurd sizes itself from the NODE.** `resources.py`
   reads `memory.max` and `cpu.max`; with no limit the cgroup reports "max" and
   the fallback is the node's figures. A pod on a 64 GB node starts as many
   workers as the node could feed and is OOM-killed against a limit it never
   saw. The chart refuses to render without limits for exactly this reason.
   **The memory limit is the sizing knob** — leave `workers` empty.

2. **The image runs as uid 65532 and chowns its home in the Dockerfile**, rather
   than relying on the pod's `fsGroup`. Not every CSI driver applies fsGroup,
   and a chart that works on some clusters is worse than one that fails on all.

3. **Anything that creates the home must mount the home.** `models pull`
   initialises the whole home — it creates `blobs/` alongside `models/` — so an
   init container that mounts only the models volume writes into the image's
   directory and fails with `EACCES` on every single deploy. The init container
   mounts both.

4. **Fetch models in an init container, not at startup.** A pod that cannot get
   its models otherwise starts healthy, passes its probes, and fails *every*
   submission — the worst way to find out.

5. **Readiness is the drain signal.** On SIGTERM blurd answers 503 on
   `/v1/health`, so the readiness probe withdraws the endpoint before it stops
   accepting. Keep the readiness probe twitchy and the liveness probe tolerant:
   a busy replica is not a broken one, and restarting it mid-job discards queued
   uploads for nothing.

6. **Roll pods when config changes.** blurd reads its configuration once at
   startup, so a `helm upgrade` that only edits the ConfigMap would leave every
   replica on the old settings. The Deployment carries a `checksum/config`
   annotation for this.

### Known gap

The chart has **not been applied to a live cluster** — none was reachable from
the development environment. It lints, renders valid manifests for every
supported combination (schema-validated against the Kubernetes 1.29 API), and
the pod shape passes conformance under Docker. Scheduling, CSI behaviour,
ingress and HPA behaviour remain unverified in situ. Say so rather than implying
otherwise.

## Configuration reaches the container by ENVIRONMENT only

A container cannot run `blurd config set` before it starts, and a running daemon
reads its configuration once. **Anything a deployment must set needs an entry in
`config.ENV_OVERRIDES`** — adding a setting without one makes it unreachable in
every deployment shape that matters.
