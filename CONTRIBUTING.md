# Contributing to blurd

## The short version

```bash
python3 tests/seam_check.py     # 1 second; run it on every change to src/
```

Then conformance against a running daemon — see
[the testing guide](#running-the-tests) below.

## What this project values

**Measurements over adjectives.** Numbers in this repo trace to something in
`bench/` or `spec/`. If a change claims to be faster or smaller, say by how
much, on what input, and how it was measured. A PR that says "optimised the
pipeline" will be asked for a number.

**Negative results are results.** `spec/resources.md` records an optimisation
that produced *no* measurable saving, and why the obvious measurement methods
were misleading. That entry exists so nobody spends a day rediscovering it.
Write those down.

**Say what was not verified.** The Helm chart renders, lints and schema-checks,
and has never been applied to a live cluster — the README says both halves.
Keep that habit.

## Architecture rules that are enforced

Two seams are guarded by tests, because "pluggable" otherwise means "pluggable
wherever someone remembered":

- **`src/db_sql.py` is the only module that executes SQL**, and `db_mongo.py`
  must implement exactly the function set `db.py` dispatches.
  `tests/seam_check.py` fails the build otherwise.
- **Nothing outside `src/store.py` may build a blob path**, or the
  `local`↔`s3` swap silently breaks for one code path.

`AGENTS.md` carries the full list of rules and the reason behind each. It is
worth reading before a first change — most of the entries exist because
something went wrong.

## Running the tests

Everything is black-box: the suites take **a binary and a URL**, never Python
imports, so the same files are the acceptance test for a port to another
language.

```bash
export BLURD_HOME=/tmp/blurd-test
./blurd dashboard-password 'a-dev-password'    # BEFORE serve; the daemon reads config once
./blurd serve --daemon --port 8770

j() { python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["key"])'; }
K=$(./blurd keys add c | j)
A=$(./blurd keys add a --scope-tag conformance-a | j)   # these tag names matter
B=$(./blurd keys add b --scope-tag conformance-b | j)

python3 tests/conformance.py --bin ./blurd --url http://127.0.0.1:8770 \
  --api-key "$K" --image small.jpg --image-b large.jpg \
  --scoped-key-a "$A" --scoped-key-b "$B" \
  --dashboard-user admin --dashboard-password 'a-dev-password'
```

Omit those arguments and whole sections are **skipped**, not failed — the run
will look greener than it is.

| suite | what it catches |
|---|---|
| `tests/seam_check.py` | SQL outside `db_sql.py`; backend parity drift |
| `tests/conformance.py` | the API contract (113 checks) |
| `tests/backend_parity.py` | two metadata backends disagreeing |
| `tests/queue_bytes.py` | backpressure, and a leaking byte counter |
| `tests/multi_replica.py` | job ownership and reaping across replicas |
| `tests/schema_drift.py` | the SQLite and Postgres schemas diverging |
| `tests/helm_guardrails.sh` | the chart still refusing what it must |

A change touching queries should run conformance on **all three** metadata
backends (`docker compose --profile pg --profile mongo up -d`).

## Adding a detector

Write a class in `src/detect.py` and an entry in `src/models.py`. Nothing else
needs to know it exists. Note that a new model changes `profile_hash`, which
**invalidates every cached artifact** — that is intended, but do it knowingly.

## Commit messages

Carry the *why*, including what was tried and abandoned. State measured numbers
and name the decisive test.

## Licence

By contributing you agree your contributions are licensed under the
[AGPL-3.0](LICENSE), the same terms as the project.
