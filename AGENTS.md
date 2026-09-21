# blurd — notes for agents

## Skills in this repo
`.agents/skills/` holds the things the code cannot tell you — why a decision
went the way it did, what was tried and failed, and which "obvious" move is a
trap. Read the relevant one **before** starting:

| skill | read it before |
|---|---|
| `blurd-testing` | running or writing any test; the invocations have load-bearing arguments |
| `blurd-memory-sizing` | changing worker/queue defaults, buffers, or claiming a memory win |
| `blurd-backends` | touching `db.py`, `db_sql.py`, `db_mongo.py`, `dialect.py`, `store.py` or any query |
| `blurd-releasing` | cutting a release: what to verify, in what order, and why |
| `blurd-deploying` | changing the Dockerfile, docker-compose or the Helm chart |

## Run it
```bash
./blurd guide              # full embedded documentation, no web docs needed
./blurd --help-json        # machine-readable command list + exit codes
./blurd doctor             # environment self-check
```

## Output contract
- JSON on **stdout**: `{"version":"1.0","data":…,"timestamp":…}`. `--human` for text.
- Two commands emit their document at the TOP level, not under `data`:
  `help-json` (the catalog) and `guide` (the guide doc). A wrapped catalog is
  not a catalog — `cli-spec-conformance` checks `.commands` at the root.
- Logs, progress and access logs on **stderr**, always.
- Errors on **stderr** as `{"ok":false,"error":{code,type,message,details,recoverable,retry_after,suggestions}}`.
- Exit code == `error.code`, and the HTTP status maps from the same table
  (`src/errors.py: HTTP_FOR_EXIT`). One error vocabulary for CLI and API.

## Interpreter
`./blurd` is a launcher that picks the first Python with `sqlite3`, `cv2`,
`numpy` and `onnxruntime`. On this machine the default `python3` is built
**without `_sqlite3`**, so calling `run.py` with the wrong interpreter fails
with an unhelpful ImportError. Use `./blurd`, or set `BLURD_PYTHON`.

## Where things live
| Concern | File |
|---|---|
| Wire + disk contract (ports must match) | `spec/` |
| Detectors (add a model here and in `src/models.py`) | `src/detect.py` |
| Cache identity | `src/canonical.py`, `spec/profile-hash.md` |
| Async queue, workers, unique codes | `src/jobs.py` |
| Orchestration + timings | `src/pipeline.py` |
| Local vs `--remote` transports | `src/client.py` |
| HTTP API + dashboard routes | `src/server.py` |
| Dashboard auth, CSRF, key gating | `src/auth.py`, `spec/dashboard-auth.md` |
| Scopes and tenancy | `src/scope.py`, `spec/scoped-keys.md` |
| Blob backends (local / S3) | `src/store.py`, `spec/storage.md` |
| Metadata dispatch (the facade, no queries) | `src/db.py` |
| Metadata backends: SQL | `src/db_sql.py`, `src/dialect.py`, `spec/postgres.md` |
| Metadata backends: documents | `src/db_mongo.py`, `spec/mongo.md` |
| Schema drift guard | `tests/schema_drift.py` |
| Multi-replica behaviour | `spec/replicas.md`, `tests/multi_replica.py` |
| Sizing and memory | `src/resources.py`, `spec/resources.md`, `bench/memory.sh` |
| The distributed path (all steps done) | `spec/distributed.md` |
| Seam guard | `tests/seam_check.py` |
| Throughput, capacity, scaling | `bench/throughput.py`, `spec/capacity.md` |
| Listing performance, pagination | `db_sql.py` / `db_mongo.py` (`bulk_labels`, `query_artifacts`), `spec/scaling.md` |
| Black-box conformance (154 checks) | `tests/conformance.py` |
| Two backends must answer identically (77 checks) | `tests/backend_parity.py` |
| Queue backpressure and the byte bound | `tests/queue_bytes.py` |
| **How to deploy anything** (the operator hub) | `docs/deployment.md` |
| Kubernetes chart, and what it refuses | `deploy/helm/blurd/`, `tests/helm_guardrails.sh` |
| Compose profiles (default / pg / mongo / scale) | `docker-compose.yml`, `deploy/nginx.conf` |
| Stand-in producer + consumer apps | `sidecar/` (Go) |

## Rules that matter
1. **Never persist the source image.** Only `sha256(source)`, dimensions, and
   the redacted output. `pipeline.process` drops the source bytes right after
   decoding; keep it that way.
