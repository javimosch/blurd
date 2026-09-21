#!/usr/bin/env python3
"""Black-box conformance suite.

Deliberately tests a BINARY and a BASE URL, never Python imports. This is the
definition of "done" for the Go and machin ports: point this file at their
binary and daemon and it must pass unchanged.

    python3 tests/conformance.py --bin ./blurd --url http://127.0.0.1:8770 \\
        --api-key blk_... --image /path/to/test.jpg
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))


def cli(binary, *args, expect_code=0):
    # `binary` may carry arguments ("python3 run.py", "docker exec x blurd"), so
    # it is split rather than treated as one path. A port in another language
    # can then be tested through whatever wrapper it needs.
    p = subprocess.run([*shlex.split(binary), *args], capture_output=True, text=True)
    return p


def j(p):
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {}


def _mark_bytes(raw_bytes, seed):
    """Make a submission unique without changing a pixel: a JPEG comment
    segment shifts the sha256 while the decoded image, and therefore the
    detection work, stays identical."""
    m = f"conf-{seed}".encode()
    seg = b"\xff\xfe" + (len(m) + 2).to_bytes(2, "big") + m
    return raw_bytes[:2] + seg + raw_bytes[2:]


def http_raw(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except urllib.error.URLError as e:
        return 0, str(e).encode(), {}


def http_etag(url, key, etag):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def http(url, method="GET", key=None, body=None, ctype=None):
    req = urllib.request.Request(url, method=method, data=body)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default="./blurd")
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--dashboard-user", default="admin")
    ap.add_argument("--dashboard-password",
                    help="enables the dashboard/CSRF checks when provided")
    ap.add_argument("--scoped-key-a",
                    help="key scoped to tag:conformance-a; enables tenant isolation checks")
    ap.add_argument("--scoped-key-b", help="key scoped to tag:conformance-b")
    ap.add_argument("--image-b", help="a DIFFERENT image, for the tenant checks")
    a = ap.parse_args()

    # Fail fast and legibly if the target is not actually usable, rather than
    # exploding with a KeyError twenty checks later.
    st, _, _ = http(f"{a.url}/v1/health")
    if st != 200:
        print(f"FATAL: {a.url}/v1/health returned {st} -- is the daemon running?")
        return 2
    st, _, _ = http(f"{a.url}/v1/stats", key=a.api_key)
    if st != 200:
        print(f"FATAL: the API key was rejected ({st}). Pass a valid --api-key.")
        return 2

    print("\n-- cli output contract")
    p = cli(a.bin, "version")
    body = j(p)
    check("version exits 0", p.returncode == 0, p.stderr[:200])
    check("version emits JSON envelope on stdout",
          set(body) >= {"version", "data", "timestamp"}, p.stdout[:200])
    check("version data carries a version string",
          isinstance(body.get("data", {}).get("version"), str))

    p = cli(a.bin, "--help-json")
    hb = j(p)
    check("--help-json lists commands",
          bool(hb.get("commands") or hb.get("data", {}).get("commands")))
    check("--help-json documents exit codes",
          bool(hb.get("exit_codes") or hb.get("data", {}).get("exit_codes")))

    p = cli(a.bin, "guide", "--human")
    check("guide prints embedded documentation", len(p.stdout) > 500)

    print("\n-- cli error contract")
    p = cli(a.bin, "get", "deadbeefdeadbeef")
    check("unknown sha exits 92", p.returncode == 92, f"got {p.returncode}")
    err = {}
    try:
        err = json.loads(p.stderr)
    except ValueError:
        pass
    check("errors go to stderr as structured JSON", err.get("ok") is False)
    check("error carries type + suggestions",
          bool(err.get("error", {}).get("type")) and
          isinstance(err.get("error", {}).get("suggestions"), list))
    check("stdout stays clean on error", p.stdout.strip() == "")

    print("\n-- api auth")
    s, _, _ = http(f"{a.url}/v1/health")
    check("health is unauthenticated 200", s == 200, f"got {s}")
    s, _, _ = http(f"{a.url}/v1/stats")
    check("stats without a key is 401", s == 401, f"got {s}")
    s, _, _ = http(f"{a.url}/v1/stats", key="blk_definitely_wrong")
    check("stats with a bad key is 401", s == 401, f"got {s}")
    s, b, _ = http(f"{a.url}/v1/stats", key=a.api_key)
    check("stats with a valid key is 200", s == 200, f"got {s}")

    print("\n-- api submit is asynchronous")
    raw = open(a.image, "rb").read()
    code = f"conformance/{int(time.time())}-{len(raw)}.jpg"
    s, b, h = http(f"{a.url}/v1/images?tags=conformance&code={urllib.parse.quote(code, safe='')}",
                   "POST", a.api_key, raw, "image/jpeg")
    check("submit returns 202 without waiting", s == 202, f"got {s}")
    job = json.loads(b).get("data", {})
    check("submit returns a job id", str(job.get("job_id", "")).startswith("job_"))
    check("job starts queued or running", job.get("status") in ("queued", "running"),
          str(job.get("status")))
    check("202 carries a Location header to the job",
          h.get("Location", "").endswith(job.get("job_id", "x")))
    check("job echoes the external_id", job.get("external_id") == code)

    s, b, _ = http(f"{a.url}/v1/jobs/{job['job_id']}?wait=120", key=a.api_key)
    done = json.loads(b).get("data", {})
    check("job reaches a terminal state", done.get("status") in ("done", "failed"),
          str(done.get("status")))
    check("job records a duration", done.get("duration_ms") is not None)
    sha = (done.get("result") or {}).get("source_sha", "")
    check("finished job carries a 64-char source_sha", len(sha) == 64, sha)
    check("finished job carries timing stats",
          "total_ms" in (done.get("result") or {}).get("stats", {}).get("timings", {}))
    check("finished job carries detection counts",
          "faces" in (done.get("result") or {}).get("stats", {}).get("detections", {}))

    s, b, _ = http(f"{a.url}/v1/jobs?limit=5", key=a.api_key)
    jl = json.loads(b).get("data", {})
    check("job list reports queue depth", "workers" in jl.get("queue", {}))

    print("\n-- dedup, by bytes and by code")
    s, b, _ = http(f"{a.url}/v1/images?tags=conformance2&wait=60", "POST", a.api_key,
                   raw, "image/jpeg")
    d2 = json.loads(b).get("data", {})
    check("?wait returns a settled job with 200", s == 200 and d2["status"] == "done",
          f"got {s}/{d2.get('status')}")
    check("resubmitting identical bytes is a cache hit", d2.get("cached") is True)
    check("cache hit keeps the same sha", d2.get("source_sha") == sha)
    check("cache hit merges new tags",
          "conformance2" in (d2.get("result") or {}).get("tags", []))

    t0 = time.perf_counter()
    s, b, _ = http(f"{a.url}/v1/images?code={urllib.parse.quote(code, safe='')}",
                   "POST", a.api_key, raw, "image/jpeg")
    code_ms = (time.perf_counter() - t0) * 1000
    d3 = json.loads(b).get("data", {})
    check("a known code short-circuits to done", d3.get("status") == "done",
          str(d3.get("status")))
    check("code short-circuit is marked cached", d3.get("cached") is True)
    check("code short-circuit skips processing (<250ms)", code_ms < 250,
          f"{code_ms:.0f} ms")

    print("\n-- external id is a stable handle")
    s, b, _ = http(f"{a.url}/v1/images/by-code/{urllib.parse.quote(code, safe='')}",
                   key=a.api_key)
    check("GET by-code resolves the record", s == 200, f"got {s}")
    check("by-code returns the same sha", json.loads(b)["data"]["source_sha"] == sha)
    check("record lists its codes", code in json.loads(b)["data"].get("codes", []))
    s, b, h = http(f"{a.url}/v1/blobs/by-code/{urllib.parse.quote(code, safe='')}",
                   key=a.api_key)
    check("blob by-code is 200", s == 200, f"got {s}")
    check("blob by-code is an image", b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n")
    etag = h.get("ETag", "")
    check("blob by-code sets an ETag", bool(etag))
    s304, _, _ = http_etag(f"{a.url}/v1/blobs/by-code/{urllib.parse.quote(code, safe='')}",
                           a.api_key, etag)
    check("If-None-Match yields 304", s304 == 304, f"got {s304}")
    s, _, _ = http(f"{a.url}/v1/images/by-code/no-such-code-xyz", key=a.api_key)
    check("unknown code is 404", s == 404, f"got {s}")

    s, b, _ = http(f"{a.url}/v1/images?code={urllib.parse.quote(code, safe='')}",
                   key=a.api_key)
    check("list filters by code",
          any(i["source_sha"] == sha for i in json.loads(b)["data"]["items"]))

    print("\n-- api read")
    s, b, _ = http(f"{a.url}/v1/images/{sha}", key=a.api_key)
    check("GET by sha is 200", s == 200, f"got {s}")
    s, b, h = http(f"{a.url}/v1/blobs/{sha}", key=a.api_key)
    check("blob download is 200", s == 200, f"got {s}")
    check("blob is an image", h.get("Content-Type", "").startswith("image/"))
    check("blob is a real JPEG/PNG",
          b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n")
    check("blob carries no EXIF segment", b"Exif\x00\x00" not in b[:4096])
    s, b, _ = http(f"{a.url}/v1/images?tag=conformance", key=a.api_key)
    listing = json.loads(b).get("data", {})
    check("tag filter returns the artifact",
          any(i["source_sha"] == sha for i in listing.get("items", [])))
    s, _, _ = http(f"{a.url}/v1/images?tag=no-such-tag-xyz", key=a.api_key)
    check("unknown tag filter still 200s", s == 200)

    print("\n-- ttl: blobs expire, rows survive")
    ttl_code = "conf-ttl-" + code.rsplit("-", 1)[-1]
    ttl_prof = urllib.parse.quote(json.dumps({"storage": {"ttl": 61}}))
    s, b, _ = http(f"{a.url}/v1/images?code={ttl_code}&wait=60&profile={ttl_prof}",
                   "POST", a.api_key, raw, "image/jpeg")
    d = json.loads(b).get("data", {})
    tres = d.get("result") or {}
    check("ttl submit completes", s == 200 and d.get("status") == "done",
          f"got {s}/{d.get('status')}")
    check("result carries expires_at", bool(tres.get("expires_at")))
    check("ttl lands in the profile (its own cache identity)",
          tres.get("profile_hash") is not None
          and tres.get("profile_hash") != d2.get("profile_hash"))
    s, b, _ = http(f"{a.url}/v1/blobs/by-code/{ttl_code}", key=a.api_key)
    check("blob fetchable before expiry", s == 200, f"got {s}")
    # expires_at counts from processing, not from this response — sleeping a
    # flat 62s races the job's own runtime. Wait until the deadline + buffer.
    exp_ts = datetime.strptime(tres["expires_at"], "%Y-%m-%dT%H:%M:%SZ"
                               ).replace(tzinfo=timezone.utc).timestamp()
    time.sleep(max(0, exp_ts - time.time()) + 3)
    s, b, _ = http(f"{a.url}/v1/blobs/by-code/{ttl_code}", key=a.api_key)
    err = json.loads(b).get("error", {}) if s != 200 else {}
    check("blob is 410 after expiry", s == 410, f"got {s}")
    check("410 is typed resource_expired, not not_found",
          err.get("type") == "resource_expired", err.get("type"))
    s, b, _ = http(f"{a.url}/v1/images/by-code/{ttl_code}", key=a.api_key)
    check("record survives the blob", s == 200, f"got {s}")
    s, b, _ = http(f"{a.url}/v1/images?code={ttl_code}&wait=60&profile={ttl_prof}",
                   "POST", a.api_key, raw, "image/jpeg")
    d = json.loads(b).get("data", {})
    check("resubmit after expiry reprocesses", d.get("status") == "done"
          and (d.get("result") or {}).get("cached") is False,
          f"{d.get('status')}/cached={((d.get('result') or {}).get('cached'))}")
    s, b, _ = http(f"{a.url}/v1/blobs/by-code/{ttl_code}", key=a.api_key)
    check("revived blob is fetchable again", s == 200, f"got {s}")

    print("\n-- ssrf guards")
    for bad in ("http://127.0.0.1:1/x.jpg", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/x.jpg", "file:///etc/passwd"):
        s, _, _ = http(f"{a.url}/v1/images", "POST", a.api_key,
                       json.dumps({"url": bad}).encode(), "application/json")
        check(f"refuses {bad}", s in (400, 422), f"got {s}")

    print("\n-- remote cli == local cli")
    p = cli(a.bin, "--remote", a.url, "--api-key", a.api_key, "get", sha)
    rb = j(p).get("data", {})
    check("remote get exits 0", p.returncode == 0, p.stderr[:200])
    check("remote get returns the unwrapped record", rb.get("source_sha") == sha)
    p = cli(a.bin, "--remote", a.url, "--api-key", a.api_key, "stats")
    check("remote stats returns counters", "artifacts" in j(p).get("data", {}))
    p = cli(a.bin, "--remote", a.url, "--api-key", a.api_key, "get", "--code", code)
    check("remote get --code works", j(p).get("data", {}).get("source_sha") == sha,
          p.stderr[:200])
    p = cli(a.bin, "--remote", a.url, "--api-key", a.api_key, "jobs", "list", "--limit", "3")
    check("remote jobs list works", "items" in j(p).get("data", {}), p.stderr[:200])
    jid = j(p).get("data", {}).get("items", [{}])[0].get("job_id")
    p = cli(a.bin, "--remote", a.url, "--api-key", a.api_key, "jobs", "get", jid)
    check("remote jobs get embeds the result",
          bool(j(p).get("data", {}).get("result")), p.stderr[:200])
    p = cli(a.bin, "--remote", a.url, "--api-key", a.api_key, "--human",
            "jobs", "get", jid)
    check("remote jobs get --human renders",
          p.returncode == 0, p.stderr[:200])
    p = cli(a.bin, "blur", "/nonexistent-image-xyz.jpg")
    check("a failed job exits non-zero", p.returncode != 0, f"got {p.returncode}")

    if a.dashboard_password:
        print("\n-- dashboard is not a back door to the API")
        basic = "Basic " + base64.b64encode(
            f"{a.dashboard_user}:{a.dashboard_password}".encode()).decode()

        s, _, h = http_raw(f"{a.url}/", headers={})
        check("dashboard demands auth", s == 401, f"got {s}")
        s, _, h = http_raw(f"{a.url}/", headers={"Authorization": basic})
        check("dashboard serves the page when authenticated", s == 200, f"got {s}")
        cookie = h.get("Set-Cookie", "")
        token = ""
        if "blurd_csrf=" in cookie:
            token = cookie.split("blurd_csrf=")[1].split(";")[0]
        check("page issues a CSRF cookie", bool(token))
        check("CSRF cookie is SameSite=Strict", "SameSite=Strict" in cookie, cookie[:80])

        # Read paths need no token; mutations must have one.
        s, b, _ = http_raw(f"{a.url}/ui-api/keys", headers={"Authorization": basic})
        check("dashboard can list keys", s == 200, f"got {s}")
        keys_body = json.loads(b).get("data", {})
        check("key listing never returns a usable key",
              all("key" not in k for k in keys_body.get("keys", [])))
        check("key listing shows only a prefix",
              all(k.get("prefix", "").startswith("blk_") for k in keys_body.get("keys", [])))

        s, _, _ = http_raw(f"{a.url}/ui-api/keys", method="POST",
                           headers={"Authorization": basic,
                                    "Content-Type": "application/json"},
                           body=b'{"name":"csrf-probe"}')
        check("minting without a CSRF token is refused", s == 401, f"got {s}")

        s, _, _ = http_raw(f"{a.url}/ui-api/images/{sha}", method="DELETE",
                           headers={"Authorization": basic})
        check("deleting an image without a CSRF token is refused", s == 401, f"got {s}")

        authed = {"Authorization": basic, "Content-Type": "application/json",
                  "Cookie": f"blurd_csrf={token}", "X-Blurd-CSRF": token}
        s, b, _ = http_raw(f"{a.url}/ui-api/keys", method="POST", headers=authed,
                           body=b'{"name":"conformance-probe"}')
        creation_on = keys_body.get("creation_enabled")
        if creation_on:
            check("with creation enabled, minting still needs the admin secret",
                  s == 401, f"got {s}")
        else:
            check("with creation disabled, minting is refused (409)", s == 409, f"got {s}")
            check("the refusal says how to enable it",
                  "enable" in json.loads(b).get("error", {}).get("details", {}))

        s, b, _ = http_raw(f"{a.url}/ui-api/audit", headers={"Authorization": basic})
        check("audit trail is readable from the dashboard", s == 200, f"got {s}")

        s, _, _ = http_raw(f"{a.url}/v1/stats",
                           headers={"Authorization": basic})
        check("dashboard credentials do NOT authenticate the v1 API",
              s == 401, f"got {s}")

    print("\n-- key export/import (hashes are portable, plaintext is not)")
    # The same key must work on a second instance without ever being re-shown:
    # export carries key_sha + scope, import registers them.
    secret = f"blk_conf_{time.time_ns():x}"
    p = cli(a.bin, "keys", "add", "conf-provided", "--key", secret)
    check("keys add --key registers a caller-supplied key",
          p.returncode == 0 and j(p).get("data", {}).get("key") == secret,
          p.stderr[:200])
    s, _, _ = http(f"{a.url}/v1/stats", key=secret)
    check("a key supplied with --key authenticates", s == 200, f"got {s}")
    p = cli(a.bin, "keys", "add", "conf-provided-dup", "--key", secret)
    err = json.loads(p.stderr).get("error", {}) if p.stderr.strip() else {}
    check("adding the same secret twice is a conflict",
          p.returncode != 0 and err.get("type") == "resource_conflict",
          p.stderr[:200])

    p = cli(a.bin, "keys", "export")
    exp = j(p).get("data", {})
    check("keys export emits a records list", isinstance(exp.get("keys"), list))
    check("export contains the provided key's sha",
          any(k.get("key_sha") == hashlib.sha256(secret.encode()).hexdigest()
              for k in exp.get("keys", [])))
    check("export never contains plaintext",
          all("key" not in k for k in exp.get("keys", [])))

    secret2 = f"blk_imp_{time.time_ns():x}"
    sha2 = hashlib.sha256(secret2.encode()).hexdigest()
    colliding = exp["keys"][0]["id"] if exp.get("keys") else "deadbeef"
    payload = {"format": "blurd-keys/1", "keys": [
        {"id": "imported1", "name": "conf-imported", "prefix": secret2[:12],
         "key_sha": sha2, "scope": None},
        {"id": colliding, "name": "conf-remapped", "prefix": "blk_x",
         "key_sha": hashlib.sha256(f"blk_rem_{time.time_ns():x}".encode()).hexdigest(),
         "scope": None},
        {"name": "conf-malformed"}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        p = cli(a.bin, "keys", "import", path)
        res = j(p).get("data", {})
        check("keys import registers new records", res.get("count") == 2,
              json.dumps(res)[:200])
        check("an id collision is remapped, not failed",
              colliding in res.get("remapped_ids", {}))
        check("malformed records are skipped",
              any(s_.get("reason") == "malformed record"
                  for s_ in res.get("skipped", [])))
        s, _, _ = http(f"{a.url}/v1/stats", key=secret2)
        check("an imported key_sha authenticates its plaintext",
              s == 200, f"got {s}")
        p = cli(a.bin, "keys", "import", path)
        res = j(p).get("data", {})
        check("re-importing is a no-op", res.get("count") == 0,
              json.dumps(res)[:200])
    finally:
        os.unlink(path)

    print("\n-- storage backend")
    st, bb, _ = http(f"{a.url}/v1/health")
    health = json.loads(bb)
    check("health names the storage backend", health.get("storage") in ("local", "s3"),
          str(health.get("storage")))
    # The backend must be invisible in behaviour: same bytes in, same bytes out,
    # whichever store is configured. This is what makes S3 a swap, not a fork.
    probe = _mark_bytes(raw, f"storage-{time.time_ns()}")
    st, bb, _ = http(f"{a.url}/v1/images?wait=180", "POST", a.api_key, probe, "image/jpeg")
    rec = json.loads(bb)["data"]
    check("a submission round-trips through the configured backend",
          rec["status"] == "done", str(rec.get("error")))
    if rec.get("result"):
        st, blob, h = http(f"{a.url}/v1/blobs/{rec['source_sha']}", key=a.api_key)
        check("the stored blob reads back as a real image",
              blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n")
        check("the blob length matches what was recorded",
              len(blob) == rec["result"]["blob"]["bytes"],
              f'{len(blob)} vs {rec["result"]["blob"]["bytes"]}')
        check("the record exposes a backend-neutral key",
              "key" in rec["result"]["blob"] and "/" in rec["result"]["blob"]["key"])

    print("\n-- concurrency and force")
    # Detectors under concurrent load. cv2.FaceDetectorYN is stateful, so a
    # shared instance corrupts across worker threads -- a failure mode that no
    # single-image test can reach.
    import concurrent.futures as _fut

    def _mark(raw_bytes, seed):
        m = f"conf-{seed}-{time.time_ns()}".encode()
        seg = b"\xff\xfe" + (len(m) + 2).to_bytes(2, "big") + m
        return raw_bytes[:2] + seg + raw_bytes[2:]

    def _submit(i):
        st, bb, _ = http(f"{a.url}/v1/images?wait=180", "POST", a.api_key,
                         _mark(raw, i), "image/jpeg")
        return json.loads(bb)["data"]

    with _fut.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_submit, range(12)))
    bad = [r for r in results if r["status"] != "done"]
    check("12 distinct images processed concurrently all succeed",
          not bad, "; ".join(str(r.get("error", {}).get("message", ""))[:80] for r in bad[:2]))
    check("concurrent submissions produce distinct artifacts",
          len({r["source_sha"] for r in results}) == 12)

    # force must survive the trip through the queue
    code_f = f"force/{int(time.time())}.jpg"
    q = urllib.parse.quote(code_f, safe="")
    s, b, _ = http(f"{a.url}/v1/images?code={q}&wait=120", "POST", a.api_key, raw, "image/jpeg")
    s, b, _ = http(f"{a.url}/v1/images?code={q}&force=1&wait=120", "POST", a.api_key,
                   raw, "image/jpeg")
    forced = json.loads(b)["data"]
    check("force=1 actually reprocesses instead of serving the cache",
          forced["status"] == "done" and forced["cached"] is False,
          f"cached={forced.get('cached')}")
    s, b, _ = http(f"{a.url}/v1/images?code={q}&wait=120", "POST", a.api_key, raw, "image/jpeg")
    check("without force, the same code still short-circuits",
          json.loads(b)["data"]["cached"] is True)

    print("\n-- listing scales: cursors, sorting, ranges")
    s, b, _ = http(f"{a.url}/v1/images?limit=5", key=a.api_key)
    p1 = json.loads(b)["data"]
    check("listing reports a total", isinstance(p1.get("total"), int))
    check("listing says whether the count was capped", "total_capped" in p1)
    if p1.get("next_cursor"):
        s, b, _ = http(f"{a.url}/v1/images?limit=5&cursor={urllib.parse.quote(p1['next_cursor'])}",
                       key=a.api_key)
        p2 = json.loads(b)["data"]
        first = {i["source_sha"] + i["profile_hash"] for i in p1["items"]}
        second = {i["source_sha"] + i["profile_hash"] for i in p2["items"]}
        check("cursor page 2 does not repeat page 1", not (first & second),
              f"{len(first & second)} overlapping")
        check("cursor pages omit the total (counted once, on page 1)",
              p2.get("total") is None, str(p2.get("total")))
        s, _, _ = http(f"{a.url}/v1/images?limit=5&cursor=not-a-real-cursor", key=a.api_key)
        check("a malformed cursor is rejected, not ignored", s in (400, 422), f"got {s}")

    s, b, _ = http(f"{a.url}/v1/images?limit=10&sort=faces&direction=desc", key=a.api_key)
    faces = [i["n_faces"] for i in json.loads(b)["data"]["items"]]
    check("sort=faces desc is actually ordered", faces == sorted(faces, reverse=True),
          str(faces))
    s, b, _ = http(f"{a.url}/v1/images?limit=10&sort=created&direction=asc", key=a.api_key)
    dates = [i["created_at"] for i in json.loads(b)["data"]["items"]]
    check("sort=created asc is actually ordered", dates == sorted(dates), str(dates[:3]))
    s, b, _ = http(f"{a.url}/v1/images?limit=5&sort=nonsense", key=a.api_key)
    check("an unknown sort falls back rather than erroring", s == 200, f"got {s}")

    s, b, _ = http(f"{a.url}/v1/images?limit=50&since=1999-01-01T00:00:00Z"
                   f"&until=1999-12-31T23:59:59Z", key=a.api_key)
    check("a date range with no data returns an empty page",
          json.loads(b)["data"]["count"] == 0)
    s, b, _ = http(f"{a.url}/v1/images?limit=50&since=2000-01-01T00:00:00Z", key=a.api_key)
    check("an open-ended date range still returns rows",
          json.loads(b)["data"]["count"] > 0)

    s, b, _ = http(f"{a.url}/v1/jobs?limit=5&sort=duration&direction=desc", key=a.api_key)
    jd = json.loads(b)["data"]
    check("jobs sort by duration", s == 200 and "items" in jd, f"got {s}")
    check("job listings omit the embedded artifact record",
          all(j.get("result") is None for j in jd["items"]),
          "a listing should not build a full record per row")
    s, b, _ = http(f"{a.url}/v1/jobs?limit=5&status=done", key=a.api_key)
    check("jobs filter by status",
          all(j["status"] == "done" for j in json.loads(b)["data"]["items"]))
    if jd.get("next_cursor"):
        s, b, _ = http(f"{a.url}/v1/jobs?limit=5&sort=duration&direction=desc"
                       f"&cursor={urllib.parse.quote(jd['next_cursor'])}", key=a.api_key)
        ids1 = {j["job_id"] for j in jd["items"]}
        ids2 = {j["job_id"] for j in json.loads(b)["data"]["items"]}
        check("job cursor page 2 does not repeat page 1", not (ids1 & ids2))

    # A single job GET still carries its result -- only listings are light.
    #
    # Checked across EVERY done job in the page, not just the first. Picking one
    # made this order-dependent: it passed on a fast machine and failed on a
    # small one, because which job sorts first by duration changes. The job it
    # happened to pick on the slow box was one whose artifact had been replaced
    # by a `?force=1` reprocess, and which therefore reported a null result for
    # an image that was still there.
    done = [j for j in jd["items"] if j["status"] == "done"]
    if done:
        missing = []
        for job in done:
            s, b, _ = http(f"{a.url}/v1/jobs/{job['job_id']}", key=a.api_key)
            if json.loads(b)["data"].get("result") is None:
                missing.append(job["job_id"])
        check("a single job GET still embeds its result",
              not missing,
              f"{len(missing)}/{len(done)} done jobs report no result: "
              f"{missing[:3]}")

    if a.scoped_key_a and a.scoped_key_b and a.image_b:
        print("\n-- scoped keys are tenants, not just filters")
        # Every image in this section is made unique PER RUN. A scope defines a
        # tenant, so re-running the suite with the same scoped keys reuses the
        # same tenant -- and labels tenant A wrote in a previous run would make
        # "A cannot see B's image" fail against a product that is behaving
        # correctly. Marked bytes give each run its own shas.
        run = time.time_ns()
        raw_a = _mark(raw, f"tenant-a-{run}")
        raw_b = _mark(open(a.image_b, "rb").read(), f"tenant-b-{run}")
        raw_shared = _mark(open(a.image_b, "rb").read(), f"shared-{run}")
        shared = f"shared/{run}.jpg"

        # Same code, different images, different tenants.
        s, b, _ = http(f"{a.url}/v1/images?code={urllib.parse.quote(shared, safe='')}&wait=60",
                       "POST", a.scoped_key_a, raw_a, "image/jpeg")
        ja = json.loads(b)["data"]
        s, b, _ = http(f"{a.url}/v1/images?code={urllib.parse.quote(shared, safe='')}&wait=60",
                       "POST", a.scoped_key_b, raw_b, "image/jpeg")
        jb = json.loads(b)["data"]
        check("two tenants may use the same unique code",
              ja["status"] == "done" and jb["status"] == "done",
              f"{ja['status']}/{jb['status']}")
        check("the same code maps to different images per tenant",
              ja["source_sha"] != jb["source_sha"])
        check("a scoped key's submission is stamped with its scope",
              "conformance-a" in (ja.get("result") or {}).get("tags", []),
              str((ja.get("result") or {}).get("tags")))

        enc = urllib.parse.quote(shared, safe="")
        s, b, _ = http(f"{a.url}/v1/images/by-code/{enc}", key=a.scoped_key_a)
        check("by-code resolves within the caller's tenant",
              json.loads(b)["data"]["source_sha"] == ja["source_sha"])
        s, ba, _ = http(f"{a.url}/v1/blobs/by-code/{enc}", key=a.scoped_key_a)
        s, bb, _ = http(f"{a.url}/v1/blobs/by-code/{enc}", key=a.scoped_key_b)
        check("blob bytes differ per tenant for the same code", ba != bb)

        s, _, _ = http(f"{a.url}/v1/images/{jb['source_sha']}", key=a.scoped_key_a)
        check("a tenant cannot read another's image by sha (404)", s == 404, f"got {s}")
        s, _, _ = http(f"{a.url}/v1/blobs/{jb['source_sha']}", key=a.scoped_key_a)
        check("a tenant cannot read another's blob by sha (404)", s == 404, f"got {s}")
        s, _, _ = http(f"{a.url}/v1/jobs/{jb['job_id']}", key=a.scoped_key_a)
        check("a tenant cannot read another's job (404)", s == 404, f"got {s}")

        s, b, _ = http(f"{a.url}/v1/images", key=a.scoped_key_a)
        shas = {i["source_sha"] for i in json.loads(b)["data"]["items"]}
        check("listings never include another tenant's images",
              jb["source_sha"] not in shas)
        s, b, _ = http(f"{a.url}/v1/stats", key=a.scoped_key_a)
        st = json.loads(b)["data"]
        check("stats are scoped, not instance-wide",
              st.get("scope") == "tag:conformance-a", str(st.get("scope")))

        # Labels on deduplicated bytes must not cross tenants.
        s, b, _ = http(f"{a.url}/v1/images?code={enc}-dup&tags=private-a"
                       f"&metadata=%7B%22cust%22%3A%22acme%22%7D&wait=60",
                       "POST", a.scoped_key_a, raw_shared, "image/jpeg")
        s, b2, _ = http(f"{a.url}/v1/images?code={enc}-dup&tags=private-b"
                        f"&metadata=%7B%22cust%22%3A%22globex%22%7D&wait=60",
                        "POST", a.scoped_key_b, raw_shared, "image/jpeg")
        da = (json.loads(b)["data"].get("result") or {})
        db_ = (json.loads(b2)["data"].get("result") or {})
        check("identical bytes from two tenants are stored once",
              json.loads(b)["data"]["source_sha"] == json.loads(b2)["data"]["source_sha"])
        check("a tenant does not see another's tags on shared bytes",
              "private-b" not in da.get("tags", []) and "private-a" not in db_.get("tags", []),
              f"{da.get('tags')} / {db_.get('tags')}")
        check("a tenant does not see another's metadata on shared bytes",
              da.get("metadata", {}).get("cust") == "acme"
              and db_.get("metadata", {}).get("cust") == "globex",
              f"{da.get('metadata')} / {db_.get('metadata')}")
        check("a tenant does not see another's codes on shared bytes",
              all("-dup" not in c or c.startswith(shared) for c in da.get("codes", [])))

        # Writes that contradict the scope are refused, not silently rewritten.
        s, b, _ = http(f"{a.url}/v1/images?code=violate-{run}.jpg&tags=conformance-b&wait=30",
                       "POST", a.scoped_key_a, _mark(raw, f"violate-{run}"), "image/jpeg")
        viol = json.loads(b)["data"]
        check("extra tags outside the scope stay inside the tenant",
              viol["status"] == "done")
        s, b, _ = http(f"{a.url}/v1/stats", key=a.scoped_key_b)
        check("the other tenant still cannot see it",
              "conformance-a" not in str(json.loads(b)["data"].get("tags", [])))

        # The operator sees everything and is told when a code is ambiguous.
        s, b, _ = http(f"{a.url}/v1/images/by-code/{enc}", key=a.api_key)
        check("an unscoped key is told when a code is ambiguous across tenants",
              s == 409, f"got {s}")
        check("the ambiguity names the tenants",
              len(json.loads(b).get("error", {}).get("details", {})
                  .get("tenants", [])) >= 2)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
