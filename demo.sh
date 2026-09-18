#!/usr/bin/env bash
# Bring up the whole thing in one command: blurd daemon + Go sidecar, with a
# fresh home under /tmp so it never touches an existing install.
#
#   ./demo.sh          start (prints every URL and credential)
#   ./demo.sh stop     stop both
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export BLURD_HOME="${BLURD_HOME:-/tmp/blurd-demo}"
BLURD_PORT="${BLURD_PORT:-8771}"
SIDECAR_PORT="${SIDECAR_PORT:-8790}"
DASH_USER="${DASH_USER:-admin}"
# Throwaway credentials for a localhost demo against a /tmp home. Override with
# DASH_PASS=... for anything that is not this.
DASH_PASS="${DASH_PASS:-demo-pass-2026}"

if [ "${1:-start}" = "stop" ]; then
  "$HERE/blurd" stop >/dev/null 2>&1 || true
  pkill -f "blurd-sidecar -port $SIDECAR_PORT" 2>/dev/null || true
  echo "stopped"
  exit 0
fi

mkdir -p "$BLURD_HOME"
"$HERE/blurd" models pull --all >/dev/null 2>&1 || {
  echo "could not download models; check connectivity" >&2; exit 1; }
"$HERE/blurd" dashboard-password "$DASH_PASS" >/dev/null
"$HERE/blurd" stop >/dev/null 2>&1 || true

KEY_FILE="$BLURD_HOME/sidecar.key"
if [ ! -s "$KEY_FILE" ]; then
  "$HERE/blurd" keys add sidecar \
    | "${BLURD_PYTHON:-python3.11}" -c 'import json,sys;print(json.load(sys.stdin)["data"]["key"])' \
    > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi
KEY="$(cat "$KEY_FILE")"

"$HERE/blurd" serve --daemon --port "$BLURD_PORT" >/dev/null

if [ ! -x "$HERE/sidecar/blurd-sidecar" ]; then
  (cd "$HERE/sidecar" && go build -o blurd-sidecar .)
fi
BLURD_URL="http://127.0.0.1:$BLURD_PORT" BLURD_API_KEY="$KEY" \
  BLURD_DASHBOARD_PASSWORD="$DASH_PASS" \
  nohup "$HERE/sidecar/blurd-sidecar" -port "$SIDECAR_PORT" \
  -dashboard-user "$DASH_USER" > "$BLURD_HOME/sidecar.log" 2>&1 &
sleep 1

cat <<EOF

  ==================================================================
   Open this first:   http://127.0.0.1:$SIDECAR_PORT
  ==================================================================

   1. Producer panel   drop an image, get a job, watch it finish
   2. Consumer panel   fetch it back by unique code / sha / metadata
   3. Admin            link to the blurd dashboard, bottom of the page

   blurd dashboard     http://127.0.0.1:$BLURD_PORT/
     user              $DASH_USER
     password          $DASH_PASS

   blurd API           http://127.0.0.1:$BLURD_PORT
   API key             $KEY
     (also at $KEY_FILE; the sidecar holds it server-side)
   home                $BLURD_HOME

   stop everything     ./demo.sh stop

EOF
