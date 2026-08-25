# dangler-godaddy

GoDaddy MCP server — the first first-party dangler fleet extension. Fronts the
GoDaddy REST APIs as twelve tools: the domain portfolio, full DNS record CRUD,
nameserver delegation, subscriptions, availability, and a `raw_api` escape
hatch that reaches every other endpoint GoDaddy exposes under
`api.godaddy.com`.

Standalone by design: it's a plain stdio MCP server, usable with any MCP
client — dangler just makes it lazy.

## Provisioning

GoDaddy has two auth generations (the Developer Platform launched 2026-07):

- **PAT (preferred)** — create a Personal Access Token in the dashboard at
  <https://developer.godaddy.com>, scoped (e.g. `domains.domain:read`,
  `domains.dns:update`), revocable. The only auth v3 endpoints accept.
- **sso-key (legacy)** — key+secret from <https://developer.godaddy.com/keys>.
  Deprecated, supported through 2026; still required by the older families
  (certificates, shoppers, subscriptions, orders, …).

Give the server either, via env or a credentials file (outside any repo,
e.g. `~/.godaddy/credentials`):

```
# env                                  # or GODADDY_CREDENTIALS_FILE contents
GODADDY_PAT=...                        PAT=...
GODADDY_API_KEY=... / _SECRET=...      KEY=... / SECRET=...
```

PAT wins when both are present. No credentials are required to *start* — the
schema is served, `dangler warm` harvests it, and any real call explains
what's missing.

### Environment reference

| Var | Effect |
|---|---|
| `GODADDY_PAT` | Personal Access Token → `Bearer` auth (preferred) |
| `GODADDY_API_KEY` / `GODADDY_API_SECRET` | legacy sso-key pair |
| `GODADDY_CREDENTIALS_FILE` | path to a `PAT=` (or `KEY=`/`SECRET=`) file |
| `GODADDY_ENV=ote` | target `api.ote-godaddy.com` (test, legacy keys) instead of production |
| `GODADDY_READ_ONLY=1` | refuse every mutating tool and any non-GET `raw_api` |
| `GODADDY_SHOPPER_ID` | send `X-Shopper-Id` for delegate-account operations |
| `DANGLER_DEBUG` | debug-level logging (stderr) |

## Tools

| Tool | Endpoint | Writes? |
|---|---|---|
| `list_domains` | `GET /v1/domains` | no |
| `get_domain` | `GET /v1/domains/{domain}` | no |
| `check_availability` | `GET /v1/domains/available` | no (gated: 50+ domains) |
| `list_tlds` | `GET /v1/domains/tlds` | no |
| `list_dns_records` | `GET /v1/domains/{d}/records[/{type}[/{name}]]` | no |
| `add_dns_records` | `PATCH /v1/domains/{d}/records` | additive |
| `set_dns_records` | `PUT /v1/domains/{d}/records/{type}/{name}` | scoped replace |
| `delete_dns_record` | `DELETE /v1/domains/{d}/records/{type}/{name}` | **destructive** |
| `replace_all_dns_records` | `PUT /v1/domains/{d}/records` | **destructive** (full overwrite) |
| `set_nameservers` | `PATCH /v1/domains/{domain}` | **destructive** (delegation) |
| `list_subscriptions` | `GET /v1/subscriptions` | no |
| `raw_api` | any method + path | whatever you send |

`raw_api` is the "ALL and EVERY" aggregator — the long tail without waiting
for a dedicated tool: certificates, orders, agreements, shoppers, aftermarket,
countries, **v2 customer APIs** (domain forwarding:
`/v2/customers/{customerId}/domains/forwards/{fqdn}`; transfers; privacy
forwarding), **v3 zones** (`/v3/domains/zones/{zone}/dns-records`, record-ID
based, PAT-only), **v3 registration** (quote → execute). Anything at
<https://developer.godaddy.com/doc>. The configured auth, OTE switch, and
read-only gate all apply to it.

## Known GoDaddy API caveats (verified 2026-08)

- **Account-size gates.** May 2024: GoDaddy gated APIs by domain count
  (403 `ACCESS_DENIED`, deliberately opaque). April 2026: the DNS/management
  APIs were re-opened to accounts with **1+ domain**; the **availability/
  suggest APIs still require 50+ domains** (or Discount Domain Club).
  Alternatives for availability on small accounts: GoDaddy's **official free
  no-auth hosted MCP** (`https://api.godaddy.com/v1/domains/mcp`,
  streamable-HTTP, read-only), or OTE (ungated, test data).
- **Rate limits**: 60 requests/minute per credential is the safe budget
  (GoDaddy's own docs conflict: the 2026 blog says 60/s + 20k/month). 429 +
  `Retry-After` on excess.
- **DNS v1 semantics**: no record IDs — records match by value; collection
  `PUT` is replace-all, so read-modify-write before any zone-level change.
  TTL floor 600 (v3 range 600–86400). API-driven changes can take 30–120 s to
  propagate.
- **Registrant contact changes** can trigger the 60-day transfer lock.
- **Websites + Marketing** has only a partner-gated API (Tailor Brands tier) —
  not wrapped here; `list_subscriptions` shows the products on the account.
- **GoDaddy email / Microsoft 365**: no GoDaddy API — tenants are ordinary
  Microsoft 365 tenants (manage via Microsoft Graph). At the GoDaddy layer,
  email management = MX/SPF/DKIM/DMARC records, which the `dns_*` tools cover.

## Fleet wiring

See the commented `[servers.godaddy]` block in
[`dangler.example.toml`](../../dangler.example.toml) — binary path, `identity`,
`setup_hint`, and the env block with `GODADDY_CREDENTIALS_FILE`.
