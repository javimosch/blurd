"""Blob storage.

Redacted images live outside the metadata store: a few thousand JPEGs would
bloat the database, slow every backup, and make "just copy the blobs"
impossible. The key is the same in every backend:

    <sha[0:2]>/<sha[2:4]>/<sha256>-<profile_hash>.jpg

Two backends, selected by `storage.backend`:

    local  filesystem under <BLURD_HOME>/blobs   -- dev, single container
    s3     any S3-compatible object store        -- Docker/k8s, several replicas

Why this is the seam that matters: blobs are ~95% of the stored bytes (333 kB
per image against ~19 kB of metadata). Moving them to object storage takes the
stateful footprint of an instance from ~360 GB per million images to ~19 GB --
the difference between a volume you have to think about and one you do not. It
is also what lets more than one replica serve the same data.

The S3 client is ~130 lines of SigV4 over urllib rather than boto3. Only
GET/PUT/DELETE/HEAD on single objects are needed, boto3 plus botocore adds tens
of megabytes to an image whose whole point is to be small, and the signing is
exercised against a real MinIO in the test suite rather than mocked.
"""

import datetime
import hashlib
import hmac
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4
from typing import Optional

from .errors import Internal, NotFound, Upstream, ValidationError

_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


def rel_path(sha: str, profile_hash: str, ext: str) -> str:
    """The storage key. Identical across backends, so switching one for the
    other never rewrites `artifacts.blob_path`."""
    return f"{sha[:2]}/{sha[2:4]}/{sha}-{profile_hash}{ext}"


# --- backends ----------------------------------------------------------------

