"""Embedded guide (cli-guide-spec): everything an agent needs, no web docs."""

from . import __version__

GUIDE = f"""
blurd {__version__} - image redaction (faces + licence plates) as CLI, API and dashboard

WHAT IT DOES
  Detects faces and licence plates in an image, redacts them (mosaic by
  default), stores ONLY the redacted result, and links it to the sha256 of the
  source. The source image itself is never written to disk.

  Submission is ASYNCHRONOUS: you get a job id immediately and poll it. A
  producer never holds an HTTP request open while an image is processed.

  Attach your own unique code (--code, often the filename) and a consumer can
  fetch the redacted image by that code in a single indexed lookup -- ~13 us of
  query time with 50k images stored.

THREE WAYS TO RUN THE SAME COMMANDS
  blurd blur photo.jpg                     local: runs the pipeline in-process
  blurd serve --daemon                     host the API + dashboard
  blurd --remote http://host:8770 \\
        --api-key blk_... blur photo.jpg   same command against a remote daemon

QUICK START
  blurd models pull --all                  download detectors (~8 MB, once)
  blurd blur photo.jpg --code cam3/IMG_42.jpg --tag fleet --meta site=paris
  blurd get --code cam3/IMG_42.jpg         the consumer lookup
  blurd download --code cam3/IMG_42.jpg --out redacted.jpg
  blurd list --tag fleet
  blurd jobs list
  blurd keys add ci-pipeline               create an API key (shown once)
  blurd dashboard-password hunter2
  blurd serve --daemon                     then open http://127.0.0.1:8770

COMMANDS
  blur <path|-> [--url URL]   Redact one image. '-' reads bytes from stdin.
      --code CODE             Your own unique id, indexed. Usually the filename.
                              A consumer fetches by this; re-submitting a known
                              code short-circuits without refetching.
      --async                 Return the job at once (needs --remote/a daemon).
      --wait SECONDS          How long to block (default 120).
      --on-conflict POLICY    reuse (default) | replace | reject, for when the
                              code already maps to different bytes.
      --tag T (repeatable)    Attach a tag.
      --meta k=v (repeatable) Attach metadata.
      --mode pixelate|blur|solid
      --face-score F          Min face confidence (default 0.6)
      --plate-score F         Min plate confidence (default 0.35)
      --ttl SECONDS           Keep the redacted blob only N seconds (60..2592000);
                              the record survives, the bytes are pruned on expiry
                              and a resubmit regenerates them. --ttl 86400 = 24 h.
      --force                 Reprocess even if a cached artifact exists.
      --out FILE              Also write the redacted image to FILE.
  jobs list [--status S] [--code C]   Recent jobs and queue depth.
  jobs get <job_id> [--wait S]        One job; --wait long-polls.
  list                        Filter stored artifacts.
      --code C                Exact code, or a trailing * for a prefix scan.
      --tag T, --meta k=v, --sha PREFIX, --needs-review,
      --since ISO, --until ISO, --limit N, --offset N
  get <sha>|--code CODE       Full record for one image.
  download <sha>|--code CODE --out FILE   Fetch the redacted image.
  delete <sha>                Remove an image, its artifacts and its blobs.
  stats                       Counts, bytes stored, top tags.
  serve [--host H --port P]   Run the API + dashboard. --daemon to background.
  stop | status               Daemon lifecycle.
  keys add <name> [--scope-tag T] [--scope-meta K=V]
                              Create a key. With a scope it is restricted to
                              images carrying ALL of those labels -- which also
                              gives it its own TENANT: its unique codes live in
                              their own namespace (two apps can both use
                              "IMG_0042.jpg"), its uploads are stamped with the
                              scope automatically, and it can neither see nor
                              label another tenant's images. See
                              spec/scoped-keys.md.
  keys add <name> --key SECRET   Register an existing key (e.g. one another
                              instance already issued) instead of minting one.
  keys list | keys revoke <id>
  keys export [--out FILE]     Export key records: sha256 hashes + scopes,
                              NEVER plaintext. Makes the same key work on a
                              second instance without re-showing it.
  keys import <file|'->        Import an export file. Idempotent: a key_sha
                              already present is skipped, an id collision is
                              remapped. Audited as key.import.
  dashboard-password <pw>     Set the dashboard basic-auth password.
  dashboard-keys enable --secret S | disable | status
                              Allow minting API keys from the dashboard.
                              OFF by default: a minted key outlives the
                              dashboard password, so turning one shared browser
                              login into permanent API access is opt-in and
                              needs a SECOND secret, sent as X-Blurd-Admin-Secret.
                              Listing and REVOKING keys is always available --
                              revocation is fail-safe and you want it fast.
  audit [--limit N]           Privileged mutations: key.create, key.import,
                              key.revoke,
                              image.delete, with channel and source address.
  daemon start|stop|status    Daemon lifecycle; idempotent (a second start or
                              a stop on a stopped daemon reports the state and
                              succeeds). Server also answers GET /_health and
                              POST /_shutdown (loopback only).
  feedback "<msg>" [-kind K]  Dual-write feedback: this deployment's store AND
                              the shared relay (FEEDBACK_RELAY=off disables).
                              Never fails the caller.
  models list | models pull [NAME|--all]
  config get <key> | config set <key> <value>
  storage show | storage check
                              Inspect the blob backend, or round-trip a probe
                              object through it.
  migrate-blobs --to s3|local [--batch N] [--delete-source]
                              Copy blobs between backends. Resumable: anything
                              already present at the destination is skipped.
  vacuum                      Reclaim free pages after a migration or a large
                              delete. Blocking; stop the daemon first.
  doctor                      Check interpreter, deps, models, DB.
  guide | version

PROFILES & TTL
  A profile is the processing configuration: redact mode, detector models and
  thresholds, output format, and optional storage.ttl. The shipped default is:
    {{"detect":{{"face":{{"min_score":0.6,"model":"yunet-2023mar"}},"max_side":1280,
      "plate":{{"min_score":0.35,"model":"yolov9t-512-plates"}}}},
     "output":{{"format":"jpeg","max_side":0,"quality":90}},
     "redact":{{"expand":0.18,"mode":"pixelate",
      "shape":{{"face":"ellipse","plate":"rect"}},"strength":0.06}},"version":1}}
  Any subset may be overridden per request: ?profile={{"storage":{{"ttl":86400}}}}
  on POST /v1/images, or --ttl/--mode/--face-score on the CLI (they build the
  same override). profile_hash is computed from the effective profile, and the
  artifact cache key is (source_sha, profile_hash) -- so a TTL'd image and a
  permanent one are DIFFERENT artifacts, never shared cache space.
  storage.ttl semantics: the record stores expires_at; once past, blob reads
  answer 410 resource_expired and a sweep prunes the bytes. Metadata, sha,
  detections and tags all survive -- a resubmit regenerates the blob.

OUTPUT CONTRACT
  JSON on stdout by default: {{"version":"1.0","data":...,"timestamp":...}}
  --human for a readable summary. Logs and progress go to stderr, always.
  Errors go to stderr as {{"ok":false,"error":{{code,type,message,details,
  recoverable,retry_after,suggestions}}}}.

EXIT CODES
  0 ok | 85 invalid argument | 87 validation | 92 not found | 94 conflict
  105 upstream timeout | 106 api unavailable | 107 auth failed | 110 internal

REST API (all /v1 endpoints except /v1/health need Authorization: Bearer)

  producer (the service that holds the originals)
  POST   /v1/images            raw image bytes (Content-Type: image/jpeg),
                               or {{"url":"...","external_id":"...","tags":[]}}
                               query: ?code=&tags=a,b&metadata={{...}}
                                      &on_conflict=reuse|replace|reject
                                      &wait=SECONDS&force=1
                               -> 202 + job, Location: /v1/jobs/<id>
                                  (200 if it settled within ?wait)
  GET    /v1/jobs/<id>[?wait=N]  status; `result` once done
  GET    /v1/jobs              ?status=&code=&limit=&offset=  + queue depth

  consumer (the user-facing app's backend)
  GET    /v1/blobs/by-code/<code>   redacted bytes, ONE indexed lookup. ETag +
                                    304 on If-None-Match. The hot path.
  GET    /v1/images/by-code/<code>  full record by code
  GET    /v1/thumbs/by-code/<code>  thumbnail by code
  GET    /v1/images/<sha>           full record by source hash
  GET    /v1/blobs/<sha>            redacted bytes by source hash
  GET    /v1/images                 ?code=&tag=&meta.k=v&sha=&needs_review=
                                    &limit=&offset=   (browse path, not hot)
  DELETE /v1/images/<sha>
  GET    /v1/stats                  aggregate counters + queue depth
  GET    /v1/health                 unauthenticated liveness

DEPLOYMENT
  docker compose up -d                 blurd + MinIO (blobs in object storage)
  docker compose --profile pg    up -d  + Postgres, for the shared metadata store
  docker compose --profile mongo up -d  + MongoDB, the alternative
  docker compose --profile scale up -d --scale blurd=3
  One process per SQLite home: `serve` registers an instance and refuses to
  start if a peer is heartbeating, because SQLite is a single-writer file.
  The scope for running several replicas is in spec/distributed.md.

REPLICAS
  With postgres + s3, several replicas share metadata and blobs. No shared
  queue: each replica processes what it accepts, any replica serves any result.
    BLURD_DB_BACKEND=postgres BLURD_DB_DSN=... BLURD_STORAGE_BACKEND=s3
    docker compose --profile pg --profile scale up -d --scale blurd=3
  Every job carries the instance that owns it; a reaper reclaims only jobs whose
  owner has stopped heartbeating, so a restart never seizes a live peer's work.
  On SIGTERM a replica reports 503 (draining), finishes in-flight jobs for
  BLURD_DRAIN_SECONDS, then exits. BLURD_HTTP_THREADS bounds concurrent
  connections, and therefore database connections. See spec/replicas.md.

METADATA BACKENDS
  sqlite    a file under $BLURD_HOME     (dev, one container)
  postgres  a shared service             (several replicas)
  BLURD_DB_BACKEND=postgres BLURD_DB_DSN=postgresql://user:pass@host/blurd
  Needs the optional `psycopg[binary]`. Both backends pass the same 112
  conformance checks; the backend is invisible in behaviour. One process per
  SQLITE home is still enforced -- postgres lifts that, but the leases and
  pooling in spec/distributed.md step 2 must land before several replicas are
  safe. See spec/postgres.md.

STORAGE BACKENDS
  local  filesystem under $BLURD_HOME/blobs   (dev, one container)
  s3     any S3-compatible object store       (Docker/k8s)
  Selected by BLURD_STORAGE_BACKEND, with BLURD_S3_ENDPOINT / _BUCKET /
  _ACCESS_KEY / _SECRET_KEY / _PREFIX. The storage key is the same in both, so
  switching rewrites no database rows; `blurd migrate-blobs --to s3` copies an
  existing instance over and is resumable. Blobs are ~95% of stored bytes:
  moving them takes an instance from ~360 GB per million images to ~19 GB.
  Cost: about +1.2 ms per read against MinIO on localhost. See spec/storage.md.

RESOURCES AND SIZING
  workers defaults to "auto": the lower of what CPU and MEMORY allow, read from
  the cgroup when running in a container (the host's figures are a fiction
  there). Memory is ~130 MB base + ~130 MB per worker, fitted to measurement.
  An explicit BLURD_WORKERS=N is honoured, with a warning at startup if it will
  not fit -- better than finding out from the OOM killer mid-job.
  `blurd doctor` prints the whole budget. Re-measure: bash bench/memory.sh
  Freed memory is returned to the OS between jobs (BLURD_MALLOC_TRIM=0 to
  disable): -22% peak RSS for -5% throughput. See spec/resources.md.

CAPACITY (measured, 4 physical cores, no GPU)
  ~3.4 images/second per physical core at 1280px; 13.4 img/s on this box,
  48,300 images/hour. Detection is ~110-130ms and nearly size-independent
  because it runs on a 1280px copy. The work is CPU-bound: throughput flattens
  after ~4 workers, and a process pool beats threads by only 14%.
  Storage: ~333 KB/image blob + ~19 KB database. 1M images is ~360 GB.
  GPU is out of scope: blurd targets CPU-only VMs, so capacity is bought in
  cores and sizing is linear.
  Reads peak ~800-1200/s; cache in front for read-heavy loads.
  Tune with: blurd config set workers N | blurd config set ort_threads N
  Benchmark it yourself: python3 bench/throughput.py --url ... --api-key ...
  Scaling, and whether blurd can run as a cluster: spec/capacity.md

LISTING AT SCALE (measured, 50k images + 50k jobs)
  Listings are cursor-paginated, not offset-paginated: pass the next_cursor
  from one page as ?cursor= on the next. Page 1000 costs what page 1 costs,
  and rows cannot be skipped or repeated by concurrent inserts.
  ?sort= created|faces|plates|size|review with ?direction=asc|desc -- each is
  index-backed. Date range is ?since=&until= (RFC3339).
  Totals stop counting at 10000 and set total_capped=true; the total is
  returned on the first page only, because paging cannot change it.
  A full dashboard grid page (list + 24 thumbnails + header) costs ~34 ms.
  See spec/scaling.md.

LOOKUP COST (measured, 50k stored images)
  by unique code      ~13 us   indexed, use this in the user-facing path
  by full sha256      ~10 us   indexed
  by metadata k=v     ~7 ms    matches many rows and sorts; a browse path
  by sha PREFIX       ~6 ms    a scan; convenience only

SCOPED KEYS (running several apps off one instance)
  blurd keys add app-acme --scope-tag acme
  blurd keys add app-fleet  --scope-meta appId=fleet
  A scope is a conjunction: every constraint must hold. It defines a tenant, so
  codes and labels are namespaced by it. Out-of-scope reads return 404, not 403,
  so a scoped key cannot probe for what exists. Writes contradicting the scope
  fail with 86/scope_violation. /v1/stats is computed within the scope.
  A scoped delete releases only that tenant's claim on shared bytes.

UNIQUE CODES (external ids)
  --code / external_id is the producer's own identifier, usually the filename.
  It is a PRIMARY KEY of (tenant, external_id): one code points at exactly one
  source image within a tenant, and several codes may point at the same one
  (identical bytes are stored once).
  Re-submitting a known code with on_conflict=reuse (the default) returns a
  finished job immediately without fetching or decoding anything.
  A known code with DIFFERENT bytes fails with 94/resource_conflict unless you
  pass on_conflict=replace -- silently repointing it would make a consumer's
  cached URL start returning a different photo.

CACHING / DEDUP
  The cache key is (sha256 of source bytes, profile_hash), never the sha alone.
  profile_hash covers the models, thresholds, redaction mode and output
  settings, so upgrading a model produces a new artifact instead of silently
  serving a stale redaction. Re-submitting a known pair returns
  "cached": true in ~1ms and merges any new tags/metadata.

ASYNC DURABILITY
  A queued `url` job survives a daemon restart: the daemon can re-fetch it.
  A queued raw-upload job does NOT -- its bytes are held in memory and never
  spooled to disk, because "the source image is never written to disk" is the
  point of this service. Those jobs fail on restart with a recoverable error
  telling the producer to resubmit.

LIMITS YOU SHOULD KNOW
  Detection recall is not 100%. Artifacts with no detections, or with a
  detection below 0.55, are flagged needs_review=true so a human can check
  them in the dashboard. Treat blurd as a strong first pass, not a guarantee.
"""


