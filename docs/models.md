---
title: Models
layout: default
nav_order: 3
---

# The detection models

blurd ships **no weights**. Both detectors are fetched on demand by
`blurd models pull`, pinned by URL **and** sha256, so the file you run is the
file that was tested:

| class | model | size | input | licence |
|---|---|---|---|---|
| faces | YuNet (`opencv/opencv_zoo`, 2023-03) | 233 KB | 320 px | Apache-2.0 |
| plates | YOLOv9-t 512 end-to-end (`ankandrew/open-image-models`) | 7.8 MB | 512 px | GPL-3.0 lineage |

Both run on CPU through onnxruntime — no GPU, no PyTorch, no framework
dependency beyond `onnxruntime` itself. A complete pipeline pass is ~130 ms
per image on a laptop i5.

## Why these two

The constraint that picked the models is the deployment target: a small CPU
container (the sizing model budgets ~750 MB total, workers included). That
ruled out the accuracy-first choices and made **lightweight + proven** the
whole criterion:

- **YuNet** is OpenCV's own tiny face detector — 233 KB, Apache-2.0, and it
  ships inside cv2 as `FaceDetectorYN`, which means the reference
  implementation and the model come from the same place. RetinaFace-family
  models are more accurate at distance but cost 10–50× the compute; at the
  sizes faces appear in dashcam/CCTV frames, the bottleneck is pixel count,
  not model capacity.
- **YOLOv9-t end-to-end** is the smallest converted plate detector that runs
  without post-processing code — the ONNX graph already includes NMS
  ("end2end"), so blurd does not carry a NMS implementation it would have to
  port and re-verify per backend/runtime. 7.8 MB and still real-time on CPU.

Detection is deliberately **regions, not characters**: blurd never OCRs a
plate — it locates and blurs the rectangle. That is why a model trained for
plate *localisation* is sufficient, and why an ALPR/OCR stack would be pure
cost: more dependencies, more failure modes, zero extra pixels blurred.

## What the benchmark showed

On a 24-image synthetic street set (generated specifically to vary subject
distance — close / mid / far):

| distance | faces | plates |
|---|---|---|
| close | strong | strong |
| mid (~10–20 m) | good | good |
| far (30 m+) | **none detected** | partial, low-confidence (0.37–0.42) |

The honest limit: faces below ~15–20 px are invisible to YuNet, and far
plates land right at the default 0.35 score floor. That is a property of the
input resolution, not a bug — and it is exactly what `needs_review` and the
per-submission `--face-score` / `--plate-score` overrides exist for. Lower
the thresholds only knowing what it buys: more far-region detections *and*
more false positives on street furniture.

## Threading, the load-bearing detail

The two detectors have **different concurrency rules**, and getting this
wrong corrupts silently:

- onnxruntime `InferenceSession` is thread-safe — the YOLO session is shared
  across all workers.
- `cv2.FaceDetectorYN` is **stateful** (input size is set on the instance) —
  sharing it under concurrency throws `(-215:Assertion failed) buf.shape`.
  blurd keeps one YuNet instance **per worker thread**.

This is the kind of bug that never appears in a single-image test and always
appears in production.

## Licensing, plainly

- **Face path is clean**: Apache-2.0 weights.
- **Plate path is GPL-3.0 lineage** (YOLOv9 weights). Fine for a POC and for
  self-hosted use; a real constraint if blurd is ever offered as a hosted
  product. The detector is one registry entry + one class — swapping it is a
  one-file change by design.

## Adding or swapping a model

1. A detector class in `src/detect.py` (`kind` decides the threading rule).
2. A registry entry in `src/models.py` — pinned URL + sha256 + licence.
3. Nothing else: the cache key is `(source_sha, profile_hash)` and the model
   name is inside the profile hash, so a model change produces *new*
   artifacts instead of silently serving redactions made by the old one.
