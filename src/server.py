"""HTTP server: the M2M JSON API under /v1 and the human dashboard under /.

Uses ThreadingHTTPServer from the stdlib rather than a framework, on purpose:
the wire contract is what a Go/machin port must reimplement, and a stdlib
implementation keeps that contract free of framework-specific behaviour.
Image upload is raw-body (Content-Type: image/*) rather than multipart --
simpler for machine callers and for every port.
"""

import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, auth, db, jobs as jobs_mod, scope as scope_mod
from .client import LocalClient
from .config import Config
from .errors import (AuthFailed, BlurdError, Conflict, Expired, Internal,
                     NotFound, ValidationError)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
MAX_BODY = 64 * 1024 * 1024

_ROUTES = [
    ("POST",   re.compile(r"^/v1/images/?$")),
    ("GET",    re.compile(r"^/v1/images/?$")),
    ("GET",    re.compile(r"^/v1/images/(?P<sha>[0-9a-f]{6,64})$")),
    ("DELETE", re.compile(r"^/v1/images/(?P<sha>[0-9a-f]{6,64})$")),
    ("GET",    re.compile(r"^/v1/blobs/(?P<sha>[0-9a-f]{6,64})$")),
    ("GET",    re.compile(r"^/v1/thumbs/(?P<sha>[0-9a-f]{6,64})$")),
    ("GET",    re.compile(r"^/v1/stats/?$")),
    ("GET",    re.compile(r"^/v1/health/?$")),
    ("POST",   re.compile(r"^/v1/feedback/?$")),
    ("GET",    re.compile(r"^/v1/feedback/?$")),
]

# cli-feedback-spec: open intake, but bounded.
FEEDBACK_MAX_BYTES = 16384
FEEDBACK_MAX_PER_MIN = 30


