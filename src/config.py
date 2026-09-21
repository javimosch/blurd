"""Configuration: home dir layout, defaults, and the processing profile."""

import json
import os
from pathlib import Path
from typing import Any, Dict

from .canonical import profile_hash
from .errors import ValidationError

DEFAULT_HOME = Path(os.environ.get("BLURD_HOME", Path.home() / ".blurd"))

# The processing profile. Everything here feeds profile_hash, so any change
# invalidates cached artifacts by design. Keep it small and explicit.
DEFAULT_PROFILE: Dict[str, Any] = {
    "version": 1,
    "detect": {
        "face": {"model": "yunet-2023mar", "min_score": 0.6},
        "plate": {"model": "yolov9t-512-plates", "min_score": 0.35},
        "max_side": 1280,          # long edge used for detection, boxes scale back
    },
    "redact": {
        "mode": "pixelate",        # pixelate | blur | solid
        "strength": 0.06,          # mosaic block size as a fraction of box size
        "expand": 0.18,            # grow boxes 18%: detectors crop tight
        "shape": {"face": "ellipse", "plate": "rect"},
    },
    "output": {"format": "jpeg", "quality": 90, "max_side": 0},  # 0 = keep size
}

DEFAULTS = {
    "port": 8770,
    "host": "127.0.0.1",
    "dashboard_user": "admin",
    "dashboard_password": None,        # set via `blurd dashboard-password`
    # Key CREATION from the dashboard is off by default and deliberately so:
    # the dashboard password is one shared, human-typed secret, and a minted
    # API key outlives it. Turning this on makes that password the root of
    # trust for machine access, so it also demands a second secret.
    # Enable with: blurd dashboard-keys enable --secret <s>
    "dashboard_allow_key_creation": False,
    "dashboard_key_secret": None,
    "fetch": {
        "timeout_s": 15,
        "max_bytes": 25 * 1024 * 1024,
        "max_pixels": 50_000_000,      # decompression-bomb guard
        "allow_private_ips": False,    # SSRF guard; opt-in for local testing
        "max_redirects": 3,
        "user_agent": "blurd/0.1",
    },
    # Two supported profiles, not a matrix of options:
    #   local -> filesystem + SQLite   (dev, single container)
    #   s3    -> object store + SQLite (Docker/k8s; shared metadata store next)
    # Metadata store. `sqlite` is the dev / single-container profile; the
    # `postgres` backend is what several replicas share (spec/distributed.md).
    "db": {"backend": "sqlite", "dsn": None, "database": "blurd"},
    "storage": {
        "backend": "local",
        "s3": {"endpoint": None, "bucket": None, "region": "us-east-1",
               "prefix": "", "path_style": True},
    },
    # Seconds to let in-flight jobs finish after SIGTERM. Should sit inside
    # the orchestrator's grace period (k8s terminationGracePeriodSeconds).
    "drain_seconds": 20,
    # Caps concurrent HTTP threads, and therefore database connections:
    # roughly http_threads + workers + 1 per replica. Size it against the
    # server's max_connections divided by the replica count.
    "http_threads": "auto",
    # "auto" sizes workers from the memory AND cpu actually available to this
    # process -- cgroup limits included. Deriving from os.cpu_count() alone
    # meant a 512 MiB container on a 32-core host started 31 workers and was
    # OOM-killed mid-job. See src/resources.py.
    "workers": "auto",
    # Two queue bounds. `queue_max` caps the number of queued jobs;
    # `queue_max_bytes` caps the memory the queued UPLOADS are holding, which
    # is the bound that keeps a small box alive -- blurd never spools source
    # bytes to disk, so a queued upload is resident memory. `auto` derives it
    # from what is left after the workers are paid for (src/resources.py).
    "queue_max": 1000,
    "queue_max_bytes": "auto",
    # Return freed memory to the OS between jobs. See resources.malloc_trim --
    # this is not MALLOC_TRIM_THRESHOLD_, which costs throughput.
    "malloc_trim": True,
    # Threads ONNX Runtime may use inside a single inference. Workers already
    # provide parallelism, so >1 here oversubscribes the CPU: N workers x M
    # intra-op threads compete for the same cores. 1 is right for throughput,
    # higher only helps single-image latency on an idle box.
    "ort_threads": 1,
    # onnxruntime's CPU memory arena caches freed tensors for reuse. Measured at
    # +40 MB per worker for no reliable throughput gain on this pipeline, so it
    # is off by default: blurd targets small VMs. See spec/resources.md.
    "ort_arena": False,
    "profile": DEFAULT_PROFILE,
}


