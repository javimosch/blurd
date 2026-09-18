---
name: blurd-testing
description: How to actually run blurd's test suites, and the harness traps that make a test pass while proving nothing. Read before writing or invoking conformance.py, backend_parity.py, queue_bytes.py, multi_replica.py, seam_check.py or bench/. Includes the exact invocations, which arguments are load-bearing, and the failures that are the harness rather than the code.
---

# Testing blurd

The suites are black-box: they take **a binary and a URL**, never Python
imports. That is deliberate — the same files are the acceptance test for the
planned Go/machin ports. It also means every one of them needs a live daemon
and real credentials, and getting those wrong produces failures that look like
product bugs.

## The invocations that actually work

Conformance needs more arguments than it looks like it needs. Run it with fewer
and it **silently skips whole sections** rather than failing:

```bash
H=/tmp/blurd-conf; rm -rf $H; mkdir -p $H/models
cp /path/to/models/* $H/models/
export BLURD_HOME=$H

./blurd dashboard-password 'a-dev-password'          # or 12 dashboard checks are SKIPPED
./blurd serve --daemon --port 8990
until curl -s -o /dev/null http://127.0.0.1:8990/v1/health; do sleep 1; done

j() { python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["key"])'; }
K=$(./blurd keys add c | j)
A=$(./blurd keys add a --scope-tag conformance-a | j)   # the tag name matters
B=$(./blurd keys add b --scope-tag conformance-b | j)

python3 tests/conformance.py --bin ./blurd --url http://127.0.0.1:8990 \
  --api-key "$K" --image small.jpg --image-b large.jpg \
  --scoped-key-a "$A" --scoped-key-b "$B" \
  --dashboard-user admin --dashboard-password 'a-dev-password'
# -> 113 passed, 0 failed
```

Two things there are load-bearing and neither is obvious:

- **The scoped keys must carry `conformance-a` / `conformance-b` as their scope
  tags.** The suite asserts a submission is stamped with its own scope by
  looking for that exact tag. Any other tag gives you two mystery failures
  ("a scoped key's submission is stamped with its scope", "stats are scoped")
  that look like tenancy bugs and are not.
- **`dashboard-password` must be set before `serve` starts.** A daemon reads
  its config once. Without it the dashboard is disabled and twelve checks are
  reported as failures.

## Use `./blurd-venv`, not `./blurd`, for Postgres or Mongo

`psycopg` and `pymongo` are optional dependencies and live in the venv. `./blurd`
probes for an interpreter with `sqlite3`/`cv2`/`numpy`/`onnxruntime` and may
well pick one without them. Anything touching a non-default backend uses
`./blurd-venv`.

## The suites, and what each one is for

| suite | needs | catches |
|---|---|---|
| `tests/seam_check.py` | nothing, 1 second | SQL outside `db_sql.py`; backend parity drift |
| `tests/conformance.py` | a daemon + 3 keys | the API contract, 113 checks |
| `tests/backend_parity.py` | **two** daemons | two backends disagreeing |
| `tests/queue_bytes.py` | a daemon with a small budget | backpressure, and a leaking byte counter |
| `tests/multi_replica.py` | a **live** daemon on a shared backend | job ownership, reaping |
| `tests/schema_drift.py` | nothing | the SQLite and Postgres schemas diverging |
| `bench/throughput.py`, `bench/memory.sh` | a daemon | capacity, resident memory |

`seam_check.py` costs a second and catches a class of mistake that is otherwise
found in production. Run it on every change that touches `src/`.

## Harness traps — learned the hard way

**A failure that is IDENTICAL on both backends is almost always the harness.**
That single heuristic resolved every false alarm so far. If SQLite and Mongo
both say a filter returns 0 rows, the filter is fine and the test is wrong.

Specific traps, all of which produced a green-looking test that proved nothing:

1. **Submission takes `metadata` as a JSON object; only *filters* use the
   `meta.<key>` query spelling.** Seeding with `meta.batch=x` silently submits
   no metadata at all, and then every metadata assertion "passes" against
   nothing.

2. **Never compare ids across two instances.** Artifact ids and job ids are
   minted per instance. Compare content-derived keys: `source_sha`, or the
   caller's own `external_id`.

3. **Benchmark with unique bytes.** Re-submitting identical bytes measures the
   dedup cache — 220 img/s and 1 ms — not the pipeline. Append a unique JPEG
   comment segment:
   ```python
   m = f"run-{i}".encode()
   seg = b"\xff\xfe" + (len(m) + 2).to_bytes(2, "big") + m
   payload = raw[:2] + seg + raw[2:]
   ```

4. **Tenant tests are not idempotent across runs on the same home.** The same
   scope produces the same tenant id, so re-running against a dirty home can
   pass for the wrong reason. Use a fresh `BLURD_HOME` when in doubt.

5. **`multi_replica.py` needs a daemon already running and heartbeating** on
   the same shared backend. With no live instance it fails at the first check
   ("at least one live replica is registered") — which is the test being
   under-supplied, not a reaper bug.

6. **Give each daemon its own `BLURD_HOME`.** Two daemons sharing a home
   overwrite each other's pidfile, and `blurd stop` then kills the wrong
   process and leaves a port bound. Symptom: "Port N is already in use" from a
   daemon you thought you had stopped.

## Why `backend_parity.py` exists

Conformance proves one instance is correct. It cannot prove two backends are
correct *identically*, and the failure it would miss is specific:

> A keyset cursor that is not **totally ordered** produces a boundary row that
> matches neither `<` nor `=`. It appears on both pages, or on neither. Every
> individual page still looks perfectly well-formed, so no single-page
> assertion catches it.

So `backend_parity.py` pages through every listing under every sort and both
directions on two live backends, and asserts the union of pages is exactly the
full set — nothing twice, nothing missed — and that both backends serve the
same set. Run it whenever you touch pagination, sorting, or either backend's
query builder.

## Shell hazards in this repo's test scripts

- **zsh does not word-split unquoted parameters.** `curl $AUTH` where
  `AUTH="-H 'Authorization: Bearer k'"` sends no header in zsh and every
  request 401s. This has bitten twice. Wrap test scripts in `bash -c`.
- **`pkill -f blurd` matches your own shell** running a command containing that
  string, and kills the session (exit 144). Take the PID from
  `ss -ltnp | grep :PORT` instead.
