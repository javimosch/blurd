# Jobs and unique codes

## Why submission is asynchronous

A producer holding thousands of originals cannot keep an HTTP request open for
every one of them: a slow origin fetch, a busy queue or a large image would all
turn into client-side timeouts. `POST /v1/images` therefore always creates a
job and returns `202` with `Location: /v1/jobs/<id>`.

`?wait=N` (max 120s) long-polls: if the job settles inside that window the
response is `200` with `result` populated. This is a convenience for small
callers and for the CLI, not a different code path — the work still happens on
a worker thread.

### Job states

    queued ──▶ running ──▶ done
                      └──▶ failed

`failed` carries the same error object the CLI and API use everywhere
(`code`, `type`, `message`, `details`, `recoverable`, `retry_after`,
`suggestions`), so a producer can decide whether to retry without parsing prose.

### Restart behaviour

| Job kind | On daemon restart | Why |
|---|---|---|
| `url` | requeued | the daemon can re-fetch the source |
| `stream` (upload) | failed, `recoverable: true` | the bytes were only ever in memory |

blurd does not spool uploaded source images to disk. A spool directory would be
the easy way to make upload jobs durable, and it would break the one guarantee
the service exists to provide.

Validation that is cheap (URL scheme, DNS, IP range) runs **synchronously at
submit**, so an unfetchable URL is rejected with `422` rather than accepted and
failed asynchronously. The per-redirect-hop IP re-validation still happens in
the worker.

## Unique codes (`external_id`)

The producer's own identifier for an image, typically the filename or an object
key. Stored in `external_ids` with the code as PRIMARY KEY.

- one code → exactly one `source_sha`
- many codes → the same `source_sha` (identical bytes are stored once)

### Conflict policy

`on_conflict` applies when a code is submitted that already maps to *different*
bytes:

| Value | Behaviour |
|---|---|
| `reuse` (default) | a known code short-circuits at submit: the job comes back `done`, `cached: true`, with no fetch and no decode |
| `replace` | process the new bytes and repoint the code |
| `reject` | fail the job with `94 resource_conflict` |

Repointing is never implicit, because a consumer may have cached
`/v1/blobs/by-code/<code>`; changing what that returns is a visible behaviour
change and should be requested.

### Why not just use metadata

Measured on 50 000 stored images:

| Lookup | Time |
|---|---:|
| `external_ids` PRIMARY KEY → artifact join | 13 µs |
| `artifacts` by full sha256 | 10 µs |
| metadata `key=value` filter, LIMIT 50, ordered | 7 ms |
| sha256 prefix (`LIKE`) | 6 ms |

Metadata filtering matches many rows and sorts them; it is a browsing path. The
user-facing read path must be an index hit, which is why the code gets its own
table and its own endpoints rather than living in `metadata`.
