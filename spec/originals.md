# Original retention (opt-in)

blurd's founding rule is that the source image is never persisted. This is the
one sanctioned exception, added for a producer (DND) that needs the original
for a bounded period after redaction.

## Two gates, both required

| gate | where | default |
|---|---|---|
| `originals.enabled` | instance: env `BLURD_ORIGINALS_ENABLED`, dashboard settings, or `blurd originals settings` | `false` |
| `keep_original=SECONDS` | request: query param or JSON body field; CLI `--keep-original` | absent |

A request that asks while the instance has it disabled gets **422**
`validation_error` naming the setting, at submit time (rule 7) — never a
silent ignore.

## Settings

| key | env | default | bounds |
|---|---|---|---|
| `enabled` | `BLURD_ORIGINALS_ENABLED` | `false` | |
| `max_ttl` | `BLURD_ORIGINALS_MAX_TTL` | 2592000 (30 d) | 60 .. 31536000 (hard cap 365 d) |
| `url_mode` | `BLURD_ORIGINALS_URL_MODE` | `authenticated` | `authenticated` \| `signed` |
| `sign_ttl` | `BLURD_ORIGINALS_SIGN_TTL` | 900 | 60 .. hard cap |
| signing secret | `BLURD_ORIGINALS_SIGN_SECRET` | auto-generated, stored in `settings` | never shown |

Resolution: **env > `settings` table > config.json/defaults.** The table exists
so a dashboard change reaches every replica on its next request without a
restart (a running daemon reads config.json once, rule 23). A key set by env
is reported in `locked` and refuses runtime changes with 409. When
`dashboard_key_secret` is configured, the dashboard needs it
(`X-Blurd-Admin-Secret`) to change these; every change is audited.

## Not part of the profile

`storage.ttl` is in the profile because it changes the output's lifetime.
Keeping the source does not change the redacted output, so `keep_original` is
outside `profile_hash`: retained and non-retained submissions share one
artifact.

## Storage

- Object: `originals/<sha[0:2]>/<sha[2:4]>/<sha>` via `store.py` (local or S3).
  An S3 lifecycle rule on `originals/` is a sensible backstop.
- Row: `originals(source_sha PK, blob_path, mime, bytes, created_at, expires_at)`.
  One row per sha — access follows the redacted image, so no tenant column.
  Dedup means only callers that submitted identical bytes share a sha, and
  they already hold the original.
- Written right after decode (only a real image is kept; the bytes are then
  dropped instead of held through detection), object before row. Also written
  on a redacted-cache hit, and the code-hit fast path is skipped when a
  retention is requested but none is live, since it never sees the bytes.
- A second request only **extends** `expires_at` (`CASE`/`$max`), never
  shortens it.
- Not a durability mechanism: queued uploads are still lost on restart
  (rule 6). The original is written by the worker, not at submit.

## Expiry

- The reaper sweep (`JobQueue.prune_originals`) deletes expired objects and
  blanks `blob_path`, keeping the row as a tombstone. It runs even when
  retention is disabled, so turning the feature off never strands originals.
- Reads prune lazily too, so a slow sweep never serves past the deadline.
- Past the deadline `GET /v1/originals/<sha>` answers **410**
  `resource_expired`; the record's `original` field becomes `null`.
- Deleting the image (unscoped, or the last tenant's scoped delete) deletes
  the object and the row.

## Reading

`original: {url, expires_at, bytes, mime}` appears on image records and job
results while live; `null` otherwise.

- `authenticated`: `url` = `/v1/originals/<sha>`, any key whose scope can read
  the redacted image. Out of scope → 404 (checked before expiry, so a 410
  never confirms an out-of-scope sha).
- `signed`: `url` adds `exp` (unix, `min(now + sign_ttl, expires_at)`) and
  `sig = HMAC-SHA256(secret, "<sha>.<exp>")`; needs no key. Bad or expired
  signature → 401. Also adds `url_expires_at`.
- Served with `Cache-Control: private, no-store`.
- Never under `/pub`, never rendered by the dashboard image views (an original
  undoes manual redaction regions).
