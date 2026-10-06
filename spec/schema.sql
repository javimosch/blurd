-- blurd storage schema (SQLite, WAL).
-- PORTABILITY CONTRACT: this file is the source of truth for every blurd
-- implementation (python / go / machin). No ORM, no migration framework:
-- apply it with `executescript` (or equivalent) at startup; it is idempotent.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Source images are NEVER stored. Only their hash and shape.
CREATE TABLE IF NOT EXISTS images (
  source_sha   TEXT PRIMARY KEY,          -- sha256 hex of the raw source bytes
  byte_size    INTEGER NOT NULL,
  width        INTEGER NOT NULL,
  height       INTEGER NOT NULL,
  mime         TEXT NOT NULL,
  source_kind  TEXT NOT NULL,             -- 'url' | 'file' | 'stream'
  source_ref   TEXT,                      -- original url (never the bytes)
  first_seen   TEXT NOT NULL,             -- RFC3339 UTC
  last_seen    TEXT NOT NULL
);

-- A redacted artifact is keyed by (source, profile). Same image processed with
-- a newer model or different redaction params yields a DIFFERENT artifact,
-- which is what keeps the cache honest across model upgrades.
CREATE TABLE IF NOT EXISTS artifacts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source_sha    TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
  profile_hash  TEXT NOT NULL,            -- see spec/profile-hash.md
  profile_json  TEXT NOT NULL,            -- canonical JSON of the profile
  blob_path     TEXT NOT NULL,            -- relative to <home>/blobs
  blob_sha      TEXT NOT NULL,            -- sha256 of the redacted bytes
  blob_size     INTEGER NOT NULL,
  mime          TEXT NOT NULL,
  n_faces       INTEGER NOT NULL DEFAULT 0,
  n_plates      INTEGER NOT NULL DEFAULT 0,
  min_score     REAL,                     -- lowest accepted detection score
  needs_review  INTEGER NOT NULL DEFAULT 0,
  stats_json    TEXT NOT NULL,            -- timings breakdown + model versions
  manual_regions TEXT NOT NULL DEFAULT '[]', -- operator-drawn boxes, normalized coords
  created_at    TEXT NOT NULL,
  expires_at    TEXT,                     -- NULL = kept forever
  UNIQUE (source_sha, profile_hash)
);

