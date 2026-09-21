# profile_hash

`profile_hash` is the identity of a processing configuration. It is half of the
artifact cache key:

    artifact = (sha256(source bytes), profile_hash)

Hashing the source alone would be wrong: upgrading a detector or changing the
redaction mode must produce a *new* artifact, not silently keep serving an old
redaction made by an older model.

## Algorithm

1. Take the effective profile: config defaults deep-merged with any
   per-request overrides.
2. Round every float to 3 decimals (`min_score`, `strength`, `expand`), so
   float formatting differences between languages can never split the cache.
3. Serialise to **canonical JSON**:
   - object keys sorted by unicode code point
   - separators `,` and `:` with no whitespace
   - UTF-8, non-ASCII emitted literally (no `\uXXXX` escapes)
4. `sha256` of those UTF-8 bytes, hex-encoded, **first 16 characters**.

## Reference vector

Profile (the shipped default):

```json
{"detect":{"face":{"min_score":0.6,"model":"yunet-2023mar"},"max_side":1280,"plate":{"min_score":0.35,"model":"yolov9t-512-plates"}},"output":{"format":"jpeg","max_side":0,"quality":90},"redact":{"expand":0.18,"mode":"pixelate","shape":{"face":"ellipse","plate":"rect"},"strength":0.06},"version":1}
```

    profile_hash = 1e994f69934887ce

`storage.ttl` is optional and absent from the default: a blob that expires is
a *different processing intent*, so it gets its own profile_hash rather than
sharing cache space with the permanent one.

Any port (Go, machin, …) that produces a different value for this input is
wrong and will invalidate every artifact already in the database.
