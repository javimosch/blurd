"""Async job queue.

An HTTP request must not have to stay open while an image is fetched and
processed, so POST /v1/images creates a job and returns immediately. The
producer polls GET /v1/jobs/<id>, or passes ?wait=N to block for up to N
seconds when it would rather have the answer inline.

Durability, deliberately asymmetric:
  * `url` jobs carry no payload -- the daemon can re-fetch -- so a queued or
    interrupted one is requeued on restart.
  * `stream` jobs (raw upload) hold their bytes in memory ONLY. blurd does not
    spool source images to disk, because "the source image is never written to
    disk" is the product's central promise and a spool directory would quietly
    break it. A restart therefore fails those jobs with a recoverable error
    telling the producer to resubmit -- which it can, it has the originals.
"""

import json
import queue
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from . import db, pipeline, resources, store
from .scope import Scope
from .canonical import canonical_json
from .config import Config
from .errors import (BlurdError, Conflict, Internal, NotFound, Overloaded,
                     Upstream, ValidationError)

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
ON_CONFLICT = ("reuse", "replace", "reject")


def new_id() -> str:
    return "job_" + secrets.token_hex(8)


class JobQueue:
    def __init__(self, cfg: Config, workers: int = None, maxsize: int = None,
                 instance_id: str = None, max_bytes: int = None):
        self.cfg = cfg
        # Which instance owns the jobs this queue accepts. Set by the server;
        # a bare CLI run gets its own so its rows are never mistaken for a
        # peer's.
        self.instance_id = instance_id or ("cli_" + secrets.token_hex(6))
        from . import resources
        self.workers = int(workers) if workers else resources.effective_workers(cfg)
        # On by default: measured to cut peak resident memory materially at a
        # cost that does not show up in throughput, unlike the global
        # MALLOC_TRIM_THRESHOLD_ knob. Set BLURD_MALLOC_TRIM=0 to disable.
        self._trim_after_jobs = str(cfg.get("malloc_trim", True)).lower() not in (
            "0", "false", "no", "off")
        self.maxsize = int(maxsize or cfg.get("queue_max", 1000))
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=self.maxsize)
        # TWO bounds, because they guard different things.
        #
        # `maxsize` bounds the number of jobs. `max_bytes` bounds the memory
        # those jobs are holding, which is the bound that actually matters:
        # queued uploads live in RAM by design (blurd never spools source bytes
        # -- see spec/jobs-and-codes.md), so a job-count bound promises nothing
        # about memory. 1000 jobs x ~1.5 MB is ~1.5 GB on a box the sizing model
        # thinks needs 750 MB, and the first symptom is an OOM kill.
        #
        # A `url` job holds no payload, so it consumes a slot and no bytes; an
        # upload consumes both. That asymmetry is the reason for keeping both
        # bounds rather than replacing one with the other.
        self.max_bytes = int(max_bytes) if max_bytes else \
            resources.effective_queue_bytes(cfg, self.workers)
        self._payloads: Dict[str, bytes] = {}       # stream bytes, memory only
        self._queued_bytes = 0                      # sum of len() of the above
        self._events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._draining = threading.Event()
        self._running: set = set()          # job ids currently being processed
        self._threads: List[threading.Thread] = []

    # -- lifecycle ------------------------------------------------------------
    REAP_INTERVAL = 30.0

    def start(self) -> dict:
        recovered = self.reap()
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"blurd-worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        reaper = threading.Thread(target=self._reaper, name="blurd-reaper",
                                  daemon=True)
        reaper.start()
        self._threads.append(reaper)
        return {"workers": self.workers, "queue_max": self.maxsize,
                "queue_max_bytes": self.max_bytes, **recovered}

    def _reaper(self) -> None:
        """A peer's crash should not wait for someone to restart this one."""
        while not self._stop.wait(self.REAP_INTERVAL):
            try:
                self.reap()
                self.prune_expired()
                self.prune_audit()
                cb = getattr(self, "on_sweep", None)
                if cb:
                    cb()      # the server hangs its rate-event flush here
            except Exception:
                pass          # a failed sweep must never take the process down

    def stop(self, drain_seconds: float = 0.0) -> None:
        """Stop accepting, then let in-flight work finish.

        On SIGTERM a container has a grace period; spending it finishing the
        images already decoded is better than failing them and making the
        producer resubmit. Anything still queued stays queued -- it is owned by
        this instance and will be reaped once the heartbeat stops.
        """
        self._draining.set()
        if drain_seconds > 0:
            deadline = time.time() + drain_seconds
            while time.time() < deadline:
                with self._lock:
                    busy = len(self._running)
                if busy == 0 and self._q.empty():
                    break
                time.sleep(0.2)
        self._stop.set()

    @property
    def draining(self) -> bool:
        return self._draining.is_set()

    def prune_expired(self) -> int:
        """Delete objects and thumbnails for artifacts past their TTL.

        The row survives (sha, detections, codes) -- only the bytes go, so a
        late fetch gets a typed 410 instead of a 404 that looks like the image
        was never processed. Bounded per pass; N replicas racing is safe since
        the store delete is idempotent.
        """
        conn = db.connect(self.cfg.db_file)
        blobs = store.build(self.cfg)
        rows = db.expired_blobs(conn, 200)
        for r in rows:
            try:
                blobs.delete(r["blob_path"])
            except Exception:
                pass          # already gone; the row still gets marked
            db.expire_artifact(conn, r["id"])
        if rows:
            conn.commit()
        # No conn.close(): connect() hands out the thread's cached connection;
        # closing it poisons every later query on this thread.
        return len(rows)

    def prune_audit(self, days: int = 30) -> int:
        """Retention for the audit/event log so a long-lived instance does not
        grow its home on traffic alone. 30 days by default."""
        conn = db.connect(self.cfg.db_file)
        n = db.audit_prune(conn, days)
        if n:
            conn.commit()
        return n

    def reap(self) -> dict:
        """Reclaim jobs whose owner is no longer alive.

        With one process this was "requeue everything unfinished at startup".
        With peers that is catastrophic: a restarting replica would seize jobs
        another replica is actively running. A job is orphaned only once its
        owner has stopped heartbeating, which the `instances` table already
        tracks.

        Runs at startup and periodically, so a peer's crash is picked up
        without waiting for someone to restart.
        """
        conn = db.connect(self.cfg.db_file)
        live = [i["id"] for i in db.live_instances(conn)]
        if self.instance_id not in live:
            live.append(self.instance_id)     # never reap our own in-flight work
        rows = db.orphaned_jobs(conn, (QUEUED, RUNNING), live)
        requeued, dropped = 0, 0
        for r in rows:
            if r["source_kind"] == "url":
                # No payload: any replica can re-fetch it, so adopt it.
                db.requeue_job(conn, r["id"], QUEUED, self.instance_id)
                self._q.put(r["id"])
                requeued += 1
            else:
                # The bytes lived in the dead owner's memory and were never
                # written to disk. Only the producer can supply them again.
                self._fail(conn, r["id"], Upstream(
                    "Uploaded bytes were lost when the owning instance stopped; resubmit",
                    {"reason": "stream payloads are held in memory, never spooled",
                     "previous_owner": r["owner_id"]},
                    retry_after=0))
                dropped += 1
        conn.commit()
        return {"requeued": requeued, "dropped": dropped}

    # -- submission -----------------------------------------------------------
    def submit(self, *, url=None, path=None, data=None, external_id=None,
               tags=None, metadata=None, overrides=None, force=False,
               on_conflict="reuse", scope: Scope = None) -> dict:
        if self._draining.is_set():
            # Also 503: a draining replica is refusing work it could otherwise
            # do, which is what a balancer needs to hear to route elsewhere.
            raise Overloaded("This instance is shutting down; retry",
                             {"draining": True}, retry_after=2)
        if on_conflict not in ON_CONFLICT:
            raise BlurdError(85, "invalid_argument",
                             f"on_conflict must be one of {ON_CONFLICT}",
                             {"got": on_conflict})
        conn = db.connect(self.cfg.db_file)
        scope = scope or Scope()
        # Force the submission inside the key's scope before anything else, so
        # a scoped app can never create an image it cannot subsequently read.
        tags, metadata = scope.stamp(tags, metadata)
        tenant = scope.tenant
        profile = self.cfg.resolve_profile(overrides)
        phash = Config.hash_of(profile)

        # Fast path: a known external_id under the default policy short-circuits
        # before any fetch or decode. For a re-ingesting producer this is the
        # difference between a 1 ms index hit and re-downloading the original.
        if external_id and not force and on_conflict == "reuse":
            existing = resolve_code(conn, external_id, phash, tenant)
            if existing is not None:
                return self._instant_done(conn, existing, external_id, phash,
                                          profile, tags, metadata, on_conflict,
                                          tenant)

        kind = "url" if url else ("file" if path else "stream")
        if kind == "url":
            # Reject an unfetchable URL now, with a 4xx, rather than accepting
            # the job and failing it a second later where only a poll reveals it.
            from . import fetch
            fetch.validate_url(url, self.cfg.get("fetch", {}))
        if kind == "file":
            # Read now: the worker runs later and the file may be gone by then.
            from . import fetch
            data, _ = fetch.read_file(path, self.cfg.get("fetch", {}))
            kind = "stream"
        if data is not None:
            max_bytes = int(self.cfg.get("fetch", {}).get(
                "max_bytes", 5 * 1024 * 1024))
            if len(data) > max_bytes:
                raise ValidationError(
                    f"Image too large: {len(data)} bytes > limit {max_bytes}",
                    {"max_bytes": max_bytes})
            # Magic bytes, not the Content-Type header. A body that sniffs to
            # octet-stream can never decode, so it fails now with a 422 instead
            # of queueing and dying in the worker where only a poll reveals it.
            from . import fetch
            if fetch.sniff_mime(data) == "application/octet-stream":
                raise ValidationError(
                    "Not an image: unrecognised magic bytes",
                    {"bytes": len(data)},
                    ["Supported: jpeg, png, webp, bmp, tiff"])

        job_id = new_id()
        db.insert_job(conn, {
            "id": job_id, "status": QUEUED, "external_id": external_id,
            "on_conflict": on_conflict, "source_kind": kind,
            "source_ref": url or path, "source_sha": None, "profile_hash": phash,
            "profile_json": canonical_json(profile),
            "tags_json": json.dumps(tags or []),
            "metadata_json": json.dumps(metadata or {}), "artifact_id": None,
            "cached": 0, "tenant": tenant, "force": 1 if force else 0,
            "owner_id": self.instance_id,
            "created_at": db.now(), "started_at": None, "finished_at": None,
            "duration_ms": None})

        # Admission and reservation are one critical section: checking the
        # budget and then taking it in two steps lets N concurrent uploads all
        # see room for one and all take it.
        size = len(data) if data is not None else 0
        with self._lock:
            if not self._admits(size):
                full = {"queue_max_bytes": self.max_bytes,
                        "queued_bytes": self._queued_bytes,
                        "rejected_bytes": size}
            else:
                full = None
                if data is not None:
                    self._payloads[job_id] = data
                    self._queued_bytes += size
                self._events[job_id] = threading.Event()
        if full is not None:
            return self._reject(conn, job_id, "Queue is holding too many bytes; "
                                "retry shortly", full)
        try:
            self._q.put_nowait(job_id)
        except queue.Full:
            with self._lock:
                self._release_payload(job_id)
                self._events.pop(job_id, None)
            return self._reject(conn, job_id, "Queue is full; retry shortly",
                                {"queue_max": self.maxsize})
        return self.get(job_id)

    def _admits(self, size: int) -> bool:
        """Is there room for `size` more bytes? Caller holds the lock.

        An upload arriving at an EMPTY queue is always admitted, whatever its
        size. Without that, a body larger than the budget -- possible, since
        MAX_BODY is 64 MB and a small box's budget is 22 MB -- would be
        rejected forever, and retrying would never help. Admitting it means
        one oversized payload can exceed the budget; refusing it means a
        producer that can never make progress, which is worse.
        """
        if size == 0:                       # a url job holds no bytes
            return True
        if self._queued_bytes == 0:
            return True
        return self._queued_bytes + size <= self.max_bytes

    def _release_payload(self, job_id: str) -> None:
        """Drop a payload and give its bytes back. Caller holds the lock.

        The ONLY place `_payloads` shrinks, so the counter cannot drift: a pop
        that forgot to decrement would leak budget until the queue refused
        everything, and the process would look healthy the whole time.
        """
        data = self._payloads.pop(job_id, None)
        if data is not None:
            self._queued_bytes = max(0, self._queued_bytes - len(data))

    def _reject(self, conn, job_id: str, message: str, details: dict):
        """Fail an over-budget submission with a retryable answer.

        503 with `retry_after` is backpressure, not an error: the producer is
        being asked to slow down, and telling it so is the difference between a
        queue that sheds load and one that falls over. The job row is written
        as failed first, so the producer can still look it up by its code and
        see what happened rather than finding nothing.
        """
        self._fail(conn, job_id, Overloaded(message, dict(details)))
        conn.commit()
        raise Overloaded(message, dict(details, job_id=job_id))

    def _instant_done(self, conn, sha, external_id, phash, profile,
                      tags, metadata, on_conflict, tenant="global") -> dict:
        """Record a completed job for a cache hit found before any work."""
        row = db.find_artifact(conn, sha, phash)
        job_id = new_id()
        db.merge_tags(conn, sha, tags or [], tenant)
        db.merge_metadata(conn, sha, metadata or {}, tenant)
        db.touch_code(conn, tenant, external_id, phash)
        ts = db.now()
        db.insert_job(conn, {
            "id": job_id, "status": DONE, "external_id": external_id,
            "on_conflict": on_conflict, "source_kind": "code-hit",
            "source_ref": None, "source_sha": sha, "profile_hash": phash,
            "profile_json": canonical_json(profile),
            "tags_json": json.dumps(tags or []),
            "metadata_json": json.dumps(metadata or {}), "artifact_id": row["id"],
            "cached": 1, "tenant": tenant, "force": 0,
            "owner_id": self.instance_id, "created_at": ts,
            "started_at": ts, "finished_at": ts, "duration_ms": 0})
        return self.get(job_id)

    # -- worker ---------------------------------------------------------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._running.add(job_id)
            try:
                self._run(job_id)
            except Exception as exc:                 # a worker must never die
                try:
                    conn = db.connect(self.cfg.db_file)
                    self._fail(conn, job_id, Internal(f"Worker crashed: {exc}"))
                    conn.commit()
                except Exception:
                    pass
            finally:
                if self._trim_after_jobs:
                    # A decoded 10 MP image is ~30 MB of arrays; glibc keeps
                    # the freed pages unless asked. Between jobs is the cheap
                    # moment to ask.
                    resources.malloc_trim()
                with self._lock:
                    self._running.discard(job_id)
                    self._release_payload(job_id)
                    ev = self._events.get(job_id)
                if ev:
                    ev.set()
                self._q.task_done()

    def _run(self, job_id: str) -> None:
        conn = db.connect(self.cfg.db_file)
        row = db.find_job(conn, job_id)
        if row is None or row["status"] not in (QUEUED, RUNNING):
            return
        started = time.perf_counter()
        db.mark_job_running(conn, job_id, RUNNING)

        with self._lock:
            payload = self._payloads.get(job_id)
        try:
            if row["source_kind"] == "url":
                res = pipeline.process(
                    self.cfg, url=row["source_ref"],
                    tags=json.loads(row["tags_json"]),
                    metadata=json.loads(row["metadata_json"]),
                    overrides=json.loads(row["profile_json"]),
                    tenant=row["tenant"] or "global",
                    force=bool(row["force"]))
            else:
                if payload is None:
                    raise Upstream(
                        "Uploaded bytes are no longer available; resubmit",
                        {"job_id": job_id}, retry_after=0)
                res = pipeline.process(
                    self.cfg, data=payload,
                    tags=json.loads(row["tags_json"]),
                    metadata=json.loads(row["metadata_json"]),
                    overrides=json.loads(row["profile_json"]),
                    tenant=row["tenant"] or "global",
                    force=bool(row["force"]))

            sha = res["source_sha"]
            if row["external_id"]:
                bind_code(conn, row["external_id"], sha, row["on_conflict"],
                          row["tenant"] or "global", row["profile_hash"])
            artifact = db.find_artifact(conn, sha, row["profile_hash"])
            db.mark_job_done(conn, job_id, DONE, sha, artifact["id"], res["cached"],
                             round((time.perf_counter() - started) * 1000, 2))
        except BlurdError as exc:
            self._fail(conn, job_id, exc, started)
            conn.commit()
        except Exception as exc:
            self._fail(conn, job_id, Internal(f"Processing failed: {exc}"), started)
            conn.commit()

    def _fail(self, conn, job_id: str, exc: BlurdError, started: float = None) -> None:
        db.mark_job_failed(
            conn, job_id, FAILED, json.dumps(exc.to_dict()["error"]),
            round((time.perf_counter() - started) * 1000, 2) if started else None)

    # -- reads ----------------------------------------------------------------
    def get(self, job_id: str, scope: Scope = None) -> dict:
        conn = db.connect(self.cfg.db_file)
        row = db.find_job(conn, job_id)
        if row is None:
            raise NotFound("job", job_id)
        if scope is not None and not scope.is_global:
            # Same 404 as a job that does not exist: a scoped caller should not
            # be able to probe for the existence of another tenant's jobs.
            if (row["tenant"] or "global") != scope.tenant:
                raise NotFound("job", job_id)
            return job_dict(self.cfg, conn, row, tenant=scope.tenant)
        return job_dict(self.cfg, conn, row)

    def wait(self, job_id: str, timeout: float, scope: Scope = None) -> dict:
        """Block until the job settles or the timeout expires. Callers that do
        not want to block simply omit it and poll."""
        deadline = time.time() + max(0.0, timeout)
        with self._lock:
            ev = self._events.get(job_id)
        while True:
            rec = self.get(job_id, scope)
            if rec["status"] in (DONE, FAILED):
                return rec
            remaining = deadline - time.time()
            if remaining <= 0:
                return rec
            if ev is not None:
                ev.wait(min(remaining, 0.25))
            else:
                time.sleep(min(remaining, 0.1))

    def depth(self, scope: Scope = None) -> dict:
        conn = db.connect(self.cfg.db_file)
        tenant = scope.tenant if scope is not None and not scope.is_global else None
        counts = db.job_status_counts(conn, tenant)
        with self._lock:
            queued_bytes = self._queued_bytes
        return {"queued": counts.get(QUEUED, 0), "running": counts.get(RUNNING, 0),
                "done": counts.get(DONE, 0), "failed": counts.get(FAILED, 0),
                "in_queue": self._q.qsize(), "workers": self.workers,
                "queue_max": self.maxsize,
                # The bound that actually constrains a small box, so it is the
                # one an operator needs to see going up.
                "queued_bytes": queued_bytes,
                "queue_max_bytes": self.max_bytes}