def text() -> str:
    return GUIDE.strip()


def as_json() -> dict:
    return {
        "version": __version__,
        "commands": {
            "blur": "Submit one image (path, URL or stdin) as a job",
            "jobs": "list | get async jobs, with queue depth",
            "list": "Filter stored artifacts (--code, --tag, --meta, --sha)",
            "get": "Full record, by source sha or --code",
            "download": "Fetch redacted image bytes, by sha or --code",
            "delete": "Remove an image and its artifacts",
            "stats": "Aggregate counters",
            "serve": "Run API + dashboard (optionally as a daemon)",
            "daemon": "start | stop | status the daemon (cli-daemon-spec)",
            "stop": "Stop the daemon",
            "status": "Daemon status",
            "help-json": "This catalog",
            "keys": "add | list | revoke | export | import API keys",
            "dashboard-keys": "enable | disable | status for dashboard key minting",
            "audit": "Recent privileged mutations",
            "dashboard-password": "Set dashboard basic-auth password",
            "feedback": "Send feedback to this deployment and the shared relay",
            "models": "list | pull detector models",
            "config": "get | set configuration values",
            "storage": "show | check the blob backend",
            "migrate-blobs": "Copy blobs between storage backends",
            "vacuum": "Reclaim free database pages (blocking)",
            "doctor": "Environment self-check",
            "guide": "This guide",
            "version": "Version info",
        },
        "global_flags": ["--json", "--human", "--remote URL", "--api-key KEY",
                         "--home DIR", "--help-json"],
        "output_formats": ["json", "human"],
        "exit_codes": {
            "0": "success", "85": "invalid_argument", "87": "validation_error",
            "92": "resource_not_found", "94": "resource_conflict",
            "105": "connection_timeout", "106": "api_unavailable",
            "107": "auth_failed", "108": "overloaded",
            "110": "internal_error",
        },
    }