class Handler(BaseHTTPRequestHandler):
    server_version = f"blurd/{__version__}"
    # HTTP/1.0: see BoundedThreadingHTTPServer -- keep-alive would pin threads
    # from a bounded pool and starve new requests.
    protocol_version = "HTTP/1.0"

    # -- plumbing -------------------------------------------------------------
    def log_message(self, fmt, *args):     # access log to stderr, not stdout
        self.server.blurd_log("%s - %s" % (self.address_string(), fmt % args))

    def _send_cached(self, data: bytes, ctype: str, etag: str, extra=None):
        """Conditional GET for blobs. A user-facing app behind a backend will
        request the same redacted image repeatedly; a 304 saves the transfer
        and the disk read."""
        if (self.headers.get("If-None-Match") or "").strip('"') == etag:
            self.send_response(304)
            self.send_header("ETag", f'"{etag}"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        head = {"ETag": f'"{etag}"', "Cache-Control": "private, max-age=86400"}
        head.update(extra or {})
        self._send(200, data, ctype, head)

    def _send(self, status, payload, ctype="application/json", extra=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Blurd-Version", __version__)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, exc: BlurdError):
        self._send(exc.http_status, exc.to_dict())

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _one(self, q, key, default=None):
        v = q.get(key)
        return v[0] if v else default

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValidationError(f"Request body too large (> {MAX_BODY} bytes)")
        return self.rfile.read(length) if length else b""

    # -- dispatch -------------------------------------------------------------
    def do_GET(self):    self._dispatch("GET")
    def do_POST(self):   self._dispatch("POST")
    def do_DELETE(self): self._dispatch("DELETE")
    def do_HEAD(self):   self._dispatch("GET")

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        try:
            # cli-daemon-spec endpoints live outside /v1 and outside the
            # dashboard: an orchestrator probes them with no credentials.
            if path.rstrip("/") == "/_health" and method == "GET":
                return self._daemon_health()
            if path.rstrip("/") == "/_shutdown" and method == "POST":
                return self._daemon_shutdown()
            if path.startswith("/v1/"):
                return self._api(method, path)
            return self._ui(method, path)
        except BlurdError as exc:
            self._error(exc)
        except ValueError as exc:
            # e.g. a malformed pagination cursor: the caller's problem, not ours
            self._error(ValidationError(str(exc) or "Invalid parameter"))
        except BrokenPipeError:
            pass
        except Exception as exc:                      # never leak a traceback
            self.server.blurd_log(f"unhandled: {exc!r}")
            self._error(Internal("Unhandled server error", {"repr": repr(exc)[:200]}))

    # -- daemon lifecycle (cli-daemon-spec) -----------------------------------
    def _daemon_health(self):
        srv = self.server
        draining = srv.blurd_queue.draining
        return self._send(503 if draining else 200, {
            "ok": not draining,
            "service": "blurd",
            "pid": os.getpid(),
            "version": __version__,
            "status": "draining" if draining else "healthy",
        })

    def _daemon_shutdown(self):
        # Loopback-only rather than token-gated: a remote caller cannot stop
        # the process at all, which is strictly tighter than the spec's rule.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            raise AuthFailed("_shutdown is loopback-only")
        self._send(200, {"ok": True, "stopping": True})

        def _term():
            import signal, time
            time.sleep(0.3)          # let the response flush first
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=_term, daemon=True).start()

    # -- feedback (cli-feedback-spec) ------------------------------------------
    def _feedback_submit(self, conn):
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        if length > FEEDBACK_MAX_BYTES:
            return self._send(413, {"ok": False, "error": {
                "code": 87, "type": "too_large",
                "message": f"Feedback body exceeds {FEEDBACK_MAX_BYTES} bytes"}})
        ip = self.client_address[0]
        hits = srv.blurd_feedback_hits.setdefault(ip, [])
        now_ = time.time()
        hits[:] = [t for t in hits if now_ - t < 60]
        if len(hits) >= FEEDBACK_MAX_PER_MIN:
            return self._send(429, {"ok": False, "error": {
                "code": 108, "type": "overloaded",
                "message": "Too much feedback from this address"}})
        hits.append(now_)
        try:
            payload = json.loads(self._body() or b"{}")
        except ValueError:
            return self._send(400, {"ok": False, "error": {
                "code": 85, "type": "invalid_argument",
                "message": "Request body is not valid JSON"}})
        message = str(payload.get("message") or "").strip()
        if not message:
            return self._send(400, {"ok": False, "error": {
                "code": 85, "type": "invalid_argument",
                "message": "feedback requires a 'message'"}})
        fb_id = str(payload.get("id") or secrets.token_hex(16))
        db.insert_feedback(conn, fb_id,
                           str(payload.get("kind") or "note")[:24],
                           message[:FEEDBACK_MAX_BYTES],
                           str(payload.get("context") or "")[:FEEDBACK_MAX_BYTES],
                           str(payload.get("reporter") or "")[:64],
                           str(payload.get("version") or "")[:32], ip)
        # `stored` is true for a fresh insert and an idempotent no-op alike:
        # both mean "safely recorded".
        return self._send(200, {"ok": True, "id": fb_id, "stored": True})

    def _feedback_list(self, conn):
        # Submission is open; reading is not. The admin token is an operator
        # (unscoped) API key.
        principal = auth.require_api_key(conn, self.headers)
        if not principal["scope"].is_global:
            raise scope_mod.ScopeViolation("reading feedback is operator-only")
        limit = int(self._one(self._query(), "limit", "50") or 50)
        rows = [dict(r) for r in db.list_feedback(conn, min(limit, 200))]
        return self._send(200, {"ok": True, "feedback": rows})

    # -- API ------------------------------------------------------------------
    def _api(self, method, path):
        srv = self.server
        client: LocalClient = srv.blurd_client
        conn = db.connect(srv.blurd_cfg.db_file)

        if path.rstrip("/") == "/v1/health":
            srv_ = self.server
            draining = srv_.blurd_queue.draining
            return self._send(503 if draining else 200, {
                "ok": not draining,
                "version": __version__,
                "status": "draining" if draining else "healthy",
                "storage": srv_.blurd_store.kind,
                "database": _db_kind(),
                "instance": srv_.blurd_instance_id,
            })

        if path.rstrip("/") == "/v1/feedback":
            if method == "POST":
                return self._feedback_submit(conn)
            if method == "GET":
                return self._feedback_list(conn)

        # Everything else is machine-to-machine and needs a key.
        principal = auth.require_api_key(conn, self.headers)
        scope = principal["scope"]
        q = self._query()

        if path.rstrip("/") == "/v1/stats":
            return self._send(200, {"ok": True, "data": client.stats(scope)})

        # --- consumer hot path: one indexed lookup, straight to bytes -------
        m = re.match(r"^/v1/blobs/by-code/(?P<code>.+)$", path)
        if m and method == "GET":
            return self._serve_by_code(client, conn, urllib.parse.unquote(m.group("code")),
                                       self._one(q, "profile"), scope=scope,
                                       tenant=self._one(q, "tenant"))
        m = re.match(r"^/v1/thumbs/by-code/(?P<code>.+)$", path)
        if m and method == "GET":
            return self._serve_by_code(client, conn, urllib.parse.unquote(m.group("code")),
                                       self._one(q, "profile"), thumb=True, scope=scope)
        m = re.match(r"^/v1/images/by-code/(?P<code>.+)$", path)
        if m and method == "GET":
            return self._send(200, {"ok": True, "data": client.by_code(
                urllib.parse.unquote(m.group("code")), self._one(q, "profile"),
                scope, self._one(q, "tenant"))})

        # --- jobs -----------------------------------------------------------
        if path.rstrip("/") == "/v1/jobs" and method == "GET":
            return self._send(200, {"ok": True, "data": client.jobs(
                scope=scope, **_job_filters(q))})
        m = re.match(r"^/v1/jobs/(?P<jid>job_[0-9a-f]{8,32})$", path)
        if m and method == "GET":
            wait = min(float(self._one(q, "wait", 0) or 0), 120.0)
            rec = client.job(m.group("jid"), wait=wait, scope=scope)
            return self._send(200, {"ok": True, "data": rec})

        m = re.match(r"^/v1/(blobs|thumbs)/(?P<sha>[0-9a-f]{6,64})$", path)
        if m and method == "GET":
            return self._serve_image(client, conn, m.group("sha"),
                                     self._one(q, "profile"),
                                     thumb=path.startswith("/v1/thumbs"), scope=scope)

        m = re.match(r"^/v1/images/(?P<sha>[0-9a-f]{6,64})$", path)
        if m:
            if method == "GET":
                return self._send(200, {"ok": True, "data": client.get(
                    m.group("sha"), self._one(q, "profile"), scope)})
            if method == "DELETE":
                return self._send(200, {"ok": True, "data": client.delete(
                    m.group("sha"), scope)})

        if path.rstrip("/") == "/v1/images":
            if method == "GET":
                return self._send(200, {"ok": True, "data": client.list(
                    scope=scope, **_filters(q))})
            if method == "POST":
                return self._submit(client, q, scope)

        raise NotFound("endpoint", f"{method} {path}")

    def _submit(self, client, q, scope=None):
        """Always returns a job. ?wait=N blocks for up to N seconds so a caller
        that wants the answer inline can have it without the API pretending to
        be synchronous."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = self._body()
        wait = min(float(self._one(q, "wait", 0) or 0), 120.0)
        force = _truthy(self._one(q, "force"))
        on_conflict = self._one(q, "on_conflict", "reuse")

        if ctype == "application/json" or (raw[:1] == b"{" and not ctype.startswith("image/")):
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                raise ValidationError("Request body is not valid JSON")
            if not payload.get("url"):
                raise ValidationError(
                    "JSON submissions require a 'url' field",
                    suggestions=["POST raw image bytes with Content-Type: image/jpeg",
                                 'or POST {"url": "https://..."}'])
            job = client.submit(
                url=payload["url"], code=payload.get("external_id") or payload.get("code"),
                tags=payload.get("tags"), metadata=payload.get("metadata"),
                overrides=payload.get("profile"), force=force,
                on_conflict=payload.get("on_conflict", on_conflict), wait=wait,
                scope=scope)
        else:
            if not raw:
                raise ValidationError("Empty request body")
            tags = [t for t in (self._one(q, "tags") or "").split(",") if t]
            meta = json.loads(self._one(q, "metadata") or "{}")
            prof = json.loads(self._one(q, "profile") or "{}")
            job = client.submit(
                data=raw, code=self._one(q, "code") or self._one(q, "external_id"),
                tags=tags, metadata=meta, overrides=prof, force=force,
                on_conflict=on_conflict, wait=wait, scope=scope)

        status = 200 if job["status"] in ("done", "failed") else 202
        extra = {"Location": f"/v1/jobs/{job['job_id']}"}
        return self._send(status, {"ok": True, "data": job}, extra=extra)

    def _ui_keys(self, client, conn, cfg):
        """List is always available -- seeing which keys exist and when each was
        last used is plain operational hygiene. Creation is gated."""
        if self.command == "GET":
            return self._send(200, {"ok": True, "data": {
                "keys": auth.listing(conn),
                "creation_enabled": bool(cfg.get("dashboard_allow_key_creation")),
                "creation_requires_secret": True,
                "note": ("Key creation from the dashboard is disabled. Enable it "
                         "with: blurd dashboard-keys enable --secret <secret>")
                        if not cfg.get("dashboard_allow_key_creation") else None,
            }})
        if self.command != "POST":
            raise NotFound("endpoint", "/keys")

        if not cfg.get("dashboard_allow_key_creation"):
            raise Conflict(
                "Key creation from the dashboard is disabled",
                {"reason": "a minted key outlives the dashboard password, so "
                           "this is opt-in",
                 "enable": "blurd dashboard-keys enable --secret <secret>"})
        if not auth.check_step_up(self.headers, cfg.get("dashboard_key_secret")):
            raise AuthFailed(
                "The admin secret is required to mint an API key",
                {"header": "X-Blurd-Admin-Secret"},
                )
        try:
            payload = json.loads(self._body() or b"{}")
        except ValueError:
            raise ValidationError("Request body is not valid JSON")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationError("A key needs a name",
                                  suggestions=["Name it after the caller, e.g. 'photo-ingest'"])
        sc = scope_mod.Scope(payload.get("scope") or {})
        data = auth.generate(conn, name, actor="dashboard",
                             source_ip=self.address_string(), scope=sc)
        return self._send(201, {"ok": True, "data": data})

    def _serve_by_code(self, client, conn, code, profile, thumb=False, scope=None,
                       tenant=None):
        """Resolve external_id -> artifact in ONE indexed query and serve the
        bytes. The generic record path builds a full JSON record first; this is
        the endpoint a user-facing app hits thousands of times, so it does not."""
        # A scoped caller is pinned to its own tenant; an operator may name one
        # with ?tenant=, and otherwise sees across all of them.
        if scope is not None and not scope.is_global:
            tenant = scope.tenant
        row = db.blob_ref_by_code(conn, code, profile=profile, tenant=tenant,
                                  with_thumb=True)
        if row is None:
            raise NotFound("external_id", code)
        self._check_expired(conn, row, "external_id", code)
        if thumb:
            return self._send_cached(row["thumb"] or b"", "image/jpeg",
                                     (row["blob_sha"] or "")[:32] + "-t")
        data = self.server.blurd_store.get(row["blob_path"])
        return self._send_cached(data, row["mime"], row["blob_sha"], {
            "Content-Disposition": f'inline; filename="{code}"',
            "X-Blurd-Source-Sha": row["source_sha"],
            "X-Blurd-External-Id": code,
        })

    def _check_expired(self, conn, row, res_type, res_id) -> None:
        """410 for a TTL'd artifact whose blob is gone or due. The row stays;
        lazily prune the object here so a slow sweep never serves stale bytes."""
        if not db.artifact_expired(row):
            return
        # sqlite3.Row has no .get: column presence was already established.
        if "blob_path" in row.keys() and row["blob_path"]:
            self.server.blurd_store.delete(row["blob_path"])
        if "id" in row.keys() and row["id"] is not None:
            db.expire_artifact(conn, row["id"])
            conn.commit()
        raise Expired(res_type, res_id, {"expired_at": row["expires_at"]})

    def _serve_image(self, client, conn, sha, profile, thumb=False, scope=None):
        """Serve bytes from one indexed lookup.

        This used to call client.get(), building and discarding a full JSON
        record -- detections, tags, metadata, codes -- for every tile in the
        grid. 24 tiles meant 24 wasted record builds per page view.
        """
        row = (db.thumb_for(conn, sha, profile) if thumb
               else db.blob_ref(conn, sha, profile))
        if row is None:
            raise NotFound("image", sha)
        if scope is not None and not scope.is_global:
            if not scope.allows_sha(conn, row["source_sha"]):
                raise NotFound("image", sha)
        self._check_expired(conn, row, "image", sha)
        if thumb:
            return self._send_cached(row["thumb"] or b"", "image/jpeg",
                                     (row["blob_sha"] or "")[:32] + "-t")
        data = self.server.blurd_store.get(row["blob_path"])
        return self._send_cached(data, row["mime"], row["blob_sha"], {
            "Content-Disposition": f'inline; filename="{row["source_sha"][:16]}.jpg"',
            "X-Blurd-Source-Sha": row["source_sha"],
        })

    # -- dashboard ------------------------------------------------------------
    def _ui(self, method, path):
        srv = self.server
        password = srv.blurd_cfg.get("dashboard_password")
        if not password:
            return self._send(503, {
                "ok": False,
                "error": {"code": 87, "type": "dashboard_disabled",
                          "message": "Dashboard password is not set",
                          "suggestions": ["Run: blurd dashboard-password <password>"]}})
        if not auth.check_basic(self.headers, srv.blurd_cfg.get("dashboard_user", "admin"),
                                password):
            return self._send(401, {"ok": False, "error": {
                "code": 107, "type": "auth_failed",
                "message": "Dashboard authentication required"}},
                extra={"WWW-Authenticate": 'Basic realm="blurd dashboard"'})

        # Authenticated browser calls to the read API, so the dashboard does not
        # need an API key embedded in its JavaScript.
        if path.startswith("/ui-api/"):
            return self._ui_api(path)

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (UI_DIR / rel).resolve()
        if not str(target).startswith(str(UI_DIR.resolve())) or not target.is_file():
            raise NotFound("file", rel)
        ctypes = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                  ".css": "text/css", ".svg": "image/svg+xml"}
        extra = {}
        if rel == "index.html" and not auth.csrf_from_cookie(self.headers):
            # SameSite=Strict means this never accompanies a cross-site request,
            # which is what actually stops CSRF; the header echo is the belt to
            # that pair of braces.
            extra["Set-Cookie"] = (f"{auth.CSRF_COOKIE}={auth.new_csrf()}; "
                                   "Path=/; SameSite=Strict")
        return self._send(200, target.read_bytes(),
                          ctypes.get(target.suffix, "application/octet-stream"),
                          extra)

    def _ui_api(self, path):
        srv = self.server
        client = srv.blurd_client
        cfg = srv.blurd_cfg
        conn = db.connect(cfg.db_file)
        q = self._query()
        sub = path[len("/ui-api"):]

        # Any state-changing dashboard call must carry the token. Until now the
        # only thing standing between a hostile page and an image delete was
        # the CORS preflight a cross-origin DELETE happens to trigger -- an
        # accident, not a defence, and one that evaporates the moment a POST
        # with a simple content type is added. Which is exactly what key
        # creation is.
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            if not auth.check_csrf(self.headers):
                raise AuthFailed(
                    "Missing or invalid CSRF token",
                    {"expected_header": auth.CSRF_HEADER},
                    )

        if sub.rstrip("/") == "/keys":
            return self._ui_keys(client, conn, cfg)
        m = re.match(r"^/keys/(?P<kid>[0-9a-f]{8,32})$", sub)
        if m and self.command == "DELETE":
            data = auth.revoke(conn, m.group("kid"), actor="dashboard",
                               source_ip=self.address_string())
            return self._send(200, {"ok": True, "data": data})
        if sub.rstrip("/") == "/audit":
            return self._send(200, {"ok": True, "data": db.audit_list(
                conn, min(int(self._one(q, "limit", 50) or 50), 200))})
        if sub.rstrip("/") == "/images":
            return self._send(200, {"ok": True, "data": client.list(**_filters(q))})
        if sub.rstrip("/") == "/stats":
            return self._send(200, {"ok": True, "data": client.stats()})
        if sub.rstrip("/") == "/facets":
            # The dashboard lists unscoped, so its chips list every tenant's
            # tags/metadata -- a scoped caller never reaches this endpoint.
            return self._send(200, {"ok": True, "data": db.facets(
                conn, None, limit=100)})
        if sub.rstrip("/") == "/jobs":
            return self._send(200, {"ok": True, "data": client.jobs(**_job_filters(q))})
        m = re.match(r"^/images/(?P<sha>[0-9a-f]{6,64})$", sub)
        if m:
            if self.command == "DELETE":
                db.audit(conn, "dashboard", "image.delete", m.group("sha"),
                         self.address_string())
                return self._send(200, {"ok": True, "data": client.delete(m.group("sha"))})
            return self._send(200, {"ok": True, "data": client.get(
                m.group("sha"), self._one(q, "profile"))})
        m = re.match(r"^/(blobs|thumbs)/(?P<sha>[0-9a-f]{6,64})$", sub)
        if m:
            return self._serve_image(client, conn, m.group("sha"),
                                     self._one(q, "profile"),
                                     thumb=sub.startswith("/thumbs"))
        raise NotFound("endpoint", sub)


def _db_kind() -> str:
    from . import db as _db
    return _db.dialect().name


def _truthy(v):
    return str(v).lower() in ("1", "true", "yes", "on")


def _filters(q):
    one = lambda k, d=None: (q.get(k) or [d])[0]
    meta = {k[5:]: v[0] for k, v in q.items() if k.startswith("meta.")}
    nr = one("needs_review")
    return {
        "tag": q.get("tag") or [],
        "meta": meta,
        "sha": one("sha"),
        "code": one("code"),
        "profile_hash": one("profile_hash"),
        "needs_review": None if nr is None else _truthy(nr),
        "since": one("since"),
        "until": one("until"),
        "sort": one("sort", "created"),
        "direction": one("direction", "desc"),
        "cursor": one("cursor"),
        "limit": min(int(one("limit", 50) or 50), 200),
        "offset": int(one("offset", 0) or 0),
    }


def _job_filters(q):
    one = lambda k, d=None: (q.get(k) or [d])[0]
    return {
        "status": one("status"), "code": one("code"),
        "since": one("since"), "until": one("until"),
        "sort": one("sort", "created"), "direction": one("direction", "desc"),
        "cursor": one("cursor"),
        "limit": min(int(one("limit", 50) or 50), 200),
        "offset": int(one("offset", 0) or 0),
    }


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A thread pool, not a thread per connection.

    `ThreadingHTTPServer` spawns a thread per connection and never bounds them.
    With SQLite that is merely wasteful. With Postgres each of those threads
    opens its own connection, so a burst of clients walks straight into
    `max_connections` and takes the database out for every replica at once --
    which is a much worse failure than queueing.

    The pool caps concurrent connections at `max_threads`, and therefore caps
    database connections at roughly `max_threads + workers + 1`.

    Keep-alive is deliberately disabled (`protocol_version = HTTP/1.0`): with a
    bounded pool, idle keep-alive connections would hold threads and starve new
    requests. The cost is a TCP handshake per request, which is noise next to a
    130 ms redaction, and irrelevant behind the caching proxy the read path
    wants anyway.
    """

    daemon_threads = True
    allow_reuse_address = True
    max_threads = 32

    def __init__(self, *args, max_threads: int = None, **kwargs):
        if max_threads:
            self.max_threads = int(max_threads)
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_threads, thread_name_prefix="blurd-http")
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # Submit instead of spawning. socketserver's threading mixin would
        # start an unbounded thread here.
        self._pool.submit(self._handle, request, client_address)

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self):
        super().server_close()
        self._pool.shutdown(wait=False)