# -- external id -------------------------------------------------------------

def bind_code(conn, external_id: str, sha: str, on_conflict: str,
              tenant: str = "global", profile_hash: str = None) -> None:
    row = db.find_code(conn, tenant, external_id)
    if row is None:
        try:
            db.insert_code(conn, tenant, external_id, sha, profile_hash)
            return
        except Exception as exc:
            # Two replicas can both find nothing and both insert. The unique
            # index is the arbiter, not the SELECT above -- re-read and fall
            # through to the normal conflict logic.
            if not db.dialect().is_unique_violation(exc):
                raise
            conn.rollback()
            row = db.find_code(conn, tenant, external_id)
            if row is None:
                raise
    if row["source_sha"] == sha:
        db.touch_code(conn, tenant, external_id, profile_hash)
        return
    # Same code, different bytes. Silently repointing would make a consumer's
    # cached URL return a different photo, so it takes an explicit policy.
    if on_conflict == "replace":
        db.repoint_code(conn, tenant, external_id, sha, profile_hash)
        return
    raise Conflict(
        f"external_id '{external_id}' already maps to a different image",
        {"external_id": external_id, "existing_sha": row["source_sha"],
         "new_sha": sha,
         "hint": "resubmit with on_conflict=replace to repoint it"})


def resolve_code(conn, external_id: str, profile_hash: str = None,
                 tenant: str = "global", any_tenant: bool = False) -> Optional[str]:
    """external_id -> source_sha, within a tenant, only if a usable artifact
    exists. The tenant is part of the primary key, so this stays one index
    seek.

    `any_tenant` is for the operator (an unscoped key), who otherwise could not
    resolve a code at all -- every code belongs to some tenant's namespace, and
    the operator is in none of them. If the code is ambiguous across tenants the
    caller is told so rather than served an arbitrary one.
    """
    if any_tenant:
        rows = db.find_code_any_tenant(conn, external_id)
        if not rows:
            return None
        shas = {r["source_sha"] for r in rows}
        if len(shas) > 1:
            raise Conflict(
                f"external_id '{external_id}' is used by {len(rows)} tenants "
                "and points at different images",
                {"external_id": external_id,
                 "tenants": sorted(r["tenant"] for r in rows),
                 "hint": "pass ?tenant=<tenant> to disambiguate"})
        sha = rows[0]["source_sha"]
    else:
        row = db.find_code(conn, tenant, external_id)
        if row is None:
            return None
        sha = row["source_sha"]
    if profile_hash:
        art = db.find_artifact(conn, sha, profile_hash)
        # An expired or pruned artifact does not satisfy the code: the caller
        # gets a normal submission and the image is reprocessed instead.
        if art is None or not art["blob_path"] or db.artifact_expired(art):
            return None
    return sha


