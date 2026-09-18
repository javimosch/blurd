#!/usr/bin/env python3
"""Throughput benchmark for a running blurd daemon.

Answers the only questions that matter for capacity planning:
  * how long does one image take, by resolution
  * how many images per second does one instance sustain, by worker count
  * how many reads per second does the consumer path serve
  * where does it stop getting faster

Usage:
    python3 bench/throughput.py --url http://127.0.0.1:8775 --api-key blk_...
"""

import argparse
import base64
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def call(url, method="GET", key=None, body=None, ctype=None, timeout=300):
    req = urllib.request.Request(url, method=method, data=body)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def unique(raw: bytes, seed: int) -> bytes:
    """Make each submission genuinely distinct.

    Re-submitting identical bytes measures the dedup cache, not the pipeline:
    the first run costs ~200 ms and every repeat returns in ~1 ms. A real
    ingest stream is all-new bytes, so the benchmark appends a unique JPEG
    comment segment -- the pixels and therefore the detection work are
    unchanged, but the sha256 differs every time.
    """
    marker = f"blurd-bench-{seed}-{time.time_ns()}".encode()
    seg = b"\xff\xfe" + (len(marker) + 2).to_bytes(2, "big") + marker
    return raw[:2] + seg + raw[2:]


def submit(url, key, raw, code, wait=0):
    q = f"?code={urllib.parse.quote(code, safe='')}&force=1"
    if wait:
        q += f"&wait={wait}"
    s, b = call(url + "/v1/images" + q, "POST", key, raw, "image/jpeg")
    return json.loads(b)["data"]


def drain(url, key, job_ids, poll=0.2):
    """Wait until every job settles. Returns per-job durations."""
    done = {}
    while len(done) < len(job_ids):
        for jid in job_ids:
            if jid in done:
                continue
            s, b = call(f"{url}/v1/jobs/{jid}", key=key)
            j = json.loads(b)["data"]
            if j["status"] in ("done", "failed"):
                done[jid] = j
        if len(done) < len(job_ids):
            time.sleep(poll)
    return done


def bench_latency(url, key, images, reps=5):
    print("\n-- per-image latency (one at a time, warm models)")
    print(f"  {'image':<12}{'pixels':>12}{'KB':>8}{'total':>9}{'detect':>9}"
          f"{'decode':>9}{'redact':>9}{'encode':>9}")
    out = {}
    for label, path in images:
        raw = open(path, "rb").read()
        times, parts = [], []
        for i in range(reps):
            j = submit(url, key, unique(raw, i),
                       f"bench/lat-{label}-{i}-{time.time()}", wait=300)
            if j["status"] != "done":
                print(f"  {label}: FAILED {j.get('error', {}).get('message')}")
                break
            t = j["result"]["stats"]["timings"]
            times.append(j["duration_ms"])
            parts.append(t)
        if not times:
            continue
        med = statistics.median(times)
        det = statistics.median(p.get("detect_face_ms", 0) + p.get("detect_plate_ms", 0)
                                for p in parts)
        dec = statistics.median(p.get("decode_ms", 0) for p in parts)
        red = statistics.median(p.get("redact_ms", 0) for p in parts)
        enc = statistics.median(p.get("encode_ms", 0) for p in parts)
        import cv2
        h, w = cv2.imread(path).shape[:2]
        print(f"  {label:<12}{w*h:>12,}{len(raw)//1024:>8}{med:>8.0f}ms{det:>8.0f}ms"
              f"{dec:>8.0f}ms{red:>8.0f}ms{enc:>8.0f}ms")
        out[label] = med
    return out


def bench_throughput(url, key, path, n, concurrency):
    """Submit n images with `concurrency` producers, then wait for the queue to
    drain. Measures the daemon's sustained rate, not the client's."""
    raw = open(path, "rb").read()
    stamp = time.time()
    job_ids = []
    lock = threading.Lock()

    def one(i):
        j = submit(url, key, unique(raw, i), f"bench/tp-{stamp}-{i}")
        with lock:
            job_ids.append(j["job_id"])

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(one, range(n)))
    submitted = time.perf_counter() - t0
    done = drain(url, key, job_ids)
    elapsed = time.perf_counter() - t0
    ok = [j for j in done.values() if j["status"] == "done"]
    durations = [j["duration_ms"] for j in ok]
    return {
        "n": n, "ok": len(ok), "failed": len(done) - len(ok),
        "submit_s": submitted, "wall_s": elapsed,
        "rate": len(ok) / elapsed if elapsed else 0,
        "p50_ms": statistics.median(durations) if durations else 0,
        "p95_ms": (statistics.quantiles(durations, n=20)[18]
                   if len(durations) > 20 else max(durations, default=0)),
    }


