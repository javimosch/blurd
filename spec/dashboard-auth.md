# Dashboard authentication and key management

The dashboard and the `/v1` API use **separate credentials on purpose**: a
browser session typed by a human and a machine-to-machine credential should
never be the same secret. `blurd` enforces this — dashboard basic-auth
credentials do not authenticate a single `/v1` endpoint, and an API key does
not open the dashboard.

## What the dashboard may do with API keys

| Operation | Available | Why |
|---|---|---|
| list keys (id, name, prefix, created, last used, status) | always | operational hygiene; the listing never contains a usable key, only its prefix |
| **revoke** a key | always | fail-safe: the worst outcome of a compromise or a mistake is denial of service, never granted access — and revocation is the thing you need to do fast, during an incident, from wherever you are |
| **create** a key | on by default since 0.23; optional second secret | a minted key **outlives the dashboard password**. Set `BLURD_DASHBOARD_KEY_SECRET` to require a second secret per mint, or `BLURD_DASHBOARD_ALLOW_KEY_CREATION=false` to forbid minting |
| **rename** a key | always | cosmetic; audited as `key.rename` |
| **remove** a key | revoked keys only | an active key must be revoked first, so a delete can never silently cut a caller off; audited as `key.purge` |
| edit **scope limits** | always | per-scope max input size, optimize floor, retention (spec/storage.md); audited as `scope_policy.set/unset` |

Before 0.23 creation was opt-in. It is now on by default; to require a second
secret, or to turn it off:

```bash
blurd dashboard-keys enable --secret <admin-secret>   # optional; must differ from the
                                                      # dashboard password,
                                                      # min 12 chars
blurd dashboard-keys status
blurd dashboard-keys disable
```

With a secret set, minting requires the `X-Blurd-Admin-Secret` header, so
compromising the dashboard login alone is not sufficient. Without one, the
dashboard login is the root of trust for minting -- the 0.23 default. The plaintext key is
returned exactly once; only its sha256 is stored.

## CSRF

Every state-changing `/ui-api` call requires a token:

- the page sets `blurd_csrf=<random>; Path=/; SameSite=Strict`
- the browser echoes it in `X-Blurd-CSRF`
- the server compares the two with a constant-time check

`SameSite=Strict` is what actually stops the attack — the cookie never
accompanies a cross-site request — and the header echo covers the case where it
somehow does, since a page that cannot read the cookie cannot forge the header.

This matters more than it looks. Before key management existed, the only thing
protecting `DELETE /ui-api/images/<sha>` from a hostile page was the CORS
preflight that a cross-origin `DELETE` happens to trigger. That is an accident
of the method chosen, not a defence, and it disappears the moment an endpoint
accepts a POST with a simple content type — which is exactly what key creation
is. The token makes the protection deliberate instead of incidental.

## Audit

Every privileged mutation is recorded in the `audit` table and visible under
`blurd audit` and the dashboard's keys tab:

| action | recorded |
|---|---|
| `key.create` | key id, name, prefix, channel, source address |
| `key.revoke` | key id, channel, source address |
| `image.delete` | source sha, channel, source address |

`actor` is the channel (`dashboard` / `cli`), not a person. With one shared
dashboard password that is the most the credential can honestly attest to —
recording a username would imply an accountability the design does not provide.
Per-person attribution needs per-person logins, which is a real feature, not a
config change.

## Known limits

- One shared dashboard password: no per-user attribution, no lockout, no 2FA.
- No rate limiting on dashboard login attempts.
- The minted key is displayed in a browser, so it can land in screenshots or
  browser history in a way a terminal does not.
- Revocation is immediate (checked per request), but there is no key expiry.