def job_dict(cfg: Config, conn, row, tenant: str = None, light: bool = False) -> dict:
    """`light` omits the embedded artifact record.

    A listing renders id, status, code, duration and sha -- none of which need
    the full record, yet building it cost ~7 queries per done job, so a 25-row
    page ran ~175 queries to display six columns."""
    out = {
        "job_id": row["id"],
        "status": row["status"],
        "external_id": row["external_id"],
        "source_sha": row["source_sha"],
        "profile_hash": row["profile_hash"],
        "source_kind": row["source_kind"],
        "source_ref": row["source_ref"],
        "cached": bool(row["cached"]),
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_ms": row["duration_ms"],
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "result": None,
    }
    if light:
        return out
    if row["status"] == DONE and row["artifact_id"]:
        art = db.find_artifact_by_id(conn, row["artifact_id"])
        if art is None and row["source_sha"] and row["profile_hash"]:
            # The recorded artifact is gone, but that does not mean the job's
            # output is. `?force=1` and `on_conflict=replace` REPLACE the
            # artifact for a (sha, profile_hash): the old row is deleted and a
            # new one inserted, leaving every earlier job pointing at an id that
            # no longer exists. Those jobs then reported `result: null` while
            # the redacted image they produced was sitting right there.
            #
            # A job's output is identified by (source_sha, profile_hash) -- that
            # is the cache key the whole service is built on. The artifact row
            # id is an implementation detail, so fall back to it.
            art = db.find_artifact(conn, row["source_sha"], row["profile_hash"])
        if art:
            out["result"] = pipeline.artifact_result(
                cfg, conn, art, cached=bool(row["cached"]), tenant=tenant)
            out["blob_url"] = out["result"]["blob"]["url"]
            if row["external_id"]:
                out["blob_url_by_code"] = f"/v1/blobs/by-code/{row['external_id']}"
    return out
