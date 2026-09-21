---
title: blurd
layout: default
nav_order: 1
---

# blurd

Face and licence-plate redaction for image pipelines. **The original is never
stored** — only its SHA-256, its dimensions, and the redacted output.

[Deployment guide](deployment.html){: .btn .btn-primary }
[Source](https://github.com/javimosch/blurd){: .btn }

---

## What it is

A single service that takes an image, blurs the faces and plates in it, and
hands back a redacted copy plus a job record. It is machine-to-machine: your
backend talks to it, not your users' browsers.

```
producer  ──POST /v1/images──▶  blurd  ──▶  redacted blob + job
consumer  ──GET /v1/blobs/by-code/<your own id>──▶  the redacted image
```

Submission is **asynchronous from the start**, because an HTTP request will not
hold for a batch. You get a job you can query at any point — by job id, by the
source SHA, by a metadata filter, or by **your own unique code**, which is a
primary key rather than a metadata row. That last one is the point: it makes a
consumer lookup ~13 µs where a metadata filter is ~7 ms.

## What it deliberately does not do

- **It does not store your originals.** Not on disk, not in the database, not
  while queued. Spooling queued uploads would be the obvious durability fix and
  it is refused for this reason.
- **It does not promise anonymisation.** Detection is a model; it misses things.
  blurd reports confidence and flags low-confidence results for review. Treat it
  as a large reduction in exposure, not as compliance.
- **It is not a photo editor.** One job: redact, store, serve.

## Numbers, all measured

| | |
|---|---|
| throughput | ~3.4 images/s per physical core (CPU-only; no GPU required) |
| a single instance | ~1.16 M images/day |
| memory | 120 MB + 90 MB per worker, sized automatically from the cgroup |
| stored per image | ~333 kB blob + ~19 kB metadata |
| consumer lookup by your own code | ~13 µs |
| conformance | 113 black-box checks, on every backend |

## Choices it gives you

| | options |
|---|---|
| metadata | SQLite · PostgreSQL · MongoDB |
| blobs | local disk · any S3-compatible store |
| scale | one instance · N replicas behind a load balancer |
| deploy | CLI · Docker · Docker Compose · Kubernetes (Helm) |

Every combination passes the same 113 checks. Which to pick, and why, is in the
[deployment guide](deployment.html).

## Multi-tenant by construction

One instance serves several applications without them seeing each other. A
scoped key **is a tenant**, not a filter: tags, metadata and your unique codes
are namespaced per tenant, so two apps can use the same filename for different
images. An out-of-scope read returns **404, never 403** — a 403 would confirm
the resource exists.

```bash
blurd keys add app-acme  --scope-tag acme
blurd keys add app-fleet --scope-meta appId=fleet
```

## Try it

```bash
git clone https://github.com/javimosch/blurd && cd blurd
./demo.sh          # blurd + a sidecar standing in for the two external apps
```

## Where to go next

| | |
|---|---|
| running it anywhere | [Deployment guide](deployment.html) |
| the wire contract, schema, blob layout | `spec/` in the repo |
| architecture rules and why each exists | `AGENTS.md` |
| contributing | [CONTRIBUTING.md](https://github.com/javimosch/blurd/blob/main/CONTRIBUTING.md) |
| security properties and reporting | [SECURITY.md](https://github.com/javimosch/blurd/blob/main/SECURITY.md) |

---

blurd is [AGPL-3.0](https://github.com/javimosch/blurd/blob/main/LICENSE). The
plate detector derives from YOLOv9 (GPL-3.0); the face detector is Apache-2.0.
Neither model is redistributed by this repository — both are fetched at runtime.
[Which models, and why](models.html).
