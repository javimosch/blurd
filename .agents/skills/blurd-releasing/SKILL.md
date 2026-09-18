---
name: blurd-releasing
description: Cutting a blurd release — what to verify, in what order, and why each step exists. Read before bumping a version, before claiming a change is done, or when writing a commit message for work that involved a measurement. Also covers the shell hazards that have cost real time in this repo.
---

# Releasing a change

## The order matters

Each step exists because skipping it has produced a wrong claim at least once.

1. **`python3 tests/seam_check.py`** — one second. Catches SQL outside
   `db_sql.py` and a backend that has drifted out of parity, which are the two
   mistakes that fail in production rather than in review.

2. **Conformance on every backend the change could touch.** A change to `db.py`,
   `dialect.py` or any query means all three (SQLite, Postgres, MongoDB). See
   the `blurd-testing` skill for the invocation — several of its arguments are
   load-bearing and silently skip whole sections when omitted.

3. **`tests/backend_parity.py`** if pagination, sorting or a query builder
   moved. Conformance proves one instance correct; this proves two backends
   correct *identically*.

4. **Re-measure if the change touches what the pipeline holds, or how fast it
   runs** (`bench/memory.sh`, `bench/throughput.py`), and **update the fitted
   constants** in `src/resources.py` plus the tables in `spec/resources.md` and
   `spec/capacity.md`. Those constants are fitted to measurement; leaving them
   stale is how a deployment gets OOM-killed by a default that used to be right.

5. **`bash tests/helm_guardrails.sh`** if the chart moved. A guardrail that
   stops firing is a promise the chart has quietly stopped keeping.

6. **Bump `src/__init__.py`**, the `image:` tag in `docker-compose.yml`, and
   `appVersion` in `deploy/helm/blurd/Chart.yaml` — plus the chart's own
   `version` when the chart itself changed.

7. **Update the docs that carry the fact you changed** — and only those.

## Documentation has one home per fact

`docs/deployment.md` is the operator-facing page; `spec/` is design rationale
for someone porting blurd; the chart README is the chart reference; `AGENTS.md`
and these skills are for agents. **A fact belongs in exactly one of them**, with
the others linking to it.

This is not tidiness. Deployment facts were once spread across five files, and
the README ended up claiming a compose profile was unused long after it was in
use, carrying a superseded memory model, and quoting a test count that had
moved — each had been corrected in the *other* copy. When you find yourself
editing the same sentence twice, that is the signal to delete one of them.

## Commit messages carry the why

Including **what was tried and did not work**. The in-place redaction that
produced no measurable saving is written into the 0.13.0 message precisely so
the next person does not spend a day rediscovering it.

State measured numbers rather than adjectives, name the decisive test, and say
plainly when a hypothesis was wrong. "Reduced memory" is worth far less than
"961 → 672 MB at 7 workers; a 512 MB container went from OOM-killed at 3/30 to
30/30".

## Claim only what was verified

If a path was not exercised, say so in the same breath as the result. The Helm
chart renders, lints and schema-validates, and its pod shape passes conformance
under Docker — **and it has never been applied to a live cluster**. Both halves
get stated, every time, including in the chart's own README.

## Shell hazards in this repo

- **zsh does not word-split unquoted parameters.** `curl $AUTH` with
  `AUTH="-H 'Authorization: Bearer k'"` sends no header and every request 401s —
  a "broken auth" bug that is really a shell bug. It has happened more than
  once, including inside a test loop where `$ARGS` expanded to nothing and every
  case silently reported success. Wrap multi-step scripts in `bash -c '...'`.
- **`curl` exits 0 on an HTTP error.** A readiness loop written as
  `curl -s -o /dev/null … && break` breaks on the first 502. Compare the status
  code from `-w '%{http_code}'`.
- **`pkill -f <pattern>` matches your own shell** when the pattern appears in
  the running command, killing the session. Take the PID from
  `ss -ltnp | grep :PORT`.
- **Heredocs choke on literal control characters.** Write such files with an
  editor tool instead.
- **Give each test daemon its own `BLURD_HOME`.** Two daemons sharing a home
  overwrite each other's pidfile, so `stop` kills the wrong process and leaves a
  port bound — which then presents as "port already in use" from a daemon you
  thought you had stopped.
