# dangler-cloudflare

The Cloudflare v4 REST API as an MCP server: zones, DNS records, cache purge,
Origin CA certificates, and a `raw_api` escape hatch for the long tail.

A first-party dangler fleet extension — a plain stdio MCP server, listed in
`dangler.toml` like any other, with no runtime coupling to dangler.

## Why this exists when the vendor ships a plugin

Cloudflare's own Claude Code plugin authenticates by OAuth as the whole account
and exposes an `execute` tool that runs **arbitrary JavaScript against the entire
API**. It works, and it moved the fleet's DNS and tunnel on 2026-10-05/06. Two
things are wrong with it as the permanent path:

- **It is not in the keychain.** The credential is an account-wide OAuth login
  held outside dangler, with no `identity` line, no scoping and no revocation
  story. Every other service the fleet drives holds its secret by reference.
- **It has no namable surface.** `execute` cannot be read-only, cannot be
  audited from its name, and cannot refuse a destructive call, because the
  operation is the argument.

This server is the funnel answer: thirteen named tools, one scoped revocable
token read from a referenced file, an `identity`, and a read-only switch.

## Tools

| Tool | Writes | Notes |
|---|---|---|
| `verify_token` | no | The diagnostic. Separates a missing token from a missing scope |
| `list_zones` | no | Start here — every other tool takes the `zone_id` |
| `get_zone` | no | |
| `list_dns_records` | no | Filter by `type` and exact `name`; returns record ids |
| `create_dns_record` | additive | |
| `update_dns_record` | scoped patch | Sends only the fields given |
| `delete_dns_record` | **destructive** | |
| `purge_cache` | yes | Exactly one of `everything`, `files`, `tags`, `hosts`, `prefixes` |
| `list_origin_certificates` | no | |
| `get_origin_certificate` | no | Includes the PEM |
| `issue_origin_certificate` | yes | Takes a **CSR**, never a key |
| `revoke_origin_certificate` | **destructive** | Can take an origin offline |
| `raw_api` | depends | Unaudited by construction; prefer a named tool |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CLOUDFLARE_CREDENTIALS_FILE` | *one of these is required* | Path to a file with a `TOKEN=...` line |
| `CLOUDFLARE_API_TOKEN` | | The token directly, for a one-off |
| `CLOUDFLARE_READ_ONLY` | unset | Refuse every write and any non-GET `raw_api` |
| `DANGLER_DEBUG` | unset | Debug logging, to stderr |

Per the lazy-provisioning house rule the server starts and lists its tools with
nothing configured, so `dangler warm` can harvest schemas cold; the calls that
need the token are the ones that explain what is missing.

## Two facts worth not re-learning

**Origin CA takes an ordinary API token now.** It used to require the
`X-Auth-User-Service-Key` header with a separate Origin CA key. Cloudflare
deprecated service-key authentication and **it stopped working on 30 September
2026**. The scope is `Zone · SSL and Certificates · Edit`. Any guide still
describing the header is stale.

**The envelope lies about success more usefully than the status code does.**
Cloudflare wraps everything in `{success, errors, messages, result}` and can
answer `200` with `success: false`. This client checks the flag, not the status,
and surfaces the `errors` array verbatim.

## Scope boundary

The **server-side cache purge credential** that Drupal uses on the VPS is a
different thing: a separate token with a separate scope, living on the host that
needs it. It is deliberately not part of this extension, and this extension's
token should not be reused for it.