def as_guide() -> dict:
    """The agent-skill JSON flavor of the guide (cli-guide-spec §1). Same
    content as `guide --human`, structured for a machine reader."""
    return {
        "tool": "blurd",
        "version": __version__,
        "one_liner": ("Redacts faces and licence plates in images, stores ONLY "
                      "the redacted result keyed by sha256(source) -- the "
                      "source image is never written to disk."),
        "model": ("A producer POSTs an image (optionally with its own unique "
                  "--code); a consumer fetches the redacted image by sha or by "
                  "that code. The cache key is (source_sha, profile_hash), so "
                  "a different blur profile is a different artifact. Scoped "
                  "API keys are tenants: each owns a namespace of codes and "
                  "labels, and cannot see another tenant's data (404, never "
                  "403). Keys are stored as sha256 only -- export/import moves "
                  "the hash, never the plaintext."),
        "loop": ("submit -> poll job -> read artifact. Submissions are async: "
                 "POST returns a job id; poll /v1/jobs/<id> (or --wait), then "
                 "fetch bytes or the record. A repeated submission is a dedup "
                 "hit (~1 ms, cached:true), not a reprocess."),
        "concepts": {
            "job": "async unit of work; queued, running, done or failed",
            "artifact": "one redacted output for (source_sha, profile_hash)",
            "external_id": "the producer's own code for an image; indexed "
                           "per-tenant for single-lookup consumer fetches",
            "scope": "a conjunction of tags/metadata on an API key; defines a "
                     "tenant namespace",
            "profile_hash": "hash of the redaction profile (mode, scores, "
                            "models); part of the cache key",
            "profile": "the processing configuration (redact mode, detector "
                       "models and thresholds, output format, storage.ttl). "
                       "Per-request JSON overrides merge into the defaults; "
                       "?profile={\"storage\":{\"ttl\":86400}} or --ttl 86400 "
                       "keeps a blob only 24 h -- the row survives, bytes are "
                       "pruned and resubmitting regenerates them",
            "needs_review": "artifact with weak detections, flagged for a "
                            "human in the dashboard",
        },
        "commands": as_json()["commands"],
        "examples": [
            "blurd models pull --all",
            "blurd blur photo.jpg --code cam3/IMG_42.jpg --tag fleet",
            "blurd get --code cam3/IMG_42.jpg",
            "blurd download --code cam3/IMG_42.jpg --out redacted.jpg",
            "blurd keys add app-acme --scope-tag acme",
            "blurd keys export --out keys.json && blurd keys import keys.json",
            "blurd serve --daemon --port 8770",
            "blurd blur photo.jpg --ttl 86400   # redacted blob lives 24 h, not forever",
            "blurd --remote http://host:8770 --api-key blk_... blur photo.jpg",
            "blurd daemon start|stop|status",
        ],
        "gotchas": [
            "Only the redacted image is stored; there is no way to retrieve "
            "the source later.",
            "Upload jobs are not durable across a restart (source bytes are "
            "never spooled); url jobs are requeued automatically.",
            "Cached results are keyed by (source_sha, profile_hash): change a "
            "threshold or model and every earlier artifact is bypassed, not "
            "overwritten.",
            "keys are sha256-only: a listing never shows a usable key; export "
            "carries hashes + scopes, which is what makes a key portable.",
            "Backpressure is HTTP 503 + Retry-After, never 502.",
        ],
    }
