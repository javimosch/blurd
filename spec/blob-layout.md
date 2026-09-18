# Blob layout

Redacted images live on the filesystem, not in SQLite. Thousands of JPEGs in
the database would bloat every backup and make "just rsync the blobs"
impossible.

    <BLURD_HOME>/blobs/<sha[0:2]>/<sha[2:4]>/<sha>-<profile_hash><ext>

- `sha` is the sha256 hex of the **source** bytes.
- `profile_hash` is defined in `profile-hash.md`; the same source under two
  profiles yields two files side by side.
- `ext` is `.jpg` or `.png`, matching `profile.output.format`.
- Writes go to `<final>.part` and are then `rename()`d, so a reader never
  observes a partially written blob.

The `artifacts.blob_path` column stores this path **relative** to
`<BLURD_HOME>/blobs`, so the home directory can be moved or mounted elsewhere
without rewriting the database.

The source image is never written to disk at any point.
