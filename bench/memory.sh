#!/usr/bin/env bash
# Re-measure the memory model behind src/resources.py.
#
# The sizing constants (BASE_MB, PER_WORKER_MB) are fitted to real numbers, not
# guessed, so they have to be re-fitted whenever the pipeline changes what it
# holds in memory. This prints the points and the fit.
#
#   bash bench/memory.sh [image]
#
# Defaults to the largest bench image, because sizing must assume the worst
# case: per-worker cost rises with resolution (123 MB at 1280 px, 170 MB at
# 4000 px when measured without trimming).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
IMG="${1:-/tmp/blurd-test/bench-xl.jpg}"
MODELS="${BLURD_BENCH_MODELS:-$HOME/.blurd/models}"
[ -f "$IMG" ] || { echo "no such image: $IMG" >&2; exit 1; }

point() {
  local w="$1" port="$2" home=/tmp/blurd-bench-mem-$w-$$
  rm -rf "$home"; mkdir -p "$home/models"; cp "$MODELS"/* "$home/models/" 2>/dev/null
  env -u BLURD_HOME BLURD_HOME="$home" BLURD_WORKERS="$w" BLURD_HTTP_THREADS=8 \
    ./blurd serve --daemon --port "$port" >/dev/null 2>&1
  for i in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:$port/v1/health" && break; sleep 1; done
  local pid; pid=$(cat "$home/blurd.pid")
  local key; key=$(env -u BLURD_HOME BLURD_HOME="$home" ./blurd keys add bench 2>/dev/null \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['key'])")
  curl -s -o /dev/null -X POST -H "Authorization: Bearer $key" -H "Content-Type: image/jpeg" \
    --data-binary @"$IMG" "http://127.0.0.1:$port/v1/images?wait=300"
  python3 - "$key" "$port" "$IMG" >/dev/null 2>&1 <<PY
import urllib.request, concurrent.futures as f, sys
key, port, img = sys.argv[1:4]
raw = open(img, "rb").read()
def one(i):
    m = f"bench-{i}".encode(); seg = b"\xff\xfe" + (len(m)+2).to_bytes(2,"big") + m
    r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/images?wait=300",
                               data=raw[:2]+seg+raw[2:], method="POST")
    r.add_header("Authorization", "Bearer "+key); r.add_header("Content-Type", "image/jpeg")
    urllib.request.urlopen(r).read()
with f.ThreadPoolExecutor(8) as ex: list(ex.map(one, range(24)))
PY
  echo "$w $(( $(ps -o rss= -p "$pid" | tr -d ' ') / 1024 ))"
  env -u BLURD_HOME BLURD_HOME="$home" ./blurd stop >/dev/null 2>&1
  rm -rf "$home"
}

echo "image: $IMG   (24 submissions, 8 concurrent, per worker count)"
PTS=""
port=8860
for w in 1 4 7; do
  read -r ww mb <<< "$(point "$w" "$port")"
  printf "  %s worker(s)  %5s MB\n" "$ww" "$mb"
  PTS="$PTS $ww:$mb"
  port=$((port+1))
done
python3 - $PTS <<'PY'
import sys
pts = [tuple(map(int, a.split(":"))) for a in sys.argv[1:]]
n = len(pts); sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
sxx = sum(p[0]**2 for p in pts); sxy = sum(p[0]*p[1] for p in pts)
slope = (n*sxy - sx*sy) / (n*sxx - sx*sx)
intercept = (sy - slope*sx) / n
print(f"\n  fit: peak ~= {intercept:.0f} MB + {slope:.0f} MB x workers")
print(f"  -> set BASE_MB and PER_WORKER_MB in src/resources.py from this,")
print(f"     rounding up (guessing low costs an OOM kill).")
PY
