#!/usr/bin/env bash
# coverage.sh — measure what the black-box suites actually cover.
#
# The suites are HTTP clients: the DAEMON is the code under test, so the
# daemon is what must run under coverage. `serve --daemon` forks and coverage
# follows the launcher, which is why this uses --foreground.
#
#   bash tests/coverage.sh            # daemon + sqlite conformance, then report
#   bash tests/coverage.sh --report   # report only, against existing data
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=${PORT:-8991}
H=${COV_HOME:-/tmp/blurd-cov}
DATA=/tmp/cov-blurd
RCFILE=/tmp/covconf.ini
printf '[run]\nsource = src\nsigterm = true\n' > "$RCFILE"

if [ "${1:-}" != "--report" ]; then
  rm -rf "$H" "$DATA"; mkdir -p "$H/models"; cp ~/.blurd/models/* "$H/models/"
  export BLURD_HOME=$H
  ./blurd dashboard-password 'cov-pass' >/dev/null
  .venv/bin/python -m coverage run --rcfile="$RCFILE" --data-file="$DATA" \
      run.py serve --port $PORT --foreground &
  COV_PID=$!
  trap 'kill -TERM $COV_PID 2>/dev/null; wait $COV_PID 2>/dev/null || true' EXIT
  for i in $(seq 40); do curl -s -o /dev/null http://127.0.0.1:$PORT/v1/health && break || sleep 1; done
  j() { python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["key"])'; }
  K=$(./blurd keys add cov | j)
  A=$(./blurd keys add cov-a --scope-tag conformance-a | j)
  B=$(./blurd keys add cov-b --scope-tag conformance-b | j)
  python3 tests/conformance.py --bin ./blurd --url http://127.0.0.1:$PORT \
    --api-key "$K" --image "${1:-/tmp/conf-a.jpg}" --image-b "${2:-/tmp/conf-b.jpg}" \
    --scoped-key-a "$A" --scoped-key-b "$B" \
    --dashboard-user admin --dashboard-password 'cov-pass' | tail -3
  kill -TERM $COV_PID; wait $COV_PID || true
fi
.venv/bin/python -m coverage report --data-file="$DATA"
