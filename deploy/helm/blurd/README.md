# blurd Helm chart

Face and licence-plate redaction. The service stores only the redacted image,
never the original.

**Choosing a deployment shape** — Compose or Kubernetes, which metadata backend,
which blob backend — is [`docs/deployment.md`](../../../docs/deployment.md).
This page is the chart reference.

```bash
helm install blurd ./deploy/helm/blurd \
  --set dashboard.password=change-me
```

That gives you one replica on SQLite with blobs in the pod — fine for a look,
wrong for production. The production shape is below.

## What the chart refuses to do

Most of the value here is in what it will not render. Each of these is a
failure that either happens silently at runtime or shows up as something that
looks like a different problem entirely:

| refused | because |
|---|---|
| `db.backend=sqlite` with `replicaCount > 1` | SQLite is a single-writer file; two writers corrupt it silently. blurd refuses the second instance, so the rollout would crash-loop one pod at a time. |
| `db.backend=sqlite` with autoscaling | the HPA would scale into exactly that. |
| `storage.backend=local` with `replicaCount > 1` **and no shared volume** | blobs written by one pod are invisible to the others: intermittent 404s on a fraction of reads, not an outage — much worse to diagnose. A shared (RWX) volume is accepted; see below. |
| no `resources.limits.memory` | blurd sizes its worker pool from the **cgroup**. With no limit the cgroup says "max", blurd falls back to the *node's* memory, starts as many workers as the node could feed, and is OOM-killed against a limit it never saw. |
| no `resources.limits.cpu` | same, for cores. |
| `terminationGracePeriodSeconds <= drainSeconds` | the kubelet would SIGKILL a pod mid-drain, losing the uploads queued in its memory. |
| `dashboard.enabled` with no password | the dashboard would be inert anyway. |
| postgres/mongo with no DSN, s3 with no endpoint | fails at first use instead of at install. |
| no model source at all | the pod starts healthy and then fails **every** submission. |

## Production: several replicas

Metadata in a shared store, blobs in object storage. No PVC, no StatefulSet —
replicas are interchangeable.

```bash
kubectl create secret generic blurd-db \
  --from-literal=dsn='postgresql://blurd:...@postgres:5432/blurd'
kubectl create secret generic blurd-s3 \
  --from-literal=accessKey=... --from-literal=secretKey=...
kubectl create secret generic blurd-dashboard \
  --from-literal=dashboardPassword=...

helm install blurd ./deploy/helm/blurd \
  --set replicaCount=3 \
  --set db.backend=postgres --set db.existingSecret=blurd-db \
  --set storage.backend=s3 \
  --set storage.s3.endpoint=http://minio.minio.svc:9000 \
  --set storage.s3.bucket=blurd \
  --set storage.s3.existingSecret=blurd-s3 \
  --set dashboard.existingSecret=blurd-dashboard \
  --set resources.limits.memory=1Gi --set resources.limits.cpu=4
```

Swap `db.backend=mongo` and a `mongodb://` DSN for the MongoDB variant. Both
pass the same 113 conformance checks; Postgres is the recommendation unless you
already operate MongoDB.

### Several replicas without object storage

Object storage is the recommendation, because there is nothing to share. But if
you have a ReadWriteMany volume (NFS, CephFS, EFS, Azure Files) and would rather
not run MinIO, that works too — measured at 113/113 with three replicas sharing
one volume behind a load balancer:

```bash
helm install blurd ./deploy/helm/blurd \
  --set replicaCount=3 \
  --set db.backend=postgres --set db.existingSecret=blurd-db \
  --set storage.backend=local \
  --set persistence.enabled=true --set persistence.accessMode=ReadWriteMany \
  --set persistence.size=200Gi \
  --set dashboard.existingSecret=blurd-dashboard
```

For an `existingClaim` whose access mode the chart cannot inspect, say so
explicitly with `--set persistence.shared=true`. The chart will not assume it:
guessing wrong reintroduces the split-blob failure above, silently.

Size it for the blobs — ~333 kB per image, so ~360 GB per million. That storage
bill is the reason object storage is the default recommendation.

## Sizing

blurd reads its cgroup and chooses its own worker count. **The memory limit is
the sizing knob** — leave `workers` empty.

```
peak resident  ~=  120Mi  +  90Mi x workers  +  queue budget
```

| `limits.memory` | workers | throughput (1280 px) |
|---|---:|---|
| 512Mi | 3 | ~10 img/s |
| 1Gi | 7 | ~13 img/s |
| 2Gi | 7 | ~13 img/s, deeper queue |

Throughput is ~3.4 images/s per physical core — blurd is CPU-bound, so scale
CPU for speed and memory for concurrency and queue depth. Beyond ~7 workers a
single pod stops gaining; add replicas instead.

## Behaviour under load

There is **no shared job queue and none is needed**: each replica processes what
it accepts, and any replica serves any result. The queue is per-replica, bounded
by **bytes** rather than job count, and overflow answers **503 with
Retry-After** — backpressure, not failure. Sustained 503s mean the deployment is
at capacity: raise `replicaCount`, or slow the producer.

## Rolling updates and drain

On SIGTERM a replica reports 503 on `/v1/health`, so the readiness probe pulls
it from the Service endpoints, and then finishes the images it already accepted.
`maxUnavailable: 0` means a replica is never removed before its replacement is
ready.

Queued **uploads** live in the accepting replica's memory — blurd never writes
source images to disk, which is the guarantee the whole service rests on — so a
pod SIGKILLed before draining fails those jobs with a recoverable "resubmit".
That is why `terminationGracePeriodSeconds` must exceed `drainSeconds`.

## Models

The detector models are not baked into the image: one carries a GPL-3.0 lineage,
so shipping it inside an image is a licence decision, not a packaging one. An
init container fetches them per pod. For an air-gapped cluster, pre-populate a
ReadOnlyMany volume:

```bash
--set models.initContainer.enabled=false --set models.existingClaim=blurd-models
```

Fetching in an **init** container rather than at startup is deliberate: a pod
that cannot get its models never becomes Ready, instead of starting healthy and
failing every submission.

## Security

The image runs as uid 65532 and the home is chowned in the Dockerfile rather
than relying on the pod's `fsGroup` — not every CSI driver applies fsGroup, and
a chart that only works on some of them is worse than one that does not work at
all. `automountServiceAccountToken` is off: blurd calls no Kubernetes API.

Dashboard credentials authenticate the dashboard only and never a `/v1` call;
an API key never opens the dashboard. Conformance asserts both directions.

## Verify

```bash
helm test blurd            # asserts healthy AND on the configured backends
kubectl exec deploy/blurd -- python3 run.py keys add my-app
kubectl exec deploy/blurd -- python3 run.py keys export > keys.json
kubectl exec -i deploy/blurd -- python3 run.py keys import - < keys.json
```

`helm test` deliberately checks more than reachability: a pod reporting
"healthy" while pointing at the wrong metadata store is the failure it exists to
catch.

## Status

The chart lints clean, renders valid manifests for every supported combination
(schema-validated against the Kubernetes 1.29 API), and every guardrail above is
covered by a test in `tests/helm_guardrails.sh` (30 checks). The pod shape — non-root, both
volumes, a 512Mi limit — passes all 113 conformance checks under Docker.

**It has not yet been applied to a live cluster.** No cluster was reachable from
the development environment, so scheduling, CSI behaviour, ingress and HPA
behaviour are unverified in situ.
