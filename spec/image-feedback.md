# Image feedback, detection tuning, reprocess (0.22.0)

## Feedback

A quality report on one redacted image: `kind` is one of `missed`,
`false_positive`, `bad_box`, `ok`, `other`, plus an optional `comment` and
`box` (`[x, y, w, h]` in pixels of the redacted image).

| call | who |
|---|---|
| `POST /v1/images/{sha}/feedback` | any key that can read the image (else 404) |
| `GET /v1/images/{sha}/feedback?status=all` | same |
| `GET /v1/image-feedback?status=open&kind=&since=&limit=` | the fast read for developer agents; newest first |
| `POST /v1/image-feedback/{id}/resolve {resolution}` | close it once handled |

Rows carry a `tenant` like tags (AGENTS.md rule 12): a scoped key writes and
reads only its own; an unrestricted key reads all. The dashboard writes as
`author: dashboard`, keys as `key:<name>`. Deleting an image deletes its
feedback. Not to be confused with `POST /v1/feedback`, which is feedback about
blurd itself (cli-feedback-spec).

## Detection thresholds

`detect.face.min_score`, `detect.plate.min_score`, `detect.plate.min_aspect`
(plate width / height; 0 = off) and `detect.plate.max_area` (largest share of
the frame a plate box may cover; 0/absent = off) are editable from the dashboard settings tab
(`/ui-api/profile/detect`), bounded to `min_score` 0.05–0.99, `min_aspect`
0–5 and `max_area` 0–1. They are profile fields, so a change alters `profile_hash`: existing
artifacts keep their result, new submissions produce new artifacts.

`min_aspect` defaults to 0.75 (0.22.0 shipped 1.0). Validated on 16 reported
isoprod images re-run from their originals: real plates measured 0.85
(photographed sideways) and 0.93 (angled), so 1.0 dropped them; the tall false
positives were a sticker at 0.70 and wall slabs at 0.74-0.76.

`max_area` is off by default because it only suits scenes shot from a
distance. On the same 16 images, real plates covered at most 3.8% of the frame
(a close, sideways plate) and false "plates" (street sign, barrier, rubbish,
wall slabs) 4.9-20%. poc-18-front runs it at 0.04 with face min_score 0.75:
all 10 real detections kept, false boxes 26 -> 5.

## Reprocess

`POST /v1/images/{sha}/reprocess?source=…[&wait=S][&keep_original=S]` re-runs
one image with the CURRENT profile and `force=1`. The source was never stored
(rule 1), so the caller picks where the bytes come from. There is deliberately
**no fallback chain**: a failure names the one source that failed, and the
user picks another.

| source | bytes from | fails with |
|---|---|---|
| `retained` | the live opt-in original (spec/originals.md) | 409 none kept / expired |
| `source_url` | `images.source_ref`, re-fetched now | 409 not url-submitted; 502 unreachable |
| `url` | `{"url": …}` in the body | 502 unreachable |
| `upload` | the request body (`Content-Type: image/*`) | — |

Every source but `retained` must hash to the image's sha (422 otherwise):
reprocess is not a new submission. `keep_original` additionally retains the
bytes, so the next round can use `retained`. Labels and codes stay on the sha;
manual regions belong to the old artifact and are not carried over.

The dashboard's reprocess button opens this choice (unavailable sources
greyed out). Bulk reprocess (`/ui-api/reprocess`, page or open feedback) takes
`retained` or `source_url` only.
