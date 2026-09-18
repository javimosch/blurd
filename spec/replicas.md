# Running more than one replica

Step 2 of `spec/distributed.md`. What changes when a second process exists, and
what was actually done about each item.

Verified end to end: **three replicas sharing one Postgres and one MinIO, 113/113
conformance through a load balancer**, surviving a `SIGKILL` of one of them.

```bash
BLURD_DB_BACKEND=postgres \
BLURD_DB_DSN=postgresql://blurd:blurd-dev-secret@postgres:5432/blurd \
docker compose --profile pg --profile scale up -d --scale blurd=3
```

There is still **no shared queue**, and none is needed: each replica processes
what it accepts, and any replica can serve any result because both stores are
shared.

## 1. Job ownership, and a reaper instead of startup recovery

The old `_recover()` requeued *every* unfinished job at startup. Correct with one
process; catastrophic with peers — a restarting replica would seize jobs another
replica was actively running.

Every job now carries `owner_id`, stamped by the instance that accepted it, and
every serving process heartbeats into `instances`. `reap()` reclaims only jobs
whose owner is **no longer live**:

| job kind | when its owner dies |
|---|---|
| `url` | adopted by the reaper and requeued — no payload, any replica can re-fetch |
| upload | failed with a recoverable "resubmit", naming the previous owner |

It runs at startup *and* every 30 s, so a peer's crash is picked up without
waiting for someone to restart. `tests/multi_replica.py` asserts the properties
that matter, including the one that is easy to get wrong: **a live peer's job is
never touched.**

## 2. Concurrent schema migration

Three pods starting together would run the migration and the schema script at
the same moment. `db.init()` now holds `pg_advisory_lock` for the duration. On
SQLite it is a no-op, because the instance guard means peers cannot exist.

## 3. `external_id` binding races

`bind_code` used to SELECT then INSERT. Two replicas can both find nothing and
both insert. The unique index is now the arbiter: a unique violation is caught,
the row re-read, and the normal conflict logic applied.

## 4. Bounding database connections

`ThreadingHTTPServer` spawns a thread per connection and never bounds them. With
SQLite that is merely wasteful; with Postgres each thread opens its own
connection, so a burst walks into `max_connections` and takes the database out
for *every* replica at once.

Replaced with a bounded pool (`http_threads`, default 32), which caps
connections at roughly `http_threads + workers + 1` per replica. Size it against
`max_connections ÷ replicas`.

Keep-alive is deliberately off (`HTTP/1.0`): with a bounded pool, idle
keep-alive connections would hold threads and starve new requests. The cost is a
handshake per request — noise next to a 130 ms redaction.

> This bounds connections; it is **not** a connection pool. Each thread still
> opens its own. A real pool would let 32 threads share, say, 8 connections.
> That is the better answer and it is not done.

## 5. Per-process caches

The 3-second stats memo, the detector cache and the legacy-thumb flag are
per-process. Header counters can differ slightly between replicas for a few
seconds. Left alone deliberately — the alternative is a shared cache, which is a
new dependency to solve a cosmetic problem.

## 6. Graceful drain

On `SIGTERM` a replica stops accepting (`/v1/health` returns **503** with
`"status": "draining"`, so the balancer drops it), lets in-flight work finish
for `drain_seconds` (default 20), then exits. Measured: 15 large images queued,
`SIGTERM` sent, **all 15 completed, none failed**.

Set `terminationGracePeriodSeconds` above `drain_seconds`; compose uses
`stop_grace_period: 40s`.

## What running three replicas actually found

A bug no single instance could hit: all three share the models volume and pulled
at boot simultaneously, colliding on a fixed `.part` temp file — one renamed it
while another was still writing, and the loser died with a `FileNotFoundError`
on a path that plainly existed. The temp name is now unique per process, and a
download that fails checks whether a peer finished the same file first.

The wider lesson: **an init container is the better answer for replicas.**
`BLURD_PULL_MODELS=1` is convenient for one container and a thundering herd for
several.

## Still open

- **A real connection pool** (see 4).
- **No SQLite → Postgres data migration.** A fresh Postgres starts at the
  current schema; moving an existing instance means export/import, unwritten.
- **No per-tenant fairness.** The queue is FIFO per replica, so one tenant's
  backlog still delays others on that replica.
- **Autoscaling** should key on `queue.queued` from `/v1/stats`, not CPU — CPU
  sits high by design. No HPA manifest is written.
