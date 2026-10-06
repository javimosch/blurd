# Scoped API keys

A key may be restricted to one slice of an instance, so several external apps
can share one blurd:

```bash
blurd keys add app-acme --scope-tag acme
blurd keys add app-fleet  --scope-meta appId=fleet
blurd keys add operator                          # unrestricted
```

A scope is a **conjunction** — every constraint must hold. There is no OR
anywhere, deliberately, because a scope has to be unambiguous: it does not
merely filter reads, it defines a **tenant**, and a tenant owns a namespace.

## Why a tenant and not just a read filter

Two things break if a scope is only a `WHERE` clause bolted onto queries.

**1. Unique codes collide.** `external_id` is a primary key, and two apps will
both submit `IMG_0042.jpg`. Without namespacing, one app's code silently
resolves to another app's photo. That is a correctness bug, not a leak, and no
amount of read filtering fixes it. So the key is `(tenant, external_id)` — still
one index seek, and two tenants may use the same code freely.

**2. Labels leak through dedup.** blurd stores identical bytes once, on purpose.
If two tenants submit the same photo they share a row, so without ownership,
tenant A reads tenant B's tags, metadata and codes off it. `tags`, `metadata`
and `external_ids` therefore all carry a `tenant` column, and a scoped reader
sees only its own.

The tenant id is derived from the scope: `t_` + the first 16 hex of
`sha256(canonical_json(scope))`. Keys with an identical scope share a tenant
and therefore share data. Keys with different scopes are isolated. An unscoped
key is the operator: tenant `global`, sees everything.

## What the scope does on writes

A submission is **stamped** with the scope before anything else happens:

- the scope's tags are added automatically
- the scope's metadata is set automatically

so a scoped app does not have to remember to label its own uploads, and — more
importantly — cannot create an image it would then be unable to read.

An explicit value contradicting the scope is **refused** (`86 scope_violation`),
not overwritten. Silently rewriting a caller's metadata is worse than saying no.

## What the scope does on reads

Every read path is restricted: `GET /v1/images`, `/v1/images/<sha>`,
`/v1/blobs/<sha>`, `/v1/images/by-code/<code>`, `/v1/blobs/by-code/<code>`,
`/v1/jobs`, `/v1/jobs/<id>` and `/v1/stats`.

Out-of-scope reads return **404, not 403**. A 403 would confirm that a given sha
or job exists on the instance; a scoped key should not be able to probe for it.

`/v1/stats` is computed within the scope, so a tenant cannot learn how large the
instance it shares actually is.

## Deleting

An unrestricted `DELETE` removes the image outright. A **scoped** delete removes
only that tenant's labels and codes, and reports:

```json
{"deleted": false, "released": true,
 "note": "labels removed; the image is still referenced by another tenant"}
```

The bytes go only once nobody references them. One app must not be able to
destroy another's data by deleting "its" copy of shared bytes.

## The operator and namespaced codes

Because codes live in tenant namespaces and the operator is in none of them, an
unscoped key resolving a code searches across tenants. If the code is
unambiguous it is returned; if two tenants use it for different images the
operator gets `94 resource_conflict` naming the tenants, and disambiguates with
`?tenant=<tenant>`. It is never served an arbitrary one. A record read by an
unscoped key also carries `codes_detail`, showing which tenant owns each code.

## Moving keys between instances

Two instances can honour the same key without ever seeing its plaintext again,
because the stored credential **is** the hash:

```bash
blurd keys export --out keys.json        # on instance A
blurd keys import keys.json              # on instance B
```

An export record is `{id, name, prefix, key_sha, scope}` — the same sha256 the
`api_keys` table holds, plus the scope as a JSON object. Revoked keys are not
exported, and `last_used` is per-instance state and stays behind.

Import is **idempotent**: a `key_sha` already present (including one that was
revoked on B — importing must not resurrect it) is skipped; an `id` already
taken by a different key is remapped to a fresh one and reported in
`remapped_ids`. Scopes carry over verbatim, so an imported scoped key lands in
the *same* tenant — its unique codes and labels line up with what the external
app already produces on A.

For a single known key there is a shortcut — the operator still has the
plaintext (it lives in the app's config):

```bash
blurd keys add app-acme --key blk_... --scope-tag acme
```

This is CLI-only, like the rest of key management; neither is exposed over
HTTP or the dashboard. Every import is audited as `key.import`, and a
`--key` add is audited as `key.create` with `"provided": true`.

## Known limits

- **A scope cannot be changed after creation.** Issue a new key and revoke the
  old one; the tenant id is derived from the scope, so editing it in place would
  silently move the key to a different, empty tenant.
- **Dedup is observable across tenants.** Submitting bytes another tenant has
  already submitted returns `cached: true` immediately, which reveals that
  *somebody* holds that exact image. The content is not exposed, but the timing
  is a side channel. Turning it off would mean storing duplicate bytes per
  tenant, which defeats the deduplication the service is built on.
- **Blobs are shared.** Two tenants holding the same source get byte-identical
  redactions, which is correct but means a blob's `ETag` is the same for both.
- **The dashboard is an operator surface.** It sees every tenant. There is no
  per-tenant dashboard login.
