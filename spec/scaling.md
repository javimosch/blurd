# Scaling the admin UI

Measured on a 50 000-image / 50 000-job database (SQLite, WAL, one laptop core).

## What a dashboard page actually costs

The interesting number is not a single endpoint, it is one **page view**: the
listing, every thumbnail in the grid, and the header counters.

| | before | after |
|---|---:|---:|
| full grid page (list + 24 thumbnails + header) | **~700 ms** | **34 ms** |
| SQL queries for that page | ~250 | ~34 |
| listing, 24 rows | 74 queries / 6.7 ms | 6 queries / 4.1 ms |
| listing, 200 rows | 602 queries / 23 ms | 6 queries / 7 ms |
| one thumbnail | 7 queries / 2.5 ms | 1 query / 1.8 ms |
| header counters | 10 queries / 281 ms | 1 query / 56 ms, then memoised |
| page 1000 | 101 ms (and rising) | 4–9 ms (flat) |

## The four things that were wrong

**1. N+1 on every listing.** `tags_of`, `metadata_of` and `codes_of` are fine
for one record and wrong for a page: three queries per row, 602 of them to
render 200 rows. Replaced with `bulk_labels()` — three `WHERE source_sha IN
(...)` queries for the whole page.

**2. Thumbnails went through the full record builder.** Serving a tile called
`client.get()`, which assembled detections, tags, metadata and codes into a
JSON record and then threw it away to return a JPEG. Twenty-four times per
page. Now one indexed lookup.

**3. Thumbnails were stored inline in `artifacts`.** SQLite stores a row
contiguously, so a 14 kB blob in the row means every listing, sort and
aggregate over that table drags 14 kB per row through the page cache — 723 MB
of it at 50k rows, to compute `SUM(n_faces)`. Moved to a `thumbs` table.
`SELECT a.*` is also banned in listings for the same reason.

**4. OFFSET pagination.** `LIMIT 24 OFFSET 24000` walks and discards 24 000
rows; it is O(offset) and gets worse as the table grows. It is also *wrong*
while rows are being inserted: the window shifts underneath the reader, who
then sees duplicates or gaps. Replaced with keyset (cursor) pagination, which
seeks straight to the boundary — page 1000 costs what page 1 costs.

## Design consequences

**Counting is capped.** An exact `COUNT(*)` over a filtered million-row set is
a full scan on every page view, to render a number nobody reads past the first
few digits. Counting stops at 10 000 and the response says `total_capped:
true`; the UI shows "10,000+".

**The count happens once.** Paging deeper cannot change the total, so cursor
pages omit it entirely and the UI carries the number forward. With a selective
filter the count was the most expensive part of the request.

**Only index-backed sorts are offered.** `created`, `faces`, `plates`, `size`,
`review` — each with an index including the `id` tiebreaker, which is also what
makes the cursor seekable. Sorting by image width was dropped: it lives on
`images`, so it forces the join into a temp b-tree at 179 ms against 4 ms for
every other option. Offering a sort 40x slower than its neighbours is a trap.

**Header counters are memoised for 3 seconds.** They aggregate the whole
instance and the page asks for them on every tab switch. A counter three
seconds stale is indistinguishable from a fresh one to a human.

**Job listings are "light".** A listing renders id, status, code, duration and
sha — none of which need the artifact record that `job_dict` was building at
~7 queries per done row. A single `GET /v1/jobs/<id>` still embeds it.

## Migrations at this size

Moving 723 MB of thumbnails by copying them and dropping the column rewrites
the whole table in one transaction. Attempted on the 900 MB database, it ran
for minutes and grew the file to 1.7 GB with a 1 GB WAL beside it, because WAL
retains the old pages until commit. **A startup migration must never do that.**

So the move is lazy: new thumbnails go to `thumbs`, reads fall back to the
legacy column while it exists (`COALESCE(t.jpeg, a.thumb)`), and the backlog is
moved on demand:

```bash
blurd migrate-thumbs --batch 500     # resumable, reports progress
blurd vacuum                         # reclaim the pages, when you choose to
```

`VACUUM` is likewise not run automatically. A daemon that looks hung on boot is
worse than a file that is temporarily larger than it needs to be.

## What has not been addressed

- A filter matching a large fraction of rows still costs ~50 ms on the first
  page (the capped count with an `EXISTS` probe per row). Subsequent pages are
  4–9 ms.
- There is no full-text search over metadata; filters are exact-match.
- `COUNT(*)` on `images` for the header is a PK-index scan: 1.2 ms at 50k,
  proportionally more at 10M.
- The grid loads thumbnails one request per tile. A sprite or a batched
  endpoint would cut the request count, at the cost of cacheability.
