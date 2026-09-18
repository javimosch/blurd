"""Redaction. Pixelate by default, because it is the only mode here that is
irreversible in practice: a Gaussian blur is a linear, invertible-ish operator
and a determined attacker can recover a lot from it. Mosaic throws the
information away.
"""

from typing import List

import cv2
import numpy as np

from .detect import Detection
from .errors import ValidationError

MODES = ("pixelate", "blur", "solid")


def _expand_box(d: Detection, factor: float, w: int, h: int):
    """Detectors crop tight; chins, hair and plate borders leak otherwise."""
    cx, cy = d.x + d.w / 2.0, d.y + d.h / 2.0
    nw, nh = d.w * (1.0 + factor), d.h * (1.0 + factor)
    x1 = int(max(0, round(cx - nw / 2)))
    y1 = int(max(0, round(cy - nh / 2)))
    x2 = int(min(w, round(cx + nw / 2)))
    y2 = int(min(h, round(cy + nh / 2)))
    return x1, y1, x2, y2


def _redacted_patch(patch: np.ndarray, mode: str, strength: float) -> np.ndarray:
    ph, pw = patch.shape[:2]
    if mode == "solid":
        return np.zeros_like(patch)
    if mode == "pixelate":
        # strength is the block size as a fraction of the box: 0.06 -> ~16 blocks
        blocks_w = max(2, int(round(1.0 / max(strength, 0.01))))
        sw = max(1, min(pw, blocks_w))
        sh = max(1, min(ph, max(2, int(round(blocks_w * ph / max(pw, 1))))))
        small = cv2.resize(patch, (sw, sh), interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (pw, ph), interpolation=cv2.INTER_NEAREST)
    if mode == "blur":
        k = max(3, int(round(max(pw, ph) * max(strength, 0.01) * 4)) | 1)
        return cv2.GaussianBlur(patch, (k, k), 0)
    raise ValidationError(f"Unknown redact mode '{mode}'", {"known": list(MODES)})


def apply(img: np.ndarray, dets: List[Detection], redact_cfg: dict,
          inplace: bool = False) -> np.ndarray:
    """Redact the detected regions.

    `inplace=True` writes into the caller's array instead of copying it. For a
    10 MP image that copy is ~32 MB, held for the whole encode -- the single
    largest avoidable allocation in the pipeline, and it is pure waste whenever
    the caller has no further use for the original (which the pipeline does
    not: it encodes the redacted result and drops the source).

    The semantics are identical either way: overlapping boxes already read
    pixels that earlier boxes redacted, because the patches were taken from the
    output array, not the input.
    """
    mode = redact_cfg.get("mode", "pixelate")
    if mode not in MODES:
        raise ValidationError(f"Unknown redact mode '{mode}'", {"known": list(MODES)})
    strength = float(redact_cfg.get("strength", 0.06))
    expand = float(redact_cfg.get("expand", 0.18))
    shapes = redact_cfg.get("shape", {})

    out = img if inplace else img.copy()
    h, w = out.shape[:2]
    for d in dets:
        x1, y1, x2, y2 = _expand_box(d, expand, w, h)
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        patch = out[y1:y2, x1:x2]
        red = _redacted_patch(patch, mode, strength)
        if shapes.get(d.cls, "rect") == "ellipse":
            mask = np.zeros(patch.shape[:2], dtype=np.uint8)
            cv2.ellipse(mask, ((x2 - x1) // 2, (y2 - y1) // 2),
                        ((x2 - x1) // 2, (y2 - y1) // 2), 0, 0, 360, 255, -1)
            m3 = mask[:, :, None].astype(bool)
            out[y1:y2, x1:x2] = np.where(m3, red, patch)
        else:
            out[y1:y2, x1:x2] = red
    return out


def draw_boxes(img: np.ndarray, dets: List[Detection]) -> np.ndarray:
    """Debug overlay: what the detectors saw, on top of the redacted image."""
    out = img.copy()
    colors = {"face": (0, 220, 0), "plate": (0, 160, 255)}
    for d in dets:
        c = colors.get(d.cls, (255, 255, 255))
        cv2.rectangle(out, (d.x, d.y), (d.x + d.w, d.y + d.h), c, 2)
        cv2.putText(out, f"{d.cls} {d.score:.2f}", (d.x, max(12, d.y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, c, 1, cv2.LINE_AA)
    return out