def bench_reads(url, key, code, n, concurrency):
    """Consumer hot path: fetch the redacted bytes by unique code."""
    path = f"{url}/v1/blobs/by-code/{urllib.parse.quote(code, safe='')}"
    lat = []
    lock = threading.Lock()

    def one(_):
        t = time.perf_counter()
        call(path, key=key)
        with lock:
            lat.append((time.perf_counter() - t) * 1000)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(one, range(n)))
    wall = time.perf_counter() - t0
    return {"rate": n / wall, "p50_ms": statistics.median(lat),
            "p95_ms": statistics.quantiles(lat, n=20)[18] if len(lat) > 20 else max(lat)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8775")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--images-dir", default="/tmp/blurd-test")
    ap.add_argument("--n", type=int, default=60, help="images per throughput run")
    ap.add_argument("--skip-latency", action="store_true")
    a = ap.parse_args()

    imgs = [(n, f"{a.images_dir}/bench-{n}.jpg") for n in ("s", "m", "l", "xl")]
    imgs = [(n, p) for n, p in imgs if os.path.exists(p)]
    if not imgs:
        print("no bench images found", file=sys.stderr)
        return 1

    s, b = call(f"{a.url}/v1/stats", key=a.api_key)
    workers = json.loads(b)["data"]["queue"]["workers"]
    print(f"blurd at {a.url} | {workers} workers | {os.cpu_count()} host cores")

    if not a.skip_latency:
        bench_latency(a.url, a.api_key, imgs)

    print(f"\n-- sustained throughput ({a.n} images each, medium 1280px)")
    print(f"  {'producers':>10}{'rate/s':>10}{'img/min':>10}{'p50':>9}{'p95':>9}{'wall':>8}")
    med = dict(imgs)["m"]
    for c in (1, 2, 4, 8, 16):
        r = bench_throughput(a.url, a.api_key, med, a.n, c)
        print(f"  {c:>10}{r['rate']:>10.1f}{r['rate']*60:>10.0f}"
              f"{r['p50_ms']:>8.0f}ms{r['p95_ms']:>8.0f}ms{r['wall_s']:>7.1f}s"
              + (f"   ({r['failed']} failed)" if r["failed"] else ""))

    print(f"\n-- sustained throughput by image size (8 producers)")
    print(f"  {'image':<8}{'pixels':>12}{'img/s':>9}{'img/min':>10}{'img/hour':>11}"
          f"{'p50':>9}{'core-s/img':>12}")
    import cv2
    for label, path in imgs:
        r = bench_throughput(a.url, a.api_key, path, max(20, a.n // 2), 8)
        h, w = cv2.imread(path).shape[:2]
        cores = os.cpu_count() or 1
        print(f"  {label:<8}{w*h:>12,}{r['rate']:>9.1f}{r['rate']*60:>10.0f}"
              f"{r['rate']*3600:>11,.0f}{r['p50_ms']:>8.0f}ms"
              f"{cores / r['rate'] if r['rate'] else 0:>11.2f}s"
              + (f"  ({r['failed']} failed)" if r["failed"] else ""))

    print("\n-- consumer read path (blob by unique code)")
    j = submit(a.url, a.api_key, open(med, "rb").read(), "bench/read-probe", wait=300)
    print(f"  {'clients':>10}{'reads/s':>10}{'p50':>9}{'p95':>9}")
    for c in (1, 4, 16, 32):
        r = bench_reads(a.url, a.api_key, "bench/read-probe", 300, c)
        print(f"  {c:>10}{r['rate']:>10.0f}{r['p50_ms']:>8.1f}ms{r['p95_ms']:>8.1f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