class BlurdServer(BoundedThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, addr, cfg: Config, log=None):
        from . import resources
        workers = resources.effective_workers(cfg)
        super().__init__(addr, Handler,
                         max_threads=resources.effective_http_threads(cfg, workers))
        self.blurd_cfg = cfg
        # Schema first. The queue's crash-recovery pass queries `jobs` before
        # anything else touches the database, so on a brand-new home -- which is
        # every fresh container -- `serve` used to die with "no such table".
        # Prior runs only worked because some CLI command had initialised it.
        cfg.ensure_dirs()
        db.dialect(cfg)               # bind the backend before any query runs
        db.init(cfg.db_file)
        from . import store as _store
        # Built once and shared: an S3 store is a handful of immutable fields,
        # and rebuilding it per request would re-read config on every tile.
        self.blurd_store = _store.build(cfg)
        from . import detect as _detect
        _detect.ORT_THREADS[0] = int(cfg.get("ort_threads", 1) or 1)
        _detect.ARENA[0] = str(cfg.get("ort_arena", True)).lower() not in (
            "0", "false", "no", "off")
        # Identity first: every job this instance accepts is stamped with it,
        # which is what lets a peer tell "running elsewhere" from "orphaned".
        self._register_instance()
        self.blurd_queue = jobs_mod.JobQueue(cfg, instance_id=self.blurd_instance_id)
        self.blurd_queue_info = self.blurd_queue.start()
        self.blurd_client = LocalClient(cfg, queue=self.blurd_queue)
        self.blurd_log = log or (lambda m: None)
        self.blurd_feedback_hits = {}   # per-IP rate window for /v1/feedback

    def _register_instance(self):
        """Claim a slot, and refuse to share a SQLite file with a peer.

        SQLite is a single-writer file: a second process against the same home
        is silent corruption, not a slower mode. Once the metadata store is a
        real service this check becomes a no-op and the same heartbeat carries
        job leases instead (see spec/distributed.md)."""
        import secrets, socket, threading as _t
        from . import db as _db
        self.blurd_instance_id = f"i_{socket.gethostname()[:20]}_{secrets.token_hex(4)}"
        conn = _db.connect(self.blurd_cfg.db_file)
        shareable = _db.dialect().shareable
        peers = _db.live_instances(conn, exclude=self.blurd_instance_id)
        if peers and not shareable:
            raise Conflict(
                f"Another blurd instance is already using this SQLite home "
                f"({peers[0]['host']} pid {peers[0]['pid']})",
                {"home": str(self.blurd_cfg.home), "peers": len(peers),
                 "why": "SQLite is a single-writer file; two processes sharing "
                        "one corrupt it silently",
                 "fix": "run one replica, give each its own BLURD_HOME, or move "
                        "to a shared metadata store (spec/distributed.md)"})
        _db.register_instance(conn, self.blurd_instance_id, __version__,
                              self.blurd_store.kind)

        stop = self._hb_stop = _t.Event()

        def beat():
            while not stop.wait(15):
                try:
                    _db.heartbeat_instance(_db.connect(self.blurd_cfg.db_file),
                                           self.blurd_instance_id)
                except Exception:
                    pass          # a missed beat must never kill the server

        t = _t.Thread(target=beat, name="blurd-heartbeat", daemon=True)
        t.start()

    def shutdown(self, drain_seconds: float = 0.0):
        # Stop accepting first, then let decoded images finish rather than
        # failing them and making the producer resubmit.
        self.blurd_queue.stop(drain_seconds)
        try:
            self._hb_stop.set()
            from . import db as _db
            _db.unregister_instance(_db.connect(self.blurd_cfg.db_file),
                                    self.blurd_instance_id)
        except Exception:
            pass          # a stale row expires on its own after 45s
        super().shutdown()


def serve(cfg: Config, host: str, port: int, log=None):
    srv = BlurdServer((host, port), cfg, log=log)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
