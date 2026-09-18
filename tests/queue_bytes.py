#!/usr/bin/env python3
"""Is the queue bounded by the thing that actually runs out?

The queue used to be bounded by job count alone. That bound promises nothing
about memory: queued uploads are held in RAM by design -- blurd never spools
source bytes, because it promises not to store originals -- so 1000 queued jobs
at ~1.5 MB is ~1.5 GB on a box the sizing model believes needs 750 MB. The
first symptom is an OOM kill, which looks like a crash rather than like a
capacity limit.

What this checks, against a live daemon:

  * a burst that exceeds the byte budget is REFUSED, not accepted-then-killed
  * the refusal is backpressure: 503, code 108, with a Retry-After
  * an upload larger than the whole budget still succeeds at an empty queue,
    because otherwise retrying could never help
  * the byte counter returns to zero once the work drains -- a pop that forgot
    to decrement would leak budget until the queue refused everything, while
    the process looked perfectly healthy throughout
  * `url` jobs consume a slot and no bytes

Run against a daemon started with a deliberately small budget:

  BLURD_QUEUE_MAX_BYTES=$((8*1024*1024)) blurd serve --daemon --port 8770
  python3 tests/queue_bytes.py --url http://127.0.0.1:8770 --api-key K --image f.jpg
"""

import argparse
import concurrent.futures
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def post(url, key, payload, params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}/v1/images?{q}", data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "image/jpeg")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body), dict(e.headers)
        except ValueError:
            return e.code, {"raw": body[:200].decode("utf-8", "replace")}, dict(e.headers)


def get(url, key, path):
    req = urllib.request.Request(f"{url}{path}")
    req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["data"]


def unique(raw, marker):
    """A distinct image: same pixels, different bytes, so the dedup cache does
    not quietly turn a load test into a cache-hit test."""
    m = marker.encode()
    seg = b"\xff\xfe" + (len(m) + 2).to_bytes(2, "big") + m
    return raw[:2] + seg + raw[2:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--burst", type=int, default=40)
    args = ap.parse_args()

    raw = open(args.image, "rb").read()
    stats = get(args.url, args.api_key, "/v1/stats")
    q = stats.get("queue", stats)
    budget = q.get("queue_max_bytes")
    print(f"queue budget: {budget} bytes ({(budget or 0) // 1024 // 1024} MB), "
          f"image {len(raw) // 1024} kB, burst {args.burst}")
    check("the byte budget is reported at all", isinstance(budget, int) and budget > 0,
          str(budget))
    if not budget:
        return 1

    # --- a burst that cannot fit ------------------------------------------
    # Submitted without ?wait, so they queue rather than being consumed as fast
    # as they arrive.
    print("\nburst larger than the budget")
    results = []

    def submit(i):
        return post(args.url, args.api_key, unique(raw, f"qb-{i}"),
                    {"code": f"qb-{i}"})

    with concurrent.futures.ThreadPoolExecutor(16) as ex:
        results = list(ex.map(submit, range(args.burst)))

    codes = [r[0] for r in results]
    refused = [r for r in results if r[0] == 503]
    accepted = [r for r in results if r[0] in (200, 201, 202)]
    print(f"  {len(accepted)} accepted, {len(refused)} refused, "
          f"statuses {sorted(set(codes))}")

    check("some submissions were refused rather than all accepted",
          len(refused) > 0,
          f"{len(accepted)}/{args.burst} accepted -- budget may be too large "
          f"for this burst")
    check("nothing failed with an unexpected status",
          set(codes) <= {200, 201, 202, 503}, str(sorted(set(codes))))

    if refused:
        status, body, headers = refused[0]
        err = body.get("error", {})
        check("the refusal is the backpressure code (108 -> HTTP 503)",
              err.get("code") == 108, str(err.get("code")))
        check("the refusal is typed as overload, not as an upstream failure",
              err.get("type") == "overloaded", str(err.get("type")))
        check("the refusal says how long to wait",
              err.get("retry_after") or headers.get("Retry-After"),
              f"retry_after={err.get('retry_after')} header={headers.get('Retry-After')}")
        check("the refusal names the byte budget, not the job count",
              "queue_max_bytes" in (err.get("details") or {}),
              str(list((err.get("details") or {}).keys())))
        check("the refusal is marked recoverable", err.get("recoverable") is True,
              str(err.get("recoverable")))

    # --- the counter must come back to zero --------------------------------
    print("\ndraining")
    deadline = time.time() + 300
    settled = None
    while time.time() < deadline:
        d = get(args.url, args.api_key, "/v1/stats")
        qd = d.get("queue", d)
        if qd.get("in_queue", 0) == 0 and qd.get("queued_bytes", 0) == 0:
            settled = qd
            break
        time.sleep(1)
    check("queued bytes return to zero once the queue drains",
          settled is not None,
          "still holding bytes after 300 s -- the counter is leaking")

    # --- a single oversized upload at an empty queue ------------------------
    print("\noversized upload at an empty queue")
    big = unique(raw, "qb-big") * 1
    if len(big) <= budget:
        # Pad past the budget so the case is real rather than assumed.
        pad = b"\xff\xfe" + (65533).to_bytes(2, "big") + b"\x00" * 65531
        while len(big) <= budget:
            big = big[:2] + pad + big[2:]
    status, body, _ = post(args.url, args.api_key, big, {"code": "qb-big", "wait": 300})
    check("an upload bigger than the whole budget is still accepted",
          status in (200, 201, 202),
          f"{status} {json.dumps(body)[:160]}")

    # --- url jobs consume a slot, not bytes ---------------------------------
    print("\nurl jobs")
    status, body, _ = post(args.url, args.api_key, None,
                           {"url": "https://127.0.0.1:9/nope.jpg", "code": "qb-url"})
    check("a url submission is not refused by the byte budget",
          status != 503 or (body.get("error", {}).get("details", {})
                            .get("queue_max_bytes") is None),
          f"{status} {json.dumps(body)[:160]}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