2. **The cache key is `(source_sha, profile_hash)`**, never the sha alone.
   Changing a model or a threshold must produce a new artifact.
3. **Changing `DEFAULT_PROFILE` invalidates every cached artifact**, because it
   changes `profile_hash`. That is intended — but do it knowingly.
4. **The URL fetcher re-validates the IP at every redirect hop.** Do not
   replace it with a library that follows redirects internally; that
   reintroduces the SSRF bypass the manual loop exists to prevent.
5. **Adding an API field is additive only.** `spec/openapi.yaml` is the
   contract the Go/machin ports implement; removing or renaming a field breaks
   them silently.
6. **Never spool uploaded bytes to disk to make jobs durable.** It is the
   obvious fix for "a restart loses queued uploads" and it breaks rule 1.
   `url` jobs are requeued on restart; upload jobs fail with a resubmit hint.
7. **Validate cheaply at submit, not only in the worker.** An async API that
   returns 202 for a URL it will never fetch forces the caller to poll to learn
   it was rejected. `jobs.submit` calls `fetch.validate_url` synchronously;
   `fetch.fetch_url` still re-validates every redirect hop.
8. **`external_id` is a PRIMARY KEY, not a metadata row.** That is the whole
   reason a consumer lookup is 13 µs and a metadata filter is 7 ms. Do not
   "simplify" it into the `metadata` table.
9. **Dashboard credentials must never authenticate `/v1`, and an API key must
   never open the dashboard.** A conformance check asserts both directions.
10. **Key creation from the dashboard stays opt-in behind a second secret.**
    Revocation is fail-safe and always available; creation turns one shared
    browser password into permanent machine access that survives changing it.
11. **Every `/ui-api` mutation needs the CSRF token.** Do not rely on the CORS
    preflight that a `DELETE` or a JSON POST happens to trigger — that is an
    accident of content type, not a defence.
12. **A scope is a tenant, not a `WHERE` clause.** `tags`, `metadata` and
    `external_ids` carry a `tenant` column, and `external_ids` is keyed
    `(tenant, external_id)`. Removing either makes two apps' identical filenames
    collide, and makes one tenant's labels visible on bytes deduplicated with
    another's. Every new read path must take `scope` and pass it down.
13. **Out-of-scope reads return 404, never 403.** A 403 confirms the resource
    exists, which is exactly what a scoped caller must not be able to learn.
14. **Listings must not use per-row helpers.** `tags_of`/`metadata_of`/
    `codes_of` are for a single record; a page uses `bulk_labels()`. Three
    queries per row is 602 queries for a 200-row page.
15. **Never `SELECT a.*` from `artifacts` in a listing**, and never put a blob
    in a row that listings scan. Thumbnails live in `thumbs` for that reason.
16. **Serving bytes must not build a JSON record.** `thumb_for`/`blob_ref` are
    one indexed lookup; `client.get()` is seven queries and a document.
17. **Pagination is keyset, not OFFSET.** OFFSET is O(offset) and skips or
    repeats rows when the table is being written to. Counts are capped and
    computed only on the first page.
18. **Only offer sorts that have an index including `id`.** The tiebreaker is
    what makes the cursor seekable; an unindexed sort is a full scan into a
    temp b-tree.
19. **Detectors have different threading rules.** onnxruntime sessions are
    thread-safe and shared; `cv2.FaceDetectorYN` is stateful and MUST be
    per-thread. Sharing it corrupts under concurrency and throws
    "(-215:Assertion failed) buf.shape" — invisible in any single-image test.
20. **Benchmark with unique bytes.** Re-submitting the same image measures the
    dedup cache (~1 ms), not the pipeline (~130 ms). `bench/throughput.py`
    appends a unique JPEG comment segment for this.
21. **Nothing outside `src/store.py` may build a blob path.** The whole point
    of the seam is that `local` and `s3` are interchangeable; one
    `Path(blobs_dir) / rel` elsewhere breaks that silently for one code path.
22. **Write the object before the database row.** An orphaned object is
    garbage; a row pointing at a missing object is a broken record.
23. **A container cannot run `blurd config set` before it starts**, and a
    running daemon reads its config once. Anything a deployment must set needs
    an entry in `config.ENV_OVERRIDES`.
24. **SQL in `db_sql.py` must work on BOTH engines.** `ON CONFLICT` not
    `INSERT OR IGNORE`; `RETURNING id` not `lastrowid`; alias every subquery;
    no `rowid`. Only placeholders and engine metadata belong in `dialect.py`.
