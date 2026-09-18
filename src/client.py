"""Transport-agnostic client.

Every CLI command talks to one of these. LocalClient runs the pipeline in this
process against the local SQLite; RemoteClient makes the same calls over HTTP
against a blurd daemon. `blurd blur x.jpg` and
`blurd --remote https://host --api-key ... blur x.jpg` therefore run the exact
same command code -- which is also what keeps the CLI honest as the contract
a Go/machin port has to satisfy.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import auth, db, jobs, pipeline, store as _store
from .scope import Scope
from .config import Config
from .errors import BlurdError, Internal, NotFound, Upstream


class LocalClient:
    def __init__(self, cfg: Config, queue: "jobs.JobQueue" = None):
        self.cfg = cfg
        self.cfg.ensure_dirs()
        db.dialect(cfg)               # bind the backend before any query runs
        db.init(self.cfg.db_file)
        # A CLI invocation gets its own short-lived queue; the daemon passes in
        # the long-lived one so its workers and recovery are shared.
        self._owns_queue = queue is None
        self.queue = queue or jobs.JobQueue(cfg)
        if self._owns_queue:
            self.queue.start()

    # -- write ----------------------------------------------------------------
    def submit(self, *, url=None, path=None, data=None, code=None, tags=None,
               metadata=None, overrides=None, force=False, on_conflict="reuse",
               wait: float = 0.0, scope: Scope = None) -> Dict[str, Any]:
        """Always creates a job. wait>0 blocks for up to that many seconds,
        which is what makes `blurd blur x.jpg` feel synchronous while the API
        underneath stays asynchronous."""
        job = self.queue.submit(url=url, path=path, data=data, external_id=code,
                                tags=tags, metadata=metadata, overrides=overrides,
                                force=force, on_conflict=on_conflict, scope=scope)
        if wait and job["status"] in ("queued", "running"):
            job = self.queue.wait(job["job_id"], wait, scope)
        return job

    def job(self, job_id: str, wait: float = 0.0, scope: Scope = None) -> Dict[str, Any]:
        return (self.queue.wait(job_id, wait, scope) if wait
                else self.queue.get(job_id, scope))

    def jobs(self, status=None, code=None, limit=50, offset=0, scope: Scope = None,
             since=None, until=None, sort="created", direction="desc",
             cursor=None) -> Dict[str, Any]:
        conn = db.connect(self.cfg.db_file)
        tenant = _tenant(scope)
        res = db.query_jobs(conn, status=status, code=code, since=since, until=until,
                            tenant=tenant, sort=sort, direction=direction,
                            cursor=cursor, limit=limit, offset=offset)
        return {"total": res["total"], "total_capped": res["total_capped"],
                "count": len(res["rows"]),
                "items": [jobs.job_dict(self.cfg, conn, r, tenant, light=True)
                          for r in res["rows"]],
                "next_cursor": res["next_cursor"], "has_more": res["has_more"],
                "sort": res["sort"], "direction": res["direction"],
                "queue": self.queue.depth(scope)}

    def by_code(self, code: str, profile: str = None, scope: Scope = None,
                tenant: str = None) -> Dict[str, Any]:
        """The consumer hot path: one indexed lookup, no scan. The tenant is
        part of the primary key, so two apps may both use 'IMG_0042.jpg'."""
        conn = db.connect(self.cfg.db_file)
        own = _tenant(scope)
        sha = jobs.resolve_code(conn, code, None, tenant or own or "global",
                                any_tenant=(own is None and not tenant))
        if sha is None:
            raise NotFound("external_id", code)
        return self.get(sha, profile, scope)

    # -- read -----------------------------------------------------------------
    def get(self, sha: str, profile: Optional[str] = None,
            scope: Scope = None) -> Dict[str, Any]:
        conn = db.connect(self.cfg.db_file)
        if profile:
            row = db.find_artifact(conn, sha, profile)
        else:
            row = db.find_artifact_by_sha_prefix(conn, sha)
        if not row:
            raise NotFound("image", sha)
        # Out-of-scope reads are a 404, not a 403: a scoped key must not be
        # able to confirm that a given sha exists on the instance.
        if scope is not None and not scope.is_global:
            if not scope.allows_sha(conn, row["source_sha"]):
                raise NotFound("image", sha)
        return pipeline.artifact_result(self.cfg, conn, row, cached=True,
                                        tenant=_tenant(scope))

    def list(self, scope: Scope = None, **filters) -> Dict[str, Any]:
        conn = db.connect(self.cfg.db_file)
        res = db.query_artifacts(conn, scope=scope, **filters)
        shas = [r["source_sha"] for r in res["rows"]]
        labels = db.bulk_labels(conn, shas, _tenant(scope))   # 3 queries, not 3N
        items = []
        for r in res["rows"]:
            lab = labels.get(r["source_sha"], {"tags": [], "metadata": {}, "codes": []})
            items.append({
                "source_sha": r["source_sha"], "profile_hash": r["profile_hash"],
                "created_at": r["created_at"], "n_faces": r["n_faces"],
                "n_plates": r["n_plates"], "needs_review": bool(r["needs_review"]),
                "width": r["width"], "height": r["height"],
                "bytes": r["blob_size"], "mime": r["mime"],
                "source_kind": r["source_kind"], "source_ref": r["source_ref"],
                "codes": lab["codes"],
                "tags": lab["tags"],
                "metadata": lab["metadata"],
                "blob_url": f"/v1/blobs/{r['source_sha']}?profile={r['profile_hash']}",
                "thumb_url": f"/v1/thumbs/{r['source_sha']}?profile={r['profile_hash']}",
            })
        return {"total": res["total"], "total_capped": res.get("total_capped", False),
                "count": len(items), "items": items,
                "next_cursor": res.get("next_cursor"),
                "has_more": res.get("has_more", False),
                "sort": res.get("sort"), "direction": res.get("direction")}

    def blob_by_code(self, code: str, profile: Optional[str] = None,
                     scope: Scope = None, tenant: str = None) -> bytes:
        conn = db.connect(self.cfg.db_file)
        own = _tenant(scope)
        sha = jobs.resolve_code(conn, code, None, tenant or own or "global",
                                any_tenant=(own is None and not tenant))
        if sha is None:
            raise NotFound("external_id", code)
        return self.blob(sha, profile, scope)

    def blob(self, sha: str, profile: Optional[str] = None,
             scope: Scope = None) -> bytes:
        rec = self.get(sha, profile, scope)
        return _store.build(self.cfg).get(rec["blob"]["key"])

    def stats(self, scope: Scope = None) -> Dict[str, Any]:
        conn = db.connect(self.cfg.db_file)
        s = db.stats(conn, scope)
        s["queue"] = self.queue.depth(scope)
        s["home"] = str(self.cfg.home)
        s["profile_hash"] = Config.hash_of(self.cfg.resolve_profile())
        return s

    def delete(self, sha: str, scope: Scope = None) -> Dict[str, Any]:
        """An unrestricted delete removes the image outright. A scoped delete
        removes only that tenant's labels and codes -- the bytes may be
        referenced by another tenant, and one app must not be able to destroy
        another's data by deleting 'its' copy. The image goes only once nobody
        references it."""
        from pathlib import Path
        conn = db.connect(self.cfg.db_file)
        paths = db.artifact_blob_paths(conn, sha)
        if not paths:
            raise NotFound("image", sha)

        if scope is not None and not scope.is_global:
            if not scope.allows_sha(conn, sha):
                raise NotFound("image", sha)
            tenant = scope.tenant
            db.release_tenant_labels(conn, sha, tenant)
            if db.has_any_labels(conn, sha):
                return {"source_sha": sha, "deleted": False,
                        "released": True, "scope": scope.describe(),
                        "note": "labels removed; the image is still referenced "
                                "by another tenant"}

        blobs = _store.build(self.cfg)
        for rel in paths:
            blobs.delete(rel)
        db.delete_image(conn, sha)
        return {"source_sha": sha, "deleted": True, "artifacts": len(paths)}

    # -- keys -----------------------------------------------------------------
    def keys_add(self, name, scope: Scope = None, key: str = None):
        return auth.generate(db.connect(self.cfg.db_file), name, scope=scope,
                             key=key)
    def keys_list(self): return auth.listing(db.connect(self.cfg.db_file))
    def keys_revoke(self, kid): return auth.revoke(db.connect(self.cfg.db_file), kid)
    def keys_export(self): return auth.export_keys(db.connect(self.cfg.db_file))
    def keys_import(self, payload):
        return auth.import_keys(db.connect(self.cfg.db_file), payload)

    # -- feedback -------------------------------------------------------------
    def feedback_submit(self, body):
        conn = db.connect(self.cfg.db_file)
        fb_id = body["id"]
        db.insert_feedback(
            conn, fb_id, body.get("kind", "note"), body["message"],
            body.get("context", ""), body.get("reporter", ""),
            body.get("version", ""), "cli")
        return {"id": fb_id, "stored": True}


class RemoteClient:
    """Same surface, over HTTP. Errors from the daemon are re-raised as the
    same BlurdError types, so exit codes are identical local or remote."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, method: str, path: str, *, body=None, raw=None,
              content_type=None, query=None, expect_json=True, timeout=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else None)
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type or "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            try:
                err = json.loads(detail)["error"]
                raise BlurdError(err["code"], err["type"], err["message"],
                                 err.get("details"), err.get("recoverable", False),
                                 err.get("retry_after"), err.get("suggestions"))
            except (ValueError, KeyError):
                raise Upstream(f"HTTP {exc.code} from {url}",
                               {"body": detail[:400].decode("utf-8", "replace")})
        except urllib.error.URLError as exc:
            raise Upstream(f"Cannot reach blurd daemon at {self.base}: {exc.reason}",
                           {"url": url})
        if not expect_json:
            return payload
        try:
            body = json.loads(payload)
        except ValueError:
            raise Internal("Daemon returned a non-JSON response", {"url": url})
        # Unwrap the API envelope so RemoteClient returns exactly what
        # LocalClient returns -- otherwise the two transports are not actually
        # interchangeable and every command has to know which one it got.
        if isinstance(body, dict) and "data" in body and body.get("ok") is True:
            return body["data"]
        return body

    def submit(self, *, url=None, path=None, data=None, code=None, tags=None,
               metadata=None, overrides=None, force=False, on_conflict="reuse",
               wait: float = 0.0):
        q = {"on_conflict": on_conflict}
        if force:
            q["force"] = "1"
        if wait:
            q["wait"] = str(wait)
        if path:
            with open(path, "rb") as fh:
                data = fh.read()
        if data is not None:
            if code:
                q["code"] = code
            if tags:
                q["tags"] = ",".join(tags)
            if metadata:
                q["metadata"] = json.dumps(metadata)
            if overrides:
                q["profile"] = json.dumps(overrides)
            return self._call("POST", "/v1/images", raw=data,
                              content_type="application/octet-stream", query=q,
                              timeout=self.timeout + (wait or 0))
        return self._call("POST", "/v1/images", query=q, body={
            "url": url, "external_id": code, "tags": tags or [],
            "metadata": metadata or {}, "profile": overrides or {}},
            timeout=self.timeout + (wait or 0))

    def job(self, job_id, wait: float = 0.0):
        return self._call("GET", f"/v1/jobs/{job_id}",
                          query={"wait": str(wait)} if wait else None,
                          timeout=self.timeout + (wait or 0))

    def jobs(self, status=None, code=None, limit=50, offset=0):
        q = {"limit": limit, "offset": offset}
        if status:
            q["status"] = status
        if code:
            q["code"] = code
        return self._call("GET", "/v1/jobs", query=q)

    def by_code(self, code, profile=None):
        return self._call("GET", f"/v1/images/by-code/{urllib.parse.quote(code, safe='')}",
                          query={"profile": profile} if profile else None)

    def blob_by_code(self, code, profile=None):
        return self._call("GET", f"/v1/blobs/by-code/{urllib.parse.quote(code, safe='')}",
                          query={"profile": profile} if profile else None,
                          expect_json=False)

    def get(self, sha, profile=None):
        return self._call("GET", f"/v1/images/{sha}",
                          query={"profile": profile} if profile else None)

    def list(self, **filters):
        q = {}
        for k in ("limit", "offset", "sha", "profile_hash", "since", "until", "code"):
            if filters.get(k) is not None:
                q[k] = filters[k]
        if filters.get("needs_review") is not None:
            q["needs_review"] = "1" if filters["needs_review"] else "0"
        if filters.get("tag"):
            q["tag"] = filters["tag"]
        for mk, mv in (filters.get("meta") or {}).items():
            q[f"meta.{mk}"] = mv
        return self._call("GET", "/v1/images", query=q)

    def blob(self, sha, profile=None):
        return self._call("GET", f"/v1/blobs/{sha}",
                          query={"profile": profile} if profile else None,
                          expect_json=False)

    def stats(self):
        return self._call("GET", "/v1/stats")

    def delete(self, sha):
        return self._call("DELETE", f"/v1/images/{sha}")

    def feedback_submit(self, body):
        # POST /v1/feedback is open by spec -- no key required.
        return self._call("POST", "/v1/feedback", body=body)

    def _keys_unsupported(self, *a, **k):
        raise BlurdError(85, "unsupported_remote",
                         "API keys are managed on the daemon host, not remotely",
                         suggestions=["Run `blurd keys` on the daemon machine"])
    keys_add = keys_list = keys_revoke = keys_export = keys_import = \
        _keys_unsupported


def _tenant(scope: Optional[Scope]) -> Optional[str]:
    return None if scope is None or scope.is_global else scope.tenant


def build(cfg: Config, remote: Optional[str], api_key: Optional[str]):
    if remote:
        if not api_key:
            from .errors import AuthFailed
            raise AuthFailed("--remote requires an API key",
                             {"remote": remote})
        return RemoteClient(remote, api_key)
    return LocalClient(cfg)
