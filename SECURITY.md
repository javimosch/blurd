# Security

## Reporting a vulnerability

Please report privately via [GitHub Security Advisories][advisories] rather than
opening a public issue. A first response should come within a few days.

[advisories]: https://github.com/javimosch/blurd/security/advisories/new

## What blurd protects

blurd exists to make images safe to keep, so the security properties below are
the product, not decoration. Several are asserted by `tests/conformance.py`.

- **The original image is never written to disk.** Only `sha256(source)`, the
  dimensions, and the redacted output are stored. Queued uploads live in memory
  and are lost on restart *by design* — spooling them would be the obvious fix
  and would break this guarantee.
- **API keys are stored hashed** (SHA-256). The plaintext is shown once, at
  creation. A copy of the database grants no access.
- **Dashboard credentials never authenticate a `/v1` call, and an API key never
  opens the dashboard.** Conformance asserts both directions.
- **Scoped keys are tenants.** An out-of-scope read returns **404, never 403** —
  a 403 would confirm the resource exists, which is what a scoped caller must
  not be able to learn.
- **URL ingestion is SSRF-guarded**: DNS resolution and private-range checks,
  with redirects followed manually and **the address re-validated at every
  hop**. A library that follows redirects internally reintroduces the bypass.
- **Every dashboard mutation requires a CSRF token.** The CORS preflight that a
  `DELETE` or JSON `POST` happens to trigger is an accident of content type, not
  a defence.
- **EXIF is stripped** from the redacted output.
- **Overload is backpressure, not collapse**: a bounded queue answers 503 with
  `Retry-After` rather than accumulating until the process is OOM-killed.

## What blurd does not claim

- **It is not a guarantee of anonymisation.** Detection is a model: it misses
  faces and plates, especially small, blurred, or oddly-angled ones. blurd
  reports a confidence and flags low-confidence results for review. Treat it as
  a large reduction in exposure, not as compliance.
- **Redaction is irreversible by construction** (the pixels are replaced, not
  overlaid) — but only for what was detected.
- **The dashboard password is one shared human secret.** Key creation from the
  dashboard is off by default behind a second secret, because a minted key
  outlives the password that minted it.

## Deployment notes

- Put blurd behind your own backend. It is a machine-to-machine service and is
  not designed for direct exposure to browsers.
- Terminate TLS in front of it.
- The `/v1/health` endpoint is unauthenticated by design, so it can serve as a
  container probe. It exposes version, status and backend names only.