25. **A SQLite `REAL` is `DOUBLE PRECISION` in Postgres, never `REAL`.** 8-byte
    vs 4-byte. Getting this wrong breaks keyset pagination in a way that looks
    like a logic bug. `tests/schema_drift.py` fails on it specifically.
26. **Sort columns that can be NULL need an expression**, not a bare column:
    NULL ordering differs between engines and a NULL cursor value matches
    nothing. See `JOB_SORTS`.
27. **Never reclaim a job whose owner is still heartbeating.** `reap()`
    replaced startup recovery for exactly this reason; requeueing everything
    unfinished seizes a live peer's work. `tests/multi_replica.py` asserts it.
28. **Anything that runs at startup must survive N replicas doing it at once** —
    schema migration (advisory lock), model downloads (per-process temp file),
    code binding (let the unique index arbitrate).
29. **Never size anything from `os.cpu_count()` alone.** It ignores memory and
    reports the host's cores inside a container. Use `src/resources.py`, which
    reads the cgroup. The constants there are FITTED TO MEASUREMENT -- re-run
    `bench/memory.sh` and update them if the pipeline changes what it holds.
30. **`db_sql.py` is the only module that executes SQL**, and `db.py` is a
    facade containing no queries at all. Enforced by `tests/seam_check.py` —
    run it, it is one second. `scope.py` may build WHERE fragments (the
    predicate is the scope's own logic) but must not execute them. This seam is
    why MongoDB was a new module rather than a rewrite; do not weaken it.
31. **One process per SQLite home.** `serve` registers an instance and refuses
    to start if a peer is heartbeating. Do not weaken this — two writers on one
    SQLite file corrupt it silently.
32. **A startup migration must not rewrite a large table.** Copying blobs and
    dropping a column on a 900 MB database ran for minutes and doubled the file
    via WAL. Migrate lazily, move backlogs in batches from an explicit command,
    and never `VACUUM` automatically.
33. **`db.init` migrates before running `schema.sql`.** The script creates an
    index on `tags(tenant, ...)`, which does not exist on a pre-0.4 database —
    running it first fails on the very column the migration adds.

34. **Every function `db.py` dispatches must exist in BOTH backends**, with the
    same name, arguments and return shape. `tests/seam_check.py` asserts it,
    because a missing one fails at the call, in production, on whichever
    endpoint happens to need it.
35. **`_sync_labels()` is the only writer of `artifacts.lbl`** in the Mongo
    backend, and `scope_allows_sha` must read the canonical `labels` instead —
    an authorisation check that consults a cache is one that can be stale.
36. **The onnxruntime CPU arena stays off.** It costs ~40 MB per worker for no
    reliable throughput gain, and OOM-killed a 512 MB container that completed
    30/30 without it. The constants in `src/resources.py` assume it is off.
37. **Do not claim a memory win that only shows as a smaller number in one
    run.** `tracemalloc` is blind here (numpy/cv2 allocate outside Python's
    allocator) and single-job RSS sampling is noisier than the effects being
    measured. Prove it with `bench/memory.sh` plus a memory-capped container.
38. **The queue is bounded by BYTES, not job count.** Queued uploads are
    resident memory (rule 6 forbids spooling them), so a job-count bound
    promises nothing: 1000 jobs x ~1.5 MB is ~1.5 GB on a box sized for 750 MB.
    Admission and reservation are one critical section, and `_release_payload`
    is the only place `_payloads` shrinks — a pop that forgets to decrement
    leaks budget until the queue refuses everything, while the process looks
    healthy.
39. **Backpressure is 503 (code 108, `overloaded`), never 502.** 502 means
    "upstream returned garbage" and balancers eject a backend on repeated 502s
    — the opposite of what a queue shedding load wants. A draining replica
    returns 503 for the same reason.

40. **A job's output is identified by `(source_sha, profile_hash)`, not by an
    artifact row id.** `?force=1` and `on_conflict=replace` delete the artifact
    and insert a new one, leaving every earlier job pointing at an id that no
    longer exists — those jobs reported `result: null` while the image they
    produced was sitting right there. `job_dict` falls back to the current
    artifact for the pair.
41. **The image runs as uid 65532 and chowns its home in the Dockerfile.** Do
    not depend on the pod's `fsGroup` instead: not every CSI driver applies it,
    and a deployment that works on some clusters is worse than one that fails
    on all of them.
42. **Anything that creates the home must mount the home.** `models pull`
    initialises the whole home (it creates `blobs/` alongside `models/`), so an
    init container mounting only the models volume writes into the image's
    directory and fails with EACCES on every deploy.
43. **`insert_artifact` returns None on a dedup race**, not an exception.
    Two workers can legitimately process the same `(source_sha, profile_hash)`
    at once; the loser must resolve to the winner's row. Never let the unique
    constraint escape as `internal_error`, and never share a `.part` temp name
    between writers in `store.put` -- both were real races found in a 1k-image
    batch.
44. **A container with no cgroup limits sizes itself from the NODE.** That is
    why the Helm chart refuses to render without `resources.limits`. It is the
    single most damaging misconfiguration available and it presents as a random
    OOM kill.
44. **Daemon lifecycle is idempotent by spec** (cli-daemon-spec): a second
    `daemon start` reports the running instance and succeeds; `stop`/`daemon
    stop` on a stopped daemon is a no-op success, not a 94. Reverting these to
    errors fails `cli-spec-conformance` (28 checks — run it after touching
    `main.py`'s parser, `daemon.py`, `guide.py`, or the `/_health`/`/_shutdown`
    routes).
45. **`/_shutdown` is loopback-only** — a remote caller cannot stop the
    process at all, which is stricter than the spec's token gate. It SIGTERMs
    the process so it takes the same drain path as `blurd stop`.
46. **`feedback` never fails and dispatches before `client.build`** — an agent
    without an API key must still be able to report a bug. Submission is open,
    reads need an operator key. The `id` is client-generated so the dual-write
    (app + `FEEDBACK_RELAY`) is idempotent.

44. **TTL deletes bytes, keeps the row.** `storage.ttl` is part of the
    profile (and profile_hash) so an expiring output never shares cache
    identity with a permanent one. On expiry the blob and thumb are pruned —
    by the reaper sweep and lazily on read — while the artifact, codes and
    detections stay; fetches answer 410 `resource_expired`, never 404, so a
    poller can tell "resubmit" from "never existed". `expires_at` is counted
    from processing, not submission: queued time must not eat the blob's life.
    sqlite3.Row has no `.get` and `x in row` tests VALUES — check column
    presence with `row.keys()`.

## Verifying a change
```bash
./demo.sh                      # blurd + Go sidecar, prints keys and URLs
python3 tests/seam_check.py     # db_sql.py owns all SQL; both backends in parity
python3 tests/schema_drift.py   # the two SQL schemas still agree
python3 tests/multi_replica.py  # ownership + reaping (needs a LIVE shared backend)
python3 tests/queue_bytes.py --url … --api-key … --image …   # backpressure
python3 tests/backend_parity.py --a … --b … --image …        # two backends agree
python3 bench/throughput.py --url http://127.0.0.1:8771 --api-key "$KEY"
python3 tests/conformance.py --bin ./blurd --url http://127.0.0.1:8771 \
    --api-key "$(cat /tmp/blurd-demo/sidecar.key)" --image /path/to/test.jpg
```
154 checks — but **only if you pass `--dashboard-password` and scope the two
keys with the tags `conformance-a` / `conformance-b`**; otherwise whole sections
are skipped or fail for the wrong reason. See the `blurd-testing` skill. They
test a binary and a URL, never Python imports, so the same file is the
acceptance test for a port. Run it on **every backend the change could touch**.

## The sidecar
`sidecar/` is a single-file Go program standing in for the two external apps.
Build with `cd sidecar && go build -o blurd-sidecar .`. It exists to make the
topology testable: the browser talks only to the sidecar, and the blurd API key
never leaves that process.

46. **Public reads are rule-gated and sha-addressed, never by code.** Codes are
    caller-chosen, enumerable strings; `/pub/blobs/<sha>` only. Rules are
    evaluated at read time against the image's labels so deletion revokes
    immediately — and a rule can pin which tenant's labels qualify, because
    tags are caller-controlled. Non-matching and missing both answer 404.
47. **Rate limiting is per-IP fixed-window, in `src/server.py`.** Public paths
    get 60/min, everything else 300/min; `/_health` and `/_shutdown` are exempt
    so an orchestrator cannot lock itself out. Blocked hits flush to `audit`
    as ONE row per (class, ip, window) — grouped, not per-request. Audit rows
    (including these events) are pruned past 30 days in the reaper sweep.
