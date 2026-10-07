---
name: cloudflare
description: Manage the Cloudflare account that fronts the fleet — list zones, read or change DNS records, purge the edge cache, and issue or revoke Origin CA certificates — through the `cloudflare` server in the dangler fleet. Use whenever a task touches a zone, a proxied hostname, the edge cache, or an origin certificate, e.g. "point this name at the tunnel", "is this record proxied", "purge the cache for these URLs", "issue an origin certificate for the new host", "what zones do we have".
---

# cloudflare — the edge name tag

The `cloudflare` fleet server fronts the Cloudflare v4 REST API as thirteen tools
under the account named by its `identity`. Reach it through dangler:

1. `load_server {name: "cloudflare"}` once, to see the schemas.
2. `call_tool {server: "cloudflare", tool: "list_zones", arguments: {}}`.

**`list_zones` is almost always the first call** — every other tool takes a
`zone_id`, and that is where you get it.

## Which tool

| Need | Tool | Writes? |
|---|---|---|
| is the token alive, and what can it do | `verify_token` | no |
| what zones the account holds, their plan and status | `list_zones`, `get_zone` | no |
| read DNS, optionally one type or one exact name | `list_dns_records` | no |
| add a record without touching the rest | `create_dns_record` | additive |
| flip `proxied`, repoint an address, edit a comment | `update_dns_record` | scoped patch |
| remove one record | `delete_dns_record` | **destructive** |
| drop cached objects by URL, host or prefix | `purge_cache` | yes |
| drop the whole zone's cache | `purge_cache` with `everything` | **destructive to cache** |
| what origin certificates exist, and their PEM | `list_origin_certificates`, `get_origin_certificate` | no |
| sign a CSR into an origin certificate | `issue_origin_certificate` | yes |
| revoke one | `revoke_origin_certificate` | **destructive — can take a site down** |
| anything else: tunnels, rulesets, Workers, Access | `raw_api` | depends |

## The rules this server exists to keep

**A named surface instead of arbitrary code.** The vendor's own plugin exposes an
`execute` tool that runs arbitrary JavaScript against the entire API under an
account login. Every tool here is one operation you can name, and the whole
surface sits behind one scoped, revocable token. Prefer a named tool; `raw_api`
is unaudited by construction and is for endpoints with no tool yet.

**One label under the apex.** The free Universal certificate covers `<apex>` and
`*.<apex>` only, and a wildcard matches exactly one label. A *proxied* record
with two labels beneath the apex has no edge certificate and fails the TLS
handshake. This took the bus API down on 2026-10-06. Names join levels with a
double hyphen instead: `api--myevery.<apex>`, `<slug>--dev.<apex>`. Before
setting `proxied: true` on anything, count the labels.

**Private keys never travel.** `issue_origin_certificate` takes a CSR, not a key.
Generate the CSR where the key must stay — on the origin host — and send only the
`CERTIFICATE REQUEST` block. The tool refuses a payload containing a private key
rather than forwarding it.

**Tag purge is Enterprise.** `purge_cache` with `tags` returns an error on a Free
plan. The fleet's zones are Free, so tag-based purge from here will fail; the
server-side purge credential on the VPS is a different thing with a different
scope.

## Credentials

A scoped API token, by reference only: `TOKEN=...` in the file
`CLOUDFLARE_CREDENTIALS_FILE` names, never in a repo and never pasted into a
conversation. Mint it at `https://dash.cloudflare.com/profile/api-tokens` with
only the scopes the work needs:

| Scope | For |
|---|---|
| Zone · Zone · Read | `list_zones`, `get_zone` — the baseline |
| Zone · DNS · Edit | reading and changing records |
| Zone · Cache Purge · Purge | `purge_cache` |
| Zone · SSL and Certificates · Edit | Origin CA issue, list, revoke |

Note for anyone reading older guides: Origin CA used to need the
`X-Auth-User-Service-Key` header and an Origin CA key. **That mechanism is
deprecated and stopped working on 30 September 2026.** A scoped API token is now
the only path, which is why this server has exactly one credential.

`verify_token` is the diagnostic — it separates "no token configured" from "token
configured but missing a scope", which otherwise look identical.

## Read-only mode

`CLOUDFLARE_READ_ONLY=1` refuses every mutating tool and any non-GET `raw_api`
call. Worth setting while exploring a zone you do not intend to change.