class Config:
    def __init__(self, home: Path = None):
        self.home = Path(home or DEFAULT_HOME)
        self.config_file = self.home / "config.json"
        self.db_file = self.home / "blurd.db"
        self.blobs_dir = self.home / "blobs"
        self.models_dir = self.home / "models"
        self.pid_file = self.home / "blurd.pid"
        self.log_file = self.home / "blurd.log"
        self._data = None

    def ensure_dirs(self) -> None:
        for d in (self.home, self.blobs_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def data(self) -> Dict[str, Any]:
        if self._data is None:
            self._data = dict(DEFAULTS)
            if self.config_file.exists():
                try:
                    self._data = _deep_merge(self._data, json.loads(self.config_file.read_text()))
                except (OSError, ValueError):
                    pass
            self._data = _apply_env(self._data)
        return self._data

    def get(self, key: str, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        self.ensure_dirs()
        raw = {}
        if self.config_file.exists():
            try:
                raw = json.loads(self.config_file.read_text())
            except ValueError:
                raw = {}
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.config_file.write_text(json.dumps(raw, indent=2) + "\n")
        self._data = None

    @property
    def profile(self) -> Dict[str, Any]:
        return self.get("profile", DEFAULT_PROFILE)

    def resolve_profile(self, overrides: Dict[str, Any] = None) -> Dict[str, Any]:
        """Profile for one request: config profile + per-request overrides."""
        prof = _deep_merge(json.loads(json.dumps(self.profile)), overrides or {})
        # Round thresholds so float formatting can never split the cache.
        for cls in ("face", "plate"):
            if cls in prof.get("detect", {}):
                prof["detect"][cls]["min_score"] = round(float(prof["detect"][cls]["min_score"]), 3)
        prof["redact"]["strength"] = round(float(prof["redact"]["strength"]), 3)
        prof["redact"]["expand"] = round(float(prof["redact"]["expand"]), 3)
        ttl = prof.get("storage", {}).get("ttl")
        if ttl is not None:
            # Seconds; part of profile_hash, so a different TTL never reuses a
            # permanent artifact and vice versa.
            ttl = int(ttl)
            if not 60 <= ttl <= 30 * 86400:
                raise ValidationError(
                    "storage.ttl must be between 60 and 2592000 seconds",
                    {"got": ttl, "min": 60, "max": 2592000})
            prof["storage"]["ttl"] = ttl
        return prof

    @staticmethod
    def hash_of(profile: Dict[str, Any]) -> str:
        return profile_hash(profile)


# Settings a container must be able to set without writing a config file.
# An image is immutable, so `blurd config set` is not available before the
# process starts -- and a running daemon reads its config once, at startup.
ENV_OVERRIDES = {
    "BLURD_HOST": ("host", str),
    "BLURD_PORT": ("port", int),
    "BLURD_WORKERS": ("workers", str),      # int, or "auto"
    "BLURD_MALLOC_TRIM": ("malloc_trim", str),
    "BLURD_QUEUE_MAX": ("queue_max", int),
    "BLURD_QUEUE_MAX_BYTES": ("queue_max_bytes", str),
    "BLURD_ORT_ARENA": ("ort_arena", str),
    "BLURD_DRAIN_SECONDS": ("drain_seconds", int),
    "BLURD_HTTP_THREADS": ("http_threads", str),   # int, or "auto"
    "BLURD_ORT_THREADS": ("ort_threads", int),
    "BLURD_DASHBOARD_USER": ("dashboard_user", str),
    "BLURD_DASHBOARD_PASSWORD": ("dashboard_password", str),
    "BLURD_DASHBOARD_KEY_SECRET": ("dashboard_key_secret", str),
    "BLURD_DB_BACKEND": ("db.backend", str),
    "BLURD_DB_DSN": ("db.dsn", str),
    "BLURD_DB_DATABASE": ("db.database", str),
    "BLURD_STORAGE_BACKEND": ("storage.backend", str),
    "BLURD_S3_ENDPOINT": ("storage.s3.endpoint", str),
    "BLURD_S3_BUCKET": ("storage.s3.bucket", str),
    "BLURD_S3_REGION": ("storage.s3.region", str),
    "BLURD_S3_PREFIX": ("storage.s3.prefix", str),
}


def _apply_env(data: Dict[str, Any]) -> Dict[str, Any]:
    """Environment beats the config file. Credentials are deliberately absent
    from this map: they are read straight from the environment at use, never
    merged into a structure that something might later serialise to disk."""
    for var, (path, cast) in ENV_OVERRIDES.items():
        raw = os.environ.get(var)
        if raw is None or raw == "":
            continue
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            continue
        node = data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return data


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
