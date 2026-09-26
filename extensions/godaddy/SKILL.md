---
name: godaddy
description: Manage domains registered at GoDaddy — list the portfolio, read or change DNS records (A, CNAME, TXT, MX), set nameservers, check availability and pricing, list subscriptions and renewals — through the `godaddy` server in the dangler fleet. Use whenever a task touches a domain's DNS or registration, e.g. "point this subdomain at the VPS", "add the TXT record for verification", "when does the domain renew", "is <name>.com available".
---

# godaddy — the domain and DNS name tag

The `godaddy` fleet server fronts the GoDaddy REST APIs as twelve tools under the
personal account named by its `identity`. Reach it through dangler:

1. `load_server {name: "godaddy"}` once, to see the schemas.
2. `call_tool {server: "godaddy", tool: "list_dns_records", arguments: {domain: "example.com"}}`.

## Which tool

| Need | Tool | Writes? |
|---|---|---|
| what domains the account holds, expiry, status | `list_domains`, `get_domain` | no |
| read DNS, optionally one type or one name | `list_dns_records` | no |
| add records without touching the rest | `add_dns_records` | additive |
| replace the records of one type+name (e.g. the `A` for `www`) | `set_dns_records` | scoped |
| remove one type+name | `delete_dns_record` | **destructive** |
| replace the entire zone | `replace_all_dns_records` | **destructive — full overwrite** |
| delegate to other nameservers | `set_nameservers` | yes |
| price and availability of a name | `check_availability`, `list_tlds` | no |
| what renews and when | `list_subscriptions` | no |
| anything else GoDaddy exposes | `raw_api` (method + path) | depends |

## Rules

- **Read before write.** `list_dns_records` first, then the narrowest write that does the job.
  Prefer `set_dns_records` (one type, one name) over `replace_all_dns_records`, which
  rewrites the whole zone and has no undo.
- Say what will change and get a yes before any destructive tool or a non-GET `raw_api`.
- Registration, renewal and purchases are money: report the price, never buy.
- A "no credentials" error means the PAT file named by `GODADDY_CREDENTIALS_FILE` is not
  filled on this machine; the server's `setup_hint` says how. Never paste a token anywhere.
- `GODADDY_READ_ONLY=1` in the fleet env refuses every mutating tool — use it for audits.
