"""Model registry: pinned URLs + sha256, downloaded on demand.

URLs are pinned HERE rather than taken from any upstream helper library --
open-image-models' own hub.py points at 404ing filenames ("license-plate" vs
the actual "license-plates"), which would make `blurd models pull` fail for
reasons that have nothing to do with blurd.
"""

import secrets
import urllib.request
from pathlib import Path
from typing import Dict

from .canonical import sha256_hex
from .errors import Upstream, ValidationError

REGISTRY: Dict[str, dict] = {
    "yunet-2023mar": {
        "kind": "yunet",
        "cls": "face",
        "file": "face_detection_yunet_2023mar.onnx",
        "url": "https://github.com/opencv/opencv_zoo/raw/main/models/"
               "face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "size": 232589,
        "input_size": 320,
        "license": "Apache-2.0",
        "source": "opencv/opencv_zoo",
    },
    "yolov9t-512-plates": {
        "kind": "yolov9_end2end",
        "cls": "plate",
        "file": "yolo-v9-t-512-license-plates-end2end.onnx",
        "url": "https://github.com/ankandrew/open-image-models/releases/download/"
               "assets/yolo-v9-t-512-license-plates-end2end.onnx",
        "sha256": "746fdd358ec110418775d7c9d8d07910d48b1a21471f92bf4421f6510d6daade",
        "size": 7799480,
        "input_size": 512,
        # YOLOv9 weights are GPL-3.0 derived. Fine for a POC; revisit before
        # shipping this as a hosted product (see README "Licensing").
        "license": "GPL-3.0 (yolov9 lineage)",
        "source": "ankandrew/open-image-models",
    },
}


def spec(name: str) -> dict:
    if name not in REGISTRY:
        raise ValidationError(
            f"Unknown model '{name}'",
            {"known": sorted(REGISTRY)},
            ["Run: blurd models list"],
        )
    return REGISTRY[name]


def path_for(models_dir: Path, name: str) -> Path:
    return Path(models_dir) / spec(name)["file"]


def is_present(models_dir: Path, name: str) -> bool:
    p = path_for(models_dir, name)
    return p.exists() and p.stat().st_size == spec(name)["size"]


def pull(models_dir: Path, name: str, force: bool = False) -> dict:
    """Download one model, verifying its sha256 before it is trusted."""
    s = spec(name)
    dest = path_for(models_dir, name)
    Path(models_dir).mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        if sha256_hex(dest.read_bytes()) == s["sha256"]:
            return {"model": name, "path": str(dest), "status": "present"}

    # The temp name is unique per process: replicas sharing a models volume all
    # pull at boot, and a fixed ".part" means one renames the file while
    # another is still writing to it -- which surfaces as a FileNotFoundError
    # on a path that plainly existed a moment ago.
    import os as _os
    tmp = dest.with_suffix(dest.suffix + f".part-{_os.getpid()}-{secrets.token_hex(4)}")
    try:
        req = urllib.request.Request(s["url"], headers={"User-Agent": "blurd/0.1"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            tmp.write_bytes(resp.read())
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        # A peer may have finished the same download while this one failed.
        if dest.exists() and sha256_hex(dest.read_bytes()) == s["sha256"]:
            return {"model": name, "path": str(dest), "status": "present"}
        raise Upstream(f"Failed to download model '{name}': {exc}", {"url": s["url"]})

    got = sha256_hex(tmp.read_bytes())
    if got != s["sha256"]:
        tmp.unlink(missing_ok=True)
        raise Upstream(
            f"Checksum mismatch for model '{name}'",
            {"expected": s["sha256"], "got": got, "url": s["url"]},
            retry_after=None,
        )
    tmp.replace(dest)          # atomic; last writer wins, and both wrote the same bytes
    return {"model": name, "path": str(dest), "status": "downloaded",
            "bytes": dest.stat().st_size}


def listing(models_dir: Path) -> list:
    return [
        {
            "name": name,
            "class": s["cls"],
            "kind": s["kind"],
            "license": s["license"],
            "source": s["source"],
            "bytes": s["size"],
            "present": is_present(models_dir, name),
            "path": str(path_for(models_dir, name)),
        }
        for name, s in sorted(REGISTRY.items())
    ]
