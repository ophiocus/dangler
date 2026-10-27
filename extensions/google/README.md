# gws-mcp

Self-owned MCP server giving Claude Code **full read/write on Google Docs and
Google Sheets**, **read-only Google Drive** (search, metadata, export) and
**Gmail drafts** (create, replace, read back, delete — never send) — so files
are edited **in place** (same id, same URL, revision history intact) instead of
recreated to iterate, and drafts carry exactly the links they were given.

Supersedes the claude.ai Google Drive connector for anything that isn't pure
reading (that connector's `update_file` is metadata-only and it has no Docs
body / Sheets cell API), and the claude.ai Gmail connector for any draft that
contains a link or an attachment: that connector's `create_draft` stores every
link — URL, link object, even a bare domain — as a
`https://www.google.com/url?q=…&source=gmail&ust=…` redirect in both MIME parts,
and recipients receive the redirect (observed 2026-10-27; why it does so is
unverified). Here the message is built locally with Python's `email` package and
stored as raw RFC 822, so nothing rewrites it.

## Trust model — no third-party server code

- **Our code:** `src/gws_mcp/` (~400 lines: `auth.py`, `server.py`).
- **Dependencies (5, all vendor-official):** Anthropic's `mcp` SDK; Google's
  `google-api-python-client`, `google-auth`, `google-auth-oauthlib`,
  `google-auth-httplib2`. Nothing else runs with your token.
- **Identity:** your own OAuth *Desktop* client from your own GCP project.
  Consent is granted to *you*, not to any vendor.
- **Scopes (minimum for the job, no Drive write):**
  `documents`, `spreadsheets`, `drive.readonly`, `gmail.compose`, `gmail.readonly`.
  - `gmail.compose` creates, replaces, reads and deletes drafts. Google's scope
    also permits sending; **this server has no send tool**, by design — sending
    is the account owner's act, done in Gmail.
  - `gmail.readonly` is used by one call only: looking up the message a reply
    draft answers (`Message-ID`, `References`, `Subject`, `threadId`), which
    `gmail.compose` cannot read. Without those headers the draft does not land
    in the thread.
  - Both are Google *restricted* scopes. Fine for a personal app; see
    "Consent screen" below.
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
2. Enable **Google Docs API**, **Google Sheets API**, **Google Drive API**,
   **Gmail API**
   (`…/apis/library/{docs,sheets,drive,gmail}.googleapis.com?project=<id>`).
   A disabled Gmail API shows up as `Google API error 403: Gmail API has not
   been used in project …`, and `gws-mcp doctor` fails on its mailbox check.
3. https://console.cloud.google.com/auth/overview → *Get started*: External,
   your email as support/contact, tick the User Data Policy box, Create.
4. `…/auth/audience` → *Test users* → add your email.
   Then **Publish app** (else the refresh token dies every 7 days).
5. `…/auth/clients/create` → **Desktop app** → Create → **Download JSON**
   (only chance) → save as `~/.gcp/client_secret.json`.

The same client JSON works on every machine; only the token is per machine.

### Consent screen: what to expect

- **"Google hasn't verified this app."** Gmail scopes are restricted, and a
  personal project is never verified for them. If Google shows this page,
  choose *Advanced* → *Go to <app> (unsafe)*: the app is your own client, so
  you are granting access to yourself. An app in **Testing** only lets the
  listed test users through at all.
- **Testing vs. In production.** In Testing, the refresh token dies after 7
  days and `gws-mcp auth` must be re-run weekly. Publishing needs the Branding
  page complete (Google greys out *Publish app* otherwise). Publishing an
  unverified app with restricted scopes keeps working for up to 100 users,
  each of whom sees the warning above.
- State of `carlos-mcp` (`carlos-mcp-505719`), read in the console on
  2026-10-27: publishing status **Testing**, user type External, test users
  `csantanad@gmail.com` and `tecnocraticaservices@gmail.com`; *Publish app*
  greyed out ("you must complete your configuration on the Branding page");
  Branding says "Verification is not required since your app is in Testing
  status". The Gmail API was not yet enabled that day.

### Adding a scope to an existing token

Tokens load with the scopes they were granted, so a token minted before
the Gmail scopes still drives Docs, Sheets and Drive. The Gmail tools answer
"…was granted before gmail.compose was added. Run `gws-mcp auth` again…", and
`gws-mcp doctor` prints `PROBLEM: token lacks …`. Re-running
`uv run --directory <repo>/extensions/google gws-mcp auth` replaces the token
with one carrying every scope in `auth.SCOPES`; then `drop_server` /
`load_server` the `google` entry in dangler (or restart the client) so the
running server picks it up.

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
identity = "your Google account — Docs + Sheets read/write, Drive read-only, Gmail drafts (no send)"
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

### Gmail drafts (no send)

| Tool | Does |
|---|---|
| `gmail_draft_create` | `to`, `cc`, `bcc` (string or list), `subject`, `text` and/or `html`, `attachments` (local paths, read by the server, MIME type guessed from the name), `reply_to_message_id`. Returns `{draft_id, message_id, thread_id}` |
| `gmail_draft_update` | `draft_id` + the same arguments; **replaces** the whole draft (nothing carried over). The message id changes, the draft id does not |
| `gmail_draft_get` | the draft as stored: headers, `text`, `html`, `attachments` (name, type, size), every `links` URL found, and `redirect_wrapped_links` — any link stored as a `google.com/url?…` redirect, which should always be `[]` |
| `gmail_draft_delete` | deletes a draft (permanent; drafts skip Trash) |

MIME: `text` only → `text/plain`; `html` only → `text/html`; both →
`multipart/alternative`; attachments wrap that in `multipart/mixed`. UTF-8
throughout. Messages over 4 MB go up as a media upload (Gmail's ceiling is
35 MB).

Replies: give `reply_to_message_id` (the Gmail message id, as in
`mail.google.com/…/#inbox/<id>` or the Gmail connector's message ids). The
server reads the original's `Message-ID`, `References`, `Subject`, `From`,
`Reply-To` and `threadId`, sets `In-Reply-To` and `References`, files the
draft in the thread, and fills `subject` ("Re: <original>") and `to` (the
original's Reply-To, else From) when you leave them empty.

## Tests

```bash
uv run python -m unittest discover -s tests -v   # offline MIME tests, no token
uv run python tests/smoke.py                     # live, read-only
uv run python tests/smoke.py --gmail             # live: draft to csantanad@gmail.com, read back, replace, delete
uv run python tests/smoke.py --write             # live: throwaway Doc + Sheet (trash by hand)
```

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
src/gws_mcp/gmail.py     draft MIME building and reading (pure, no network)
tests/test_gmail.py      offline MIME tests
tests/smoke.py           live stdio smoke test
```
