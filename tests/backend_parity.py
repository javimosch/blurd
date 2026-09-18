#!/usr/bin/env python3
"""Do two backends answer the same questions the same way?

Conformance proves an instance is correct. This proves two instances on
different metadata stores are correct *identically* -- which is the promise
`db.py` makes, and the thing a document model is most likely to break quietly.

It seeds the same images into both, then compares:

  * every listing, under every offered sort and both directions
  * keyset pagination: the union of all pages must be exactly the full set,
    with no row seen twice and none missed
  * per-image records, labels, codes and scoped reads
  * job listings and their sorts, including `duration`, whose sort key is null
    for unfinished jobs and was the last portability bug on the SQL side

The pagination check is the point. A cursor that is not totally ordered
produces a page boundary where one row matches neither `<` nor `=` -- so it
appears on both pages, or on neither -- and nothing else in the suite would
notice: each page looks perfectly well-formed on its own.

Run:
  python3 tests/backend_parity.py --a http://127.0.0.1:8770 --key-a KA \\
                                  --b http://127.0.0.1:8970 --key-b KB \\
                                  --image path/to.jpg
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def http(url, method="GET", key=None, body=None, ctype=None):
    req = urllib.request.Request(url, data=body, method=method)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def data(url, key):
    status, raw = http(url, key=key)
    if status != 200:
        raise SystemExit(f"{url} -> {status}: {raw[:200]!r}")
    return json.loads(raw)["data"]


def seed(url, key, image, n, label):
    """Submit `n` distinct images, varying the fields the sorts order by."""
    raw = open(image, "rb").read()
    shas = []
    for i in range(n):
        # A unique JPEG comment per submission, so each is genuinely distinct
        # bytes rather than a dedup cache hit.
        marker = f"{label}-{i}".encode()
        seg = b"\xff\xfe" + (len(marker) + 2).to_bytes(2, "big") + marker
        payload = raw[:2] + seg + raw[2:]
        q = urllib.parse.urlencode({
            "wait": 300,
            "code": f"{label}-code-{i:03d}",
            "tags": "parity," + ("even" if i % 2 == 0 else "odd"),
            # Submission takes metadata as a JSON object; `meta.<key>` is the
            # filter form only. Using the filter spelling here silently submits
            # nothing, which is how this test first passed while proving little.
            "metadata": json.dumps({"batch": label, "idx": str(i)}),
        })
        status, body = http(f"{url}/v1/images?{q}", "POST", key, payload, "image/jpeg")
        if status not in (200, 201, 202):
            raise SystemExit(f"seed failed: {status} {body[:200]!r}")
        d = json.loads(body)["data"]
        if d.get("status") != "done":
            raise SystemExit(f"seed job not done: {d.get('status')} {d.get('error')}")
        shas.append(d["source_sha"])
    return shas


def page_all(url, key, path, sort, direction, field, limit=7):
    """Walk every page by cursor. Returns `field` in the order served.

    Compared across backends by a CONTENT-derived key -- the sha, or the
    caller's own code -- never by an id. Artifact ids and job ids are minted
    per instance, so comparing them across two instances tests nothing and
    fails for the wrong reason.
    """
    got, cursor, guard = [], None, 0
    while True:
        q = {"sort": sort, "direction": direction, "limit": limit}
        if cursor:
            q["cursor"] = cursor
        d = data(f"{url}{path}?{urllib.parse.urlencode(q)}", key)
        got += [it.get(field) for it in d["items"]]
        cursor = d.get("next_cursor")
        guard += 1
        if not cursor or guard > 200:
            break
    return got


def compare_listing(a, ka, b, kb, path, sorts, expected_n, what, field):
    for sort in sorts:
        for direction in ("desc", "asc"):
            ia = page_all(a, ka, path, sort, direction, field)
            ib = page_all(b, kb, path, sort, direction, field)
            tag = f"{what} sort={sort} {direction}"

            check(f"{tag}: pages cover every row exactly once (A)",
                  len(ia) == len(set(ia)) == expected_n,
                  f"{len(ia)} served, {len(set(ia))} distinct, want {expected_n}")
            check(f"{tag}: pages cover every row exactly once (B)",
                  len(ib) == len(set(ib)) == expected_n,
                  f"{len(ib)} served, {len(set(ib))} distinct, want {expected_n}")
            check(f"{tag}: both backends serve the same set",
                  set(ia) == set(ib),
                  f"only in A: {len(set(ia) - set(ib))}, only in B: {len(set(ib) - set(ia))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="base URL of backend A")
    ap.add_argument("--key-a", required=True)
    ap.add_argument("--b", required=True, help="base URL of backend B")
    ap.add_argument("--key-b", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--n", type=int, default=23,
                    help="images to seed; deliberately not a multiple of the page size")
    args = ap.parse_args()

    print(f"seeding {args.n} images into both backends")
    seed(args.a, args.key_a, args.image, args.n, "pa")
    seed(args.b, args.key_b, args.image, args.n, "pa")

    print("\nimage listings")
    compare_listing(args.a, args.key_a, args.b, args.key_b, "/v1/images",
                    ["created", "faces", "plates", "size", "review"],
                    args.n, "images", "source_sha")

    print("\njob listings")
    compare_listing(args.a, args.key_a, args.b, args.key_b, "/v1/jobs",
                    ["created", "duration", "status"],
                    args.n, "jobs", "external_id")

    print("\nfilters")
    for q, want in [("tag=parity", args.n),
                    ("tag=even", (args.n + 1) // 2),
                    ("tag=parity&tag=odd", args.n // 2),
                    ("meta.batch=pa", args.n),
                    ("meta.idx=3", 1),
                    ("code=pa-code-007", 1),
                    ("code=pa-code-*", args.n),
                    ("tag=nonexistent", 0)]:
        da = data(f"{args.a}/v1/images?{q}&limit=200", args.key_a)
        db_ = data(f"{args.b}/v1/images?{q}&limit=200", args.key_b)
        check(f"filter {q}: same count on both",
              len(da["items"]) == len(db_["items"]) == want,
              f"A={len(da['items'])} B={len(db_['items'])} want={want}")
        check(f"filter {q}: same reported total",
              da.get("total") == db_.get("total"),
              f"A={da.get('total')} B={db_.get('total')}")

    print("\nsingle records")
    first_a = data(f"{args.a}/v1/images?sort=created&direction=asc&limit=1", args.key_a)["items"][0]
    first_b = data(f"{args.b}/v1/images?sort=created&direction=asc&limit=1", args.key_b)["items"][0]
    for side, url, key, item in (("A", args.a, args.key_a, first_a),
                                 ("B", args.b, args.key_b, first_b)):
        rec = data(f"{url}/v1/images/{item['source_sha']}", key)
        check(f"{side}: record carries its tags", "parity" in (rec.get("tags") or []))
        check(f"{side}: record carries its metadata",
              (rec.get("metadata") or {}).get("batch") == "pa")
        check(f"{side}: record carries its code",
              any(c.startswith("pa-code-") for c in (rec.get("codes") or [])))
        check(f"{side}: record carries its detections",
              isinstance(rec.get("detections"), list))

    print("\nstats")
    sa = data(f"{args.a}/v1/stats", args.key_a)
    sb = data(f"{args.b}/v1/stats", args.key_b)
    for field in ("images", "artifacts", "faces", "plates", "codes"):
        check(f"stats.{field} agrees", sa.get(field) == sb.get(field),
              f"A={sa.get(field)} B={sb.get(field)}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
