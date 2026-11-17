"""Configuration: home dir layout, defaults, and the processing profile."""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from .canonical import profile_hash
from .errors import ValidationError

DEFAULT_HOME = Path(os.environ.get("BLURD_HOME", Path.home() / ".blurd"))

# The processing profile. Everything here feeds profile_hash, so any change
# invalidates cached artifacts by design. Keep it small and explicit.
# Canonical list of redaction algorithms. redact.py imports this (config has
# no heavy deps; the reverse import would pull cv2 into every config load).
REDACT_MODES = ("pixelate", "blur", "solid")

DEFAULT_PROFILE: Dict[str, Any] = {
    "version": 1,
    "detect": {
        "face": {"model": "yunet-2023mar", "min_score": 0.6},
        # min_aspect: plates are wider than tall. 0.75, not 1.0: on the
        # isoprod originals two REAL plates measured 0.85 (photographed
        # sideways) and 0.93 (angled), while the tall false positives were
        # stickers at 0.70 and slabs at 0.74-0.76. Changes profile_hash
        # (0.22.x) -- knowingly, see AGENTS.md rule 3. Optional `max_area`
        # (share of the frame; off by default) caps implausibly big boxes.
        "plate": {"model": "yolov9t-512-plates", "min_score": 0.35,
                  "min_aspect": 0.75},
        "max_side": 1280,          # long edge used for detection, boxes scale back
    },
    "redact": {
        "mode": "pixelate",        # pixelate | blur | solid
        # optional per-class override "modes": {"face": m, "plate": m} —
        # absent on purpose: adding the key would change profile_hash for
        # every artifact already produced by existing instances.
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
    "dashboard_allow_key_creation": True,
    "dashboard_key_secret": None,
    # Instance policy: which redaction modes requests may ask for. Not part of
    # the profile (it is not a processing parameter, it is a policy), so it
    # does NOT affect profile_hash.
    "allowed_redact_modes": list(REDACT_MODES),
    # Opt-in retention of SOURCE images (src/originals.py). Off by default:
    # it is the only exception to "never persist the source image", and it
    # also needs the request to ask. The dashboard can override these at
    # runtime (stored in the database); env beats both.
    "originals": {"enabled": False, "max_ttl": 30 * 86400,
                  "url_mode": "authenticated", "sign_ttl": 900},
    "fetch": {
        "timeout_s": 15,
        # Applies to every image input -- url fetch, file read and raw upload.
        # 5 MB is ~3.5x the largest file seen in a 12-month production sample
        # (max 1.43 MB, p99 633 KB); a browser posting more than that is a gap.
        "max_bytes": 5 * 1024 * 1024,
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
        # Hard cap on live redacted blobs, in bytes; 0 = unlimited. Counted
        # from artifacts.blob_size (backend-agnostic -- works for s3 too), so
        # thumbnails and metadata are outside the budget. Over cap: expired
        # blobs are pruned first, then the write is refused with 507
        # `storage_full`. Set it on any box where disk exhaustion is the
        # failure to prevent (demo VMs, shared hosts).
        "max_bytes": 0,
        # Opt-in disk guard: refuse new work once the filesystem holding
        # `disk_path` (default: the home) is at or above this percentage
        # used; 0 = off. Unlike max_bytes it sees everything on the disk --
        # originals, thumbs, the database, other tenants of the volume -- but
        # only protects the bucket when the object store shares that disk
        # (e.g. MinIO on the same VM). See spec/storage.md.
        "max_disk_pct": 0,
        "disk_path": None,
        # Rotation (opt-in): with max_bytes set and evict="oldest", crossing
        # evict_high_pct of the cap deletes the OLDEST live objects -- TTL or
        # not -- until usage is back under evict_low_pct, instead of refusing
        # with 507. Order: expired, then originals and blobs of images with no
        # open feedback, then those with open feedback. Rows (and thumbs) stay:
        # an evicted image answers 410 with reason "evicted". The database is
        # deliberately NOT rotated; max_disk_pct is what bounds it.
        "evict": "off",
        "evict_high_pct": 90,
        "evict_low_pct": 75,
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
        self._rt = None          # last runtime overlay read from the database
        self._rt_at = 0.0

    def ensure_dirs(self) -> None:
        for d in (self.home, self.blobs_dir, self.models_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _runtime(self):
        """The shared runtime row (src/runtime.py), at most every RUNTIME_TTL
        seconds. Only once the database backend is configured -- reading it
        earlier would recurse (the backend is chosen from this config) -- and
        failing open: no database yet means no runtime layer, not an error."""
        from . import db_sql, runtime
        now = time.monotonic()
        if self._rt is not None and now - self._rt_at < runtime.RUNTIME_TTL:
            return self._rt
        self._rt_at = now
        if "d" not in db_sql._DIALECT:
            return self._rt or {}
        try:
            from . import db
            rt = runtime.load(db.connect(self.db_file))
        except Exception:
            rt = self._rt or {}
        if rt != self._rt:
            self._rt = rt
            self._data = None
        return rt

    @property
    def data(self) -> Dict[str, Any]:
        rt = self._runtime()
        if self._data is None:
            data = dict(DEFAULTS)
            if self.config_file.exists():
                try:
                    data = _deep_merge(data, json.loads(self.config_file.read_text()))
                except (OSError, ValueError):
                    pass
            if rt:
                from .runtime import overlay
                data = overlay(data, rt)
            self._data = _apply_env(data)
        return self._data

    def refresh(self) -> None:
        """Re-read the runtime row now (after this process wrote it)."""
        self._rt_at = 0.0
        self._data = None

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
                # 0 / null = off, and absent from the profile: keeps the
                # default profile_hash identical for instances not using it.
                if not prof["detect"][cls].get("max_area"):
                    prof["detect"][cls].pop("max_area", None)
                else:
                    prof["detect"][cls]["max_area"] = round(
                        float(prof["detect"][cls]["max_area"]), 3)
                if prof["detect"][cls].get("min_aspect") is not None:
                    prof["detect"][cls]["min_aspect"] = round(
                        float(prof["detect"][cls]["min_aspect"]), 3)
        prof["redact"]["strength"] = round(float(prof["redact"]["strength"]), 3)
        prof["redact"]["expand"] = round(float(prof["redact"]["expand"]), 3)
        # Whitelist: every mode the profile would use (default + per-class)
        # must be allowed by the instance. Checked here so both the config
        # profile and request overrides go through the same gate.
        allowed = self.get("allowed_redact_modes") or list(REDACT_MODES)
        used = {prof["redact"].get("mode", "pixelate")}
        modes = prof["redact"].get("modes") or {}
        if not isinstance(modes, dict):
            raise ValidationError(
                "redact.modes must be an object like {\"face\": \"solid\"}",
                {"got": type(modes).__name__})
        for cls, m in modes.items():
            if cls not in ("face", "plate"):
                raise ValidationError(
                    f"Unknown class '{cls}' in redact.modes",
                    {"known": ["face", "plate"]})
        # "modes": null / {} / {"face": null} all mean "no override" — keep the
        # hash identical to a profile that simply omits the key, or the same
        # intent would produce two cache identities.
        modes = {k: v for k, v in modes.items() if v}
        used |= set(modes.values())
        if modes:
            prof["redact"]["modes"] = modes
        else:
            prof["redact"].pop("modes", None)
        bad = used - set(allowed)
        if bad:
            raise ValidationError(
                "Redact mode not allowed by this instance",
                {"requested": sorted(bad), "allowed": list(allowed)})
        for m in used:
            if m not in REDACT_MODES:
                raise ValidationError(
                    f"Unknown redact mode '{m}'", {"known": list(REDACT_MODES)})
        out = prof.setdefault("output", {})
        opt = out.get("optimize")
        if opt is not None and not isinstance(opt, dict):
            raise ValidationError(
                'output.optimize must be an object like {"above_bytes": 1500000}',
                {"got": type(opt).__name__})
        above = int((opt or {}).get("above_bytes") or 0)
        if above <= 0:
            # Off is ABSENT, not {"above_bytes": 0}: the default profile_hash
            # must not move for instances that never asked for optimization.
            out.pop("optimize", None)
        else:
            out["optimize"] = _optimize_block(opt, above)
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


OPTIMIZE_LIMITS = {"above_bytes": (1, 1 << 31), "max_side": (256, 16384),
                   "quality": (30, 95)}
OPTIMIZE_DEFAULTS = {"max_side": 2560, "quality": 82}


def _optimize_block(opt: Dict[str, Any], above: int) -> Dict[str, Any]:
    """Validate `output.optimize`: sources larger than `above_bytes` are
    downscaled to `max_side` (long edge) right after decode and encoded at
    `quality` -- smaller output, less redaction work. Detection already runs
    on a 1280 px copy, so what it finds does not change."""
    block = {"above_bytes": above,
             "max_side": int(opt.get("max_side") or OPTIMIZE_DEFAULTS["max_side"]),
             "quality": int(opt.get("quality") or OPTIMIZE_DEFAULTS["quality"])}
    for k, (lo, hi) in OPTIMIZE_LIMITS.items():
        if not lo <= block[k] <= hi:
            raise ValidationError(
                f"output.optimize.{k} must be between {lo} and {hi}",
                {"got": block[k], "min": lo, "max": hi})
    unknown = set(opt) - set(block)
    if unknown:
        raise ValidationError("Unknown key in output.optimize",
                              {"unknown": sorted(unknown), "known": sorted(block)})
    return block


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
    "BLURD_FETCH_MAX_BYTES": ("fetch.max_bytes", int),
    "BLURD_STORAGE_MAX_BYTES": ("storage.max_bytes", int),
    "BLURD_STORAGE_MAX_DISK_PCT": ("storage.max_disk_pct", int),
    "BLURD_STORAGE_DISK_PATH": ("storage.disk_path", str),
    "BLURD_STORAGE_EVICT": ("storage.evict", str),
    "BLURD_STORAGE_EVICT_HIGH_PCT": ("storage.evict_high_pct", int),
    "BLURD_STORAGE_EVICT_LOW_PCT": ("storage.evict_low_pct", int),
    "BLURD_HTTP_THREADS": ("http_threads", str),   # int, or "auto"
    "BLURD_ORT_THREADS": ("ort_threads", int),
    "BLURD_DASHBOARD_USER": ("dashboard_user", str),
    "BLURD_DASHBOARD_PASSWORD": ("dashboard_password", str),
    "BLURD_DASHBOARD_KEY_SECRET": ("dashboard_key_secret", str),
    # Default true since 0.23; when BLURD_DASHBOARD_KEY_SECRET is set every
    # mint must also present it (rule 10).
    "BLURD_DASHBOARD_ALLOW_KEY_CREATION": (
        "dashboard_allow_key_creation",
        lambda s: s.strip().lower() in ("1", "true", "yes", "on")),
    "BLURD_DB_BACKEND": ("db.backend", str),
    "BLURD_DB_DSN": ("db.dsn", str),
    "BLURD_DB_DATABASE": ("db.database", str),
    "BLURD_STORAGE_BACKEND": ("storage.backend", str),
    # Whole-profile JSON and the request-mode whitelist — both are deployment
    # policy an operator must set without exec'ing into the container.
    "BLURD_PROFILE": ("profile", json.loads),
    "BLURD_ALLOWED_REDACT_MODES": (
        "allowed_redact_modes",
        lambda s: [m.strip() for m in s.split(",") if m.strip()]),
    "BLURD_ORIGINALS_ENABLED": ("originals.enabled", str),
    "BLURD_ORIGINALS_MAX_TTL": ("originals.max_ttl", int),
    "BLURD_ORIGINALS_URL_MODE": ("originals.url_mode", str),
    "BLURD_ORIGINALS_SIGN_TTL": ("originals.sign_ttl", int),
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
        # A dict-valued env (BLURD_PROFILE) must merge over defaults, not
        # replace them -- a partial profile would otherwise drop the detect
        # models it didn't mention.
        if isinstance(value, dict) and isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = _deep_merge(node[parts[-1]], value)
        else:
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
