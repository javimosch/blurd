#!/usr/bin/env python3
"""Rotation acceptance test against a LIVE daemon (black-box, like conformance).

Pushes unique noise images well past `storage.max_bytes` and asserts:
  * no 507 -- rotation makes room instead of refusing
  * live usage stays at or under the cap (checked via /v1/stats)
  * the oldest images answer 410 with reason "evicted", the newest 200
  * an image with OPEN feedback outlives older-and-newer unprotected ones
  * `blurd storage scan` agrees with the accounting (drift ~0)

The daemon must run with BLURD_STORAGE_EVICT=oldest and a small
BLURD_STORAGE_MAX_BYTES (e.g. 3000000). Images are ~150-400 kB of noise, so
detection finds nothing and the output size is close to the input.

    python3 tests/rotation_local.py --url http://127.0.0.1:8799 --api-key K \
        [--bin ./blurd] [--count 60]
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

import cv2
import numpy as np


def req(base, key, method, path, body=None, ctype=None):
    r = urllib.request.Request(base + path, data=body, method=method)
    r.add_header("Authorization", f"Bearer {key}")
    if ctype:
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def noise_jpeg(seed, side=640):
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (side, side, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return buf.tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--bin")
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--seed", type=int, default=1000)
    a = ap.parse_args()
    base, key = a.url.rstrip("/"), a.api_key
    fails = []

    def check(cond, msg):
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    st, body = req(base, key, "GET", "/v1/stats")
    stats = json.loads(body)["data"]
    cap = stats["storage_max_bytes"]
    check(stats.get("storage_rotation", {}).get("enabled"),
          f"rotation enabled (cap {cap})")

    shas, statuses, peak = [], [], 0
    protected = None
    for i in range(a.count):
        st, body = req(base, key, "POST", "/v1/images?wait=60",
                       noise_jpeg(a.seed + i), "image/jpeg")
        statuses.append(st)
        d = json.loads(body).get("data") or {}
        sha = (d.get("result") or {}).get("source_sha") or d.get("source_sha")
        shas.append(sha)
        if i == 2 and sha:
            # Feedback on an early image: it must be evicted last.
            req(base, key, "POST", f"/v1/images/{sha}/feedback",
                json.dumps({"kind": "missed", "comment": "rotation test"}).encode(),
                "application/json")
            protected = sha
        s = json.loads(req(base, key, "GET", "/v1/stats")[1])["data"]
        peak = max(peak, s["storage_used_bytes"])

    check(all(s in (200, 202) for s in statuses),
          f"no refusals ({sorted(set(statuses))})")
    check(peak <= cap, f"peak usage {peak} <= cap {cap}")

    first = req(base, key, "GET", f"/v1/blobs/{shas[0]}")
    err = json.loads(first[1]).get("error", {}) if first[0] != 200 else {}
    check(first[0] == 410 and err.get("details", {}).get("reason") == "evicted",
          f"oldest -> 410 evicted ({first[0]} {err.get('message')})")
    last = req(base, key, "GET", f"/v1/blobs/{shas[-1]}")
    check(last[0] == 200, f"newest -> 200 ({last[0]})")
    if protected:
        p = req(base, key, "GET", f"/v1/blobs/{protected}")
        check(p[0] == 200, f"image with open feedback survives ({p[0]})")

    if a.bin:
        out = subprocess.run([a.bin, "storage", "scan"], capture_output=True,
                             text=True)
        try:
            scan = json.loads(out.stdout)["data"]
            # Objects written by this process only; small drift is fine
            # (a probe file, a thumbnail-free legacy blob).
            check(abs(scan["drift_bytes"]) < 64 * 1024,
                  f"scan drift {scan['drift_bytes']} bytes "
                  f"(store {scan['store_bytes']}, accounted {scan['accounted_bytes']})")
        except Exception as e:
            check(False, f"storage scan parsed ({e}: {out.stdout[:200]} {out.stderr[:200]})")

    print(f"\n{len(fails)} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