class LocalStore:
    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def describe(self) -> dict:
        return {"backend": "local", "root": str(self.root)}

    def path_for(self, rel: str) -> Optional[Path]:
        """Only the local backend can offer a filesystem path. Callers must
        treat `None` as 'stream it instead'."""
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def get(self, rel: str) -> bytes:
        p = self.root / rel
        if not p.exists():
            raise NotFound("blob", rel)
        return p.read_bytes()

    def put(self, rel: str, data: bytes, content_type: str = "image/jpeg") -> None:
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name per writer: concurrent jobs racing the same
        # (source_sha, profile_hash) share `dest`, and a shared .part name
        # made the second rename fail ENOENT after the first moved it.
        tmp = dest.with_suffix(
            f"{dest.suffix}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.part")
        tmp.write_bytes(data)
        tmp.replace(dest)      # atomic: readers never see a half-written blob

    def delete(self, rel: str) -> None:
        (self.root / rel).unlink(missing_ok=True)

    def check(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / ".blurd-write-probe"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return {"ok": True, **self.describe()}


class S3Store:
    kind = "s3"

    def __init__(self, *, endpoint: str, bucket: str, access_key: str,
                 secret_key: str, region: str = "us-east-1", prefix: str = "",
                 timeout: float = 20.0, path_style: bool = True):
        if not endpoint or not bucket:
            raise ValidationError(
                "S3 storage needs an endpoint and a bucket",
                suggestions=["blurd config set storage.s3.endpoint http://minio:9000",
                             "blurd config set storage.s3.bucket blurd"])
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "us-east-1"
        self.prefix = (prefix or "").strip("/")
        self.timeout = timeout
        # Path style (endpoint/bucket/key) is what MinIO and most self-hosted
        # gateways expect; virtual-host style needs bucket-specific DNS.
        self.path_style = path_style

    def describe(self) -> dict:
        return {"backend": "s3", "endpoint": self.endpoint, "bucket": self.bucket,
                "prefix": self.prefix or None, "region": self.region}

    def path_for(self, rel: str) -> Optional[Path]:
        return None            # object storage has no filesystem path

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def _url(self, key: str):
        parts = urllib.parse.urlsplit(self.endpoint)
        quoted = "/".join(_quote(seg) for seg in key.split("/"))
        if self.path_style:
            return f"{parts.scheme}://{parts.netloc}/{self.bucket}/{quoted}", \
                   parts.netloc, f"/{self.bucket}/{quoted}"
        host = f"{self.bucket}.{parts.netloc}"
        return f"{parts.scheme}://{host}/{quoted}", host, f"/{quoted}"

    def _request(self, method: str, key: str, body: bytes = None,
                 content_type: str = None):
        url, host, path = self._url(key)
        body = body or b""
        headers = _sign(method=method, host=host, path=path, body=body,
                        access_key=self.access_key, secret_key=self.secret_key,
                        region=self.region, content_type=content_type)
        req = urllib.request.Request(url, data=body if body else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise Upstream(f"Object storage unreachable: {exc.reason}",
                           {"endpoint": self.endpoint, "bucket": self.bucket})

    def exists(self, rel: str) -> bool:
        status, _ = self._request("HEAD", self._key(rel))
        return status == 200

    def get(self, rel: str) -> bytes:
        status, body = self._request("GET", self._key(rel))
        if status == 404:
            raise NotFound("blob", rel)
        if status != 200:
            raise Upstream(
                f"Object storage returned {status} on GET",
                {"key": self._key(rel),
                 "body": body[:200].decode("utf-8", "replace")})
        return body

    def put(self, rel: str, data: bytes, content_type: str = "image/jpeg") -> None:
        status, body = self._request("PUT", self._key(rel), data, content_type)
        if status not in (200, 201):
            raise Upstream(
                f"Object storage returned {status} on PUT",
                {"key": self._key(rel),
                 "body": body[:200].decode("utf-8", "replace")})

    def delete(self, rel: str) -> None:
        status, _ = self._request("DELETE", self._key(rel))
        if status not in (200, 204, 404):
            raise Upstream(f"Object storage returned {status} on DELETE",
                           {"key": self._key(rel)})

    def check(self) -> dict:
        """Round-trip a probe object. A bucket that reads but cannot be written
        is a failure worth catching at startup, not on the first image."""
        probe = ".blurd-write-probe"
        status, body = self._request("PUT", self._key(probe), b"ok", "text/plain")
        if status == 404:
            # A bare "404 on PUT" sends people looking at the wrong thing.
            raise Upstream(
                f"Bucket '{self.bucket}' does not exist at {self.endpoint}",
                {"bucket": self.bucket, "endpoint": self.endpoint},
                retry_after=None)
        if status == 403:
            raise Upstream(
                f"Access denied writing to '{self.bucket}' -- check the credentials",
                {"bucket": self.bucket,
                 "hint": "BLURD_S3_ACCESS_KEY / BLURD_S3_SECRET_KEY"},
                retry_after=None)
        if status not in (200, 201):
            raise Upstream(f"Object storage returned {status} on the write probe",
                           {"body": body[:200].decode("utf-8", "replace")})
        got = self.get(probe)
        self.delete(probe)
        if got != b"ok":
            raise Internal("Object storage round-trip returned unexpected bytes")
        return {"ok": True, **self.describe()}


# --- SigV4 --------------------------------------------------------------------

def _quote(value: str) -> str:
    return "".join(c if c in _UNRESERVED else "".join(f"%{b:02X}" for b in c.encode())
                   for c in value)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sign(*, method: str, host: str, path: str, body: bytes, access_key: str,
          secret_key: str, region: str, content_type: str = None,
          service: str = "s3") -> dict:
    """AWS Signature Version 4, the subset needed for single-object requests.

    No query parameters are ever signed here, because blurd only ever addresses
    whole objects. If a caller adds one it must also go into the canonical query
    string, or the signature silently stops matching.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = _sha256(body)

    headers = {"host": host, "x-amz-content-sha256": payload_hash,
               "x-amz-date": amz_date}
    if content_type:
        headers["content-type"] = content_type

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        [method, path, "", canonical_headers, signed_headers, payload_hash])

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         _sha256(canonical_request.encode())])

    k_date = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, to_sign.encode(), hashlib.sha256).hexdigest()

    out = dict(headers)
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    return out


# --- construction -------------------------------------------------------------

def build(cfg):
    """The one place a backend is chosen. Everything else is handed a store."""
    # Env overrides are applied by Config (see config.ENV_OVERRIDES), so this
    # only has to read the resolved value.
    backend = (cfg.get("storage.backend", "local") or "local").lower()
    if backend == "local":
        return LocalStore(cfg.blobs_dir)
    if backend == "s3":
        s3 = cfg.get("storage.s3", {}) or {}
        return S3Store(
            endpoint=os.environ.get("BLURD_S3_ENDPOINT") or s3.get("endpoint", ""),
            bucket=os.environ.get("BLURD_S3_BUCKET") or s3.get("bucket", ""),
            # Credentials prefer the environment: a config file on a volume is a
            # worse place for a secret than a k8s Secret mounted as an env var.
            access_key=os.environ.get("BLURD_S3_ACCESS_KEY") or s3.get("access_key", ""),
            secret_key=os.environ.get("BLURD_S3_SECRET_KEY") or s3.get("secret_key", ""),
            region=os.environ.get("BLURD_S3_REGION") or s3.get("region", "us-east-1"),
            prefix=os.environ.get("BLURD_S3_PREFIX") or s3.get("prefix", ""),
            path_style=bool(s3.get("path_style", True)),
        )
    raise ValidationError(f"Unknown storage backend '{backend}'",
                          {"known": ["local", "s3"]},
                          ["blurd config set storage.backend local|s3"])
