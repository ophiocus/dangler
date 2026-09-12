# gws-mcp

Self-owned MCP server giving Claude Code **full read/write on Google Docs and
Google Sheets** and **read-only Google Drive** (search, metadata, export) —
so files are edited **in place** (same id, same URL, revision history intact)
instead of recreated to iterate.

Supersedes the claude.ai Google Drive connector for anything that isn't pure
reading (that connector's `update_file` is metadata-only and it has no Docs
body / Sheets cell API).

## Trust model — no third-party server code

- **Our code:** `src/gws_mcp/` (~400 lines: `auth.py`, `server.py`).
- **Dependencies (5, all vendor-official):** Anthropic's `mcp` SDK; Google's
  `google-api-python-client`, `google-auth`, `google-auth-oauthlib`,
  `google-auth-httplib2`. Nothing else runs with your token.
- **Identity:** your own OAuth *Desktop* client from your own GCP project.
  Consent is granted to *you*, not to any vendor.
- **Scopes (minimum for the job, no Drive write):**
  `documents`, `spreadsheets`, `drive.readonly`.
- **Secrets:** `~/.gcp/client_secret.json` (path-referenced, never printed) and
  `~/.gcp/gws-mcp-token.json` (per machine, 0600). Both `.gitignore`d by pattern.
- **Auth is explicit** (`gws-mcp auth`) and blocking on an ephemeral loopback
  port — there is no "auth on first tool call", so the "localhost refused to
  connect after consent" failure mode cannot occur.
- Kill switch: `gws-mcp revoke`, or https://myaccount.google.com/permissions.

## One-time per Google account (manual, ~5 min)

Google offers no CLI for an External consent screen + Desktop client on a
personal account, so:

1. https://console.cloud.google.com/projectcreate → project (e.g. `carlos-mcp`).
2. Enable **Google Docs API**, **Google Sheets API**, **Google Drive API**
   (`…/apis/library/{docs,sheets,drive}.googleapis.com?project=<id>`).
3. https://console.cloud.google.com/auth/overview → *Get started*: External,
   your email as support/contact, tick the User Data Policy box, Create.
4. `…/auth/audience` → *Test users* → add your email.
   Then **Publish app** (else the refresh token dies every 7 days).
5. `…/auth/clients/create` → **Desktop app** → Create → **Download JSON**
   (only chance) → save as `~/.gcp/client_secret.json`.

The same client JSON works on every machine; only the token is per machine.

## Install on a machine

This server lives in the dangler repo as `extensions/google`, so it arrives with
a clone of dangler. From that directory:

```bash
uv sync
uv run gws-mcp auth        # browser consent, blocks until done
uv run gws-mcp doctor      # secret / token / APIs
```

Then list it in `dangler.toml` rather than registering it with the client
directly — the point of the fleet is that one registration fronts everything:

```toml
[servers.google]
command = "uv"
args = ["run", "--directory", "<repo>/extensions/google", "gws-mcp"]
identity = "your Google account — Docs + Sheets read/write, Drive read-only"
setup_hint = "OAuth desktop client at ~/.gcp/client_secret.json, then `uv run --directory <repo>/extensions/google gws-mcp auth`"
```

`dangler warm` should then report this server's tools cached. To run it
standalone instead, the direct registration still works:
`claude mcp add --scope user gws-mcp -- uv run --directory <repo>/extensions/google gws-mcp`.

Env overrides: `GWS_MCP_DIR` (default `~/.gcp`), `GWS_MCP_CLIENT_SECRET`,
`GWS_MCP_TOKEN`.

## Tools

| Docs | Sheets | Drive (read-only) |
|---|---|---|
| `docs_get` plain text | `sheets_info` tabs/dims | `drive_search` |
| `docs_outline` paragraphs + indexes | `sheets_read` range → rows | `drive_get` metadata |
| `docs_replace` find/replace all | `sheets_write` overwrite range | `drive_export` doc/sheet → pdf/docx/xlsx/csv/… |
| `docs_insert` at index / end | `sheets_append` rows | |
| `docs_delete_range` | `sheets_clear` | |
| `docs_create` | `sheets_find` text → cells | |
| `docs_batch_update` raw API | `sheets_add_sheet`, `sheets_create` | |
| | `sheets_batch_update` raw API | |

Agent rule: read with `docs_outline`/`sheets_read` first, then edit by index or
A1 range. Escape hatches (`*_batch_update`) take raw Google API request lists.

## CLI

```
gws-mcp            # serve (stdio)
gws-mcp auth       # consent → ~/.gcp/gws-mcp-token.json
gws-mcp whoami
gws-mcp doctor
gws-mcp revoke
```

## Layout

```
pyproject.toml
src/gws_mcp/__init__.py
src/gws_mcp/auth.py      scopes, token load/refresh, interactive auth, revoke
src/gws_mcp/server.py    FastMCP tools + CLI
```
