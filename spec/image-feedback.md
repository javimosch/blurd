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

`detect.face.min_score`, `detect.plate.min_score` and `detect.plate.min_aspect`
(plate width / height; 0 = off) are editable from the dashboard settings tab
(`/ui-api/profile/detect`), bounded to `min_score` 0.05–0.99 and `min_aspect`
0–5. They are profile fields, so a change alters `profile_hash`: existing
artifacts keep their result, new submissions produce new artifacts.

`min_aspect` defaults to 1.0. On the isoprod dataset 97 of 748 plate boxes
were taller than wide; every one sampled was a sticker, sign, wall or rubbish.

## Reprocess

`POST /v1/images/{sha}/reprocess[?wait=S]` re-runs one image with the CURRENT
profile and `force=1`. The source was never stored (rule 1), so it needs a way
back to the bytes, in this order:

1. a live retained original (`keep_original`, spec/originals.md);
2. the URL the image was submitted from (`images.source_ref`), re-fetched
   through the normal SSRF-checked fetcher.

Neither available (an upload with no original) is `409 resource_conflict`.
If the URL now serves different bytes, the result is a different `source_sha`.
Labels and codes stay attached to the sha. Manual regions belong to the old
artifact and are not carried over.