-- Thumbnails live in their own table, NOT in an `artifacts` column.
-- SQLite stores a row contiguously, so a 14 kB thumbnail inline means every
-- listing, sort and aggregate over `artifacts` drags 14 kB per row through the
-- page cache. At 50k rows that was 723 MB of blob being scanned to compute
-- SUM(n_faces). Out here, the same scan touches a few hundred kB.
CREATE TABLE IF NOT EXISTS thumbs (
  artifact_id  INTEGER PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
  jpeg         BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS detections (
  artifact_id  INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  cls          TEXT NOT NULL,             -- 'face' | 'plate'
  x            INTEGER NOT NULL,
  y            INTEGER NOT NULL,
  w            INTEGER NOT NULL,
  h            INTEGER NOT NULL,
  score        REAL NOT NULL,
  detector     TEXT NOT NULL
);

-- Tags and metadata are mutable and merged on re-submit; they belong to the
-- source image, not to a particular artifact.
--
-- They are also owned by a TENANT (see spec/scoped-keys.md). Identical bytes
-- are stored once, so without the tenant column two apps submitting the same
-- photo would read each other's labels off the shared row.
CREATE TABLE IF NOT EXISTS tags (
  source_sha  TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
  tenant      TEXT NOT NULL DEFAULT 'global',
  tag         TEXT NOT NULL,
  PRIMARY KEY (source_sha, tenant, tag)
);

CREATE TABLE IF NOT EXISTS metadata (
  source_sha  TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
  tenant      TEXT NOT NULL DEFAULT 'global',
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  PRIMARY KEY (source_sha, tenant, key)
);

CREATE TABLE IF NOT EXISTS api_keys (
  id          TEXT PRIMARY KEY,           -- public identifier, safe to log
  name        TEXT NOT NULL,
  prefix      TEXT NOT NULL,              -- first chars of the key, for display
  key_sha     TEXT NOT NULL UNIQUE,       -- sha256 of the full key; never the key
  scope_json  TEXT,                       -- NULL = unrestricted (the operator)
  created_at  TEXT NOT NULL,
  last_used   TEXT,
  revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_detections_artifact ON detections(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_source    ON artifacts(source_sha);
CREATE INDEX IF NOT EXISTS idx_artifacts_created   ON artifacts(created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_review    ON artifacts(needs_review);
CREATE INDEX IF NOT EXISTS idx_artifacts_expires   ON artifacts(expires_at);
CREATE INDEX IF NOT EXISTS idx_tags_tag            ON tags(tenant, tag);
CREATE INDEX IF NOT EXISTS idx_metadata_kv         ON metadata(tenant, key, value);

-- ---------------------------------------------------------------------------
-- Async submission (added in 0.2.0)
-- ---------------------------------------------------------------------------

-- A job is the unit a producer gets back immediately. It outlives the HTTP
-- request that created it, so a slow fetch or a busy queue never depends on a
-- connection staying open.
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,          -- "job_" + 16 hex
  status        TEXT NOT NULL,             -- queued | running | done | failed
  external_id   TEXT,                      -- caller's own code (often a filename)
  on_conflict   TEXT NOT NULL DEFAULT 'reuse',
  source_kind   TEXT NOT NULL,             -- url | file | stream
  source_ref    TEXT,
  source_sha    TEXT,                      -- known only once processed
  profile_hash  TEXT NOT NULL,
  profile_json  TEXT NOT NULL,
  tags_json     TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  artifact_id   INTEGER,
  cached        INTEGER NOT NULL DEFAULT 0,
  attempts      INTEGER NOT NULL DEFAULT 0,
  error_json    TEXT,
  tenant        TEXT NOT NULL DEFAULT 'global',
  force         INTEGER NOT NULL DEFAULT 0,
  owner_id      TEXT,                      -- the instance that holds this job
  created_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT,
  duration_ms   REAL
);

-- The producer's own identifier, and the fastest way for a consumer to find an
-- image. Thousands of photos means this lookup must be an index hit, never a
-- scan -- hence a real PRIMARY KEY rather than a row in `metadata`.
--
-- Many external_ids may point at one source_sha (two filenames, identical
-- bytes); an external_id points at exactly one source_sha WITHIN A TENANT.
-- The tenant belongs in the key because two apps sharing an instance will both
-- submit "IMG_0042.jpg", and one app's code resolving to the other app's photo
-- is a correctness bug, not merely a leak.
CREATE TABLE IF NOT EXISTS external_ids (
  tenant       TEXT NOT NULL DEFAULT 'global',
  external_id  TEXT NOT NULL,
  source_sha   TEXT NOT NULL REFERENCES images(source_sha) ON DELETE CASCADE,
  profile_hash TEXT,                     -- the profile this code was submitted with
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  PRIMARY KEY (tenant, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created     ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_external    ON jobs(external_id);
CREATE INDEX IF NOT EXISTS idx_jobs_sha         ON jobs(source_sha);
CREATE INDEX IF NOT EXISTS idx_external_sha     ON external_ids(source_sha);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant      ON jobs(tenant);
CREATE INDEX IF NOT EXISTS idx_jobs_owner       ON jobs(owner_id);

-- ---------------------------------------------------------------------------
-- Live instances (added in 0.8.0)
-- ---------------------------------------------------------------------------

-- Every serving process registers itself and heartbeats. Two purposes:
--   1. SQLite is a single-writer file, so a second process sharing one is
--      silent corruption rather than a degraded mode. It has to be refused.
--   2. It is the foundation for job leases once several replicas run against a
--      shared database -- crash recovery must only reclaim jobs whose owner has
--      actually stopped heartbeating.
CREATE TABLE IF NOT EXISTS instances (
  id              TEXT PRIMARY KEY,
  host            TEXT NOT NULL,
  pid             INTEGER NOT NULL,
  version         TEXT,
  storage_backend TEXT,
  started_at      TEXT NOT NULL,
  last_seen       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instances_seen ON instances(last_seen);

-- ---------------------------------------------------------------------------
-- Audit trail (added in 0.3.0)
-- ---------------------------------------------------------------------------

-- Every privileged mutation, whoever made it. The dashboard authenticates with
-- one shared password, so `actor` cannot identify a person -- it records the
-- channel (dashboard vs cli) and the source address, which is the most that
-- credential can honestly support.
CREATE TABLE IF NOT EXISTS audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          TEXT NOT NULL,
  actor       TEXT NOT NULL,     -- 'dashboard' | 'cli' | 'api'
  action      TEXT NOT NULL,     -- key.create | key.revoke | image.delete
  target      TEXT,
  source_ip   TEXT,
  detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);

-- Agent/user feedback (cli-feedback-spec): submission is open, id is the
-- idempotency key, so INSERT OR IGNORE semantics apply.
CREATE TABLE IF NOT EXISTS feedback (
  id          TEXT PRIMARY KEY,          -- client-generated idempotency key
  version     TEXT,
  kind        TEXT,
  message     TEXT NOT NULL,
  context     TEXT,
  reporter    TEXT,
  ip          TEXT,
  created_at  TEXT NOT NULL
);

-- Admin-declared public-read rules (added in 0.20.0). A rule is ONE predicate
-- -- a tag, or a metadata key=value -- optionally bound to the tenant whose
-- labels may satisfy it. Evaluated at read time on /pub/blobs/<sha>, so
-- deleting a rule revokes immediately and nothing is stamped on the image.
CREATE TABLE IF NOT EXISTS public_rules (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  tenant      TEXT,           -- NULL: labels in any tenant qualify
  tag         TEXT,           -- set XOR meta_key
  meta_key    TEXT,
  meta_value  TEXT,
  created_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Listing indexes (added in 0.5.0)
-- ---------------------------------------------------------------------------
-- Each sortable column needs its own index WITH the id tiebreaker, so the
-- listing is an index walk rather than a full scan into a temp b-tree, and so
-- keyset pagination can seek straight to a cursor.
CREATE INDEX IF NOT EXISTS idx_artifacts_created_id ON artifacts(created_at, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_faces_id   ON artifacts(n_faces, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_plates_id  ON artifacts(n_plates, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_size_id    ON artifacts(blob_size, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_review_id  ON artifacts(needs_review, id);
CREATE INDEX IF NOT EXISTS idx_jobs_created_id      ON jobs(created_at, id);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created  ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_duration        ON jobs(duration_ms, id);
