#!/usr/bin/env python3
"""Multi-replica correctness: job ownership and reaping.

These are the properties that only break with more than one process, and that
no single-instance test can reach. Run against a SHARED backend (Postgres) with
at least one live replica already serving.

    BLURD_DB_BACKEND=postgres BLURD_DB_DSN=... python3 tests/multi_replica.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import db, jobs                                  # noqa: E402
from src.config import Config                             # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))


def _insert(conn, job_id, owner, kind, status=jobs.QUEUED):
    db.insert_job(conn, {
        "id": job_id, "status": status, "external_id": None, "on_conflict": "reuse",
        "source_kind": kind, "source_ref": "https://example.invalid/x.jpg",
        "source_sha": None, "profile_hash": "p", "profile_json": "{}",
        "tags_json": "[]", "metadata_json": "{}", "artifact_id": None, "cached": 0,
        "tenant": "global", "force": 0, "owner_id": owner,
        "created_at": db.now(), "started_at": None, "finished_at": None,
        "duration_ms": None})


def main():
    cfg = Config()
    db.dialect(cfg)
    d = db.dialect()
    print(f"backend: {d.name}")
    if not d.shareable:
        print("SKIP: these properties only apply to a shared backend "
              "(BLURD_DB_BACKEND=postgres)")
        return 0

    conn = db.connect()
    db.init(cfg.db_file)

    live = db.live_instances(conn)
    check("at least one live replica is registered", len(live) >= 1,
          f"found {len(live)}")
    if not live:
        return 1
    live_owner = live[0]["id"]
    dead_owner = "i_dead_" + str(int(time.time()))
    stamp = int(time.time() * 1000)

    live_url = f"job_live_url_{stamp}"
    dead_url = f"job_dead_url_{stamp}"
    dead_upload = f"job_dead_up_{stamp}"
    _insert(conn, live_url, live_owner, "url")
    _insert(conn, dead_url, dead_owner, "url")
    _insert(conn, dead_upload, dead_owner, "stream")
    conn.commit()

    # A reaper belonging to a THIRD instance, as a peer would be.
    q = jobs.JobQueue(cfg, instance_id="i_reaper_test")
    result = q.reap()

    live_after = db.find_job(conn, live_url)
    dead_after = db.find_job(conn, dead_url)
    up_after = db.find_job(conn, dead_upload)

    check("a live peer's job is NOT reclaimed",
          live_after["owner_id"] == live_owner and live_after["status"] == jobs.QUEUED,
          f"owner={live_after['owner_id']} status={live_after['status']}")
    check("a dead owner's url job IS adopted",
          dead_after["owner_id"] == "i_reaper_test" and dead_after["status"] == jobs.QUEUED,
          f"owner={dead_after['owner_id']} status={dead_after['status']}")
    check("a dead owner's upload job is failed, not adopted",
          up_after["status"] == jobs.FAILED, f"status={up_after['status']}")
    if up_after["status"] == jobs.FAILED:
        err = json.loads(up_after["error_json"] or "{}")
        check("the failure tells the producer to resubmit",
              "resubmit" in err.get("message", "").lower(), err.get("message", "")[:60])
        check("the failure is marked recoverable", err.get("recoverable") is True)
        check("the failure names the previous owner",
              err.get("details", {}).get("previous_owner") == dead_owner)
    check("reap reports what it did",
          result.get("requeued", 0) >= 1 and result.get("dropped", 0) >= 1, str(result))

    # Reaping twice must not double-adopt or resurrect anything.
    again = q.reap()
    live_twice = db.find_job(conn, live_url)
    check("reaping again still leaves the live peer's job alone",
          live_twice["owner_id"] == live_owner)
    check("reaping is idempotent for already-failed jobs",
          db.find_job(conn, dead_upload)["status"] == jobs.FAILED, str(again))

    # Through db.py, not raw SQL: this test runs against every backend, and
    # the mongo one has no `execute`. (It said so, loudly, the first time.)
    for jid in (live_url, dead_url, dead_upload):
        db.delete_job(conn, jid)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
