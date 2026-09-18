"""Detectors.

Every detector implements one method:

    detect(bgr: np.ndarray) -> list[Detection]

with boxes already expressed in the coordinate space of the image it was
handed. Scaling for the detection resolution and mapping boxes back to full
resolution happens once, in the pipeline -- not in each detector. Adding a
model means adding a class here and an entry in models.REGISTRY; nothing else
in blurd knows a detector exists.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List

import cv2
import numpy as np

from . import models
from .errors import ValidationError


@dataclass
class Detection:
    cls: str            # 'face' | 'plate'
    x: int
    y: int
    w: int
    h: int
    score: float
    detector: str

    def as_dict(self) -> dict:
        return {"cls": self.cls, "box": [self.x, self.y, self.w, self.h],
                "score": round(self.score, 4), "detector": self.detector}


class YuNetFaceDetector:
    """OpenCV YuNet. 233 KB, Apache-2.0, ships inside cv2 as FaceDetectorYN."""

    def __init__(self, model_path: Path, min_score: float, name: str):
        self.name = name
        self.min_score = min_score
        self._det = cv2.FaceDetectorYN.create(
            model=str(model_path), config="", input_size=(320, 320),
            score_threshold=float(min_score), nms_threshold=0.3, top_k=5000,
        )

    def detect(self, bgr: np.ndarray) -> List[Detection]:
        h, w = bgr.shape[:2]
        # YuNet requires input_size to match the frame it is given, exactly.
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(bgr)
        out = []
        if faces is None:
            return out
        for f in faces:
            x, y, bw, bh, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])
            if score < self.min_score:
                continue
            out.append(Detection("face", int(x), int(y), int(bw), int(bh), score, self.name))
        return out


class YoloV9End2EndDetector:
    """YOLOv9 exported with NMS inside the graph.

    Output is [N, 7] = (batch_idx, x1, y1, x2, y2, class_id, score) in the
    letterboxed input space -- note class and score are in THAT order, which is
    the opposite of most YOLO exports and the single easiest thing to get
    wrong here.
    """

    def __init__(self, model_path: Path, min_score: float, name: str, cls: str,
                 size: int, threads: int = 1):
        import onnxruntime as ort

        self.name = name
        self.cls = cls
        self.size = size
        self.min_score = min_score
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        # The CPU memory arena keeps freed tensors for reuse. That is a good
        # trade on a dedicated inference box and a bad one on a small VM, where
        # the arena is simply resident memory that never comes back.
        opts.enable_cpu_mem_arena = ARENA[0]
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input = self._sess.get_inputs()[0].name

    def _letterbox(self, img):
        ih, iw = img.shape[:2]
        r = min(self.size / ih, self.size / iw)
        nw, nh = int(round(iw * r)), int(round(ih * r))
        dw, dh = (self.size - nw) / 2, (self.size - nh) / 2
        if (iw, ih) != (nw, nh):
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
        left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
        img = cv2.copyMakeBorder(img, top, bottom, left, right,
                                 cv2.BORDER_CONSTANT, value=(114, 114, 114))
        return img, r, (dw, dh)

    def detect(self, bgr: np.ndarray) -> List[Detection]:
        lb, ratio, (dw, dh) = self._letterbox(bgr)
        blob = lb.transpose(2, 0, 1)[::-1]              # HWC BGR -> CHW RGB
        blob = np.ascontiguousarray(blob, dtype=np.float32) / 255.0
        preds = self._sess.run(None, {self._input: blob[None]})[0]
        out = []
        ih, iw = bgr.shape[:2]
        for row in preds:
            score = float(row[6])
            if score < self.min_score:
                continue
            x1 = (float(row[1]) - dw) / ratio
            y1 = (float(row[2]) - dh) / ratio
            x2 = (float(row[3]) - dw) / ratio
            y2 = (float(row[4]) - dh) / ratio
            x1, y1 = max(0.0, x1), max(0.0, y1)
            x2, y2 = min(float(iw), x2), min(float(ih), y2)
            if x2 <= x1 or y2 <= y1:
                continue
            out.append(Detection(self.cls, int(x1), int(y1),
                                 int(x2 - x1), int(y2 - y1), score, self.name))
        return out


# Two caches, because the two detector kinds have different threading rules.
#
#   * onnxruntime's InferenceSession.run() IS thread-safe, so one session is
#     shared by every worker -- that also keeps memory flat as workers scale.
#   * cv2.FaceDetectorYN is NOT. It is a stateful object: setInputSize() then
#     detect(). Sharing one across workers corrupts it under concurrency and
#     the model throws "(-215:Assertion failed) buf.shape". This is invisible
#     until two images are processed at once, which is why it survived every
#     single-image test and only surfaced under a throughput benchmark.
ORT_THREADS = [1]                # set once from config at daemon start
ARENA = [False]                   # onnxruntime cpu memory arena; see build()
_CACHE = {}                      # shared, thread-safe detectors
_LOCAL = threading.local()       # per-thread, stateful detectors
_BUILD_LOCK = threading.Lock()


def build(models_dir: Path, model_name: str, min_score: float):
    """Detectors are cached: loading an ORT session costs ~100 ms, which would
    otherwise dominate the per-image budget."""
    key = (str(models_dir), model_name, round(float(min_score), 3))
    spec = models.spec(model_name)

    if spec["kind"] == "yunet":
        local = getattr(_LOCAL, "cache", None)
        if local is None:
            local = _LOCAL.cache = {}
        if key not in local:
            local[key] = _construct(models_dir, model_name, min_score, spec)
        return local[key]

    if key in _CACHE:
        return _CACHE[key]
    with _BUILD_LOCK:
        if key not in _CACHE:
            _CACHE[key] = _construct(models_dir, model_name, min_score, spec)
    return _CACHE[key]


def _construct(models_dir: Path, model_name: str, min_score: float, spec: dict):
    path = models.path_for(models_dir, model_name)
    if not path.exists():
        raise ValidationError(
            f"Model '{model_name}' is not downloaded",
            {"expected_path": str(path)},
            [f"Run: blurd models pull {model_name}", "Run: blurd models pull --all"],
        )

    if spec["kind"] == "yunet":
        det = YuNetFaceDetector(path, min_score, model_name)
    elif spec["kind"] == "yolov9_end2end":
        det = YoloV9End2EndDetector(path, min_score, model_name,
                                    spec["cls"], spec["input_size"],
                                    threads=ORT_THREADS[0])
    else:
        raise ValidationError(f"Unsupported detector kind '{spec['kind']}'")
    return det
