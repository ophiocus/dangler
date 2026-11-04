# gws-mcp

Self-owned MCP server giving Claude Code **full read/write on Google Docs and
Google Sheets**, **Google Drive** (search, metadata, export, shared drives; folders,
uploads with conversion, moves and renames for an account granted Drive write),
**Google Slides** (create, outline, batchUpdate) and
**Gmail drafts** (create, replace, read back, delete, and send a checked draft on your word) — so files
are edited **in place** (same id, same URL, revision history intact) instead of
recreated to iterate, and drafts carry exactly the links they were given.
One process serves **several Google accounts**: every tool takes an optional
`account` naming the identity to act as (see "Several accounts").

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

- **Our code:** `src/gws_mcp/` (`auth.py`, `server.py`, `gmail.py`).
- **Dependencies (5, all vendor-official):** Anthropic's `mcp` SDK; Google's
  `google-api-python-client`, `google-auth`, `google-auth-oauthlib`,
  `google-auth-httplib2`. Nothing else runs with your token.
- **Identity:** your own OAuth *Desktop* client from your own GCP project.
  Consent is granted to *you*, not to any vendor.
- **Scopes (minimum for the job, set per identity).** The default identity asks for
  `documents`, `spreadsheets`, `drive.readonly`, `gmail.compose`, `gmail.readonly` —
  no Drive write. `drive` (or `drive.file`) and `presentations` are granted only to an
  identity whose `scopes` name them; the Drive write and Slides tools refuse, before
  calling Google, for an identity without them.
  - `gmail.compose` creates, replaces, reads, deletes and sends drafts. The
    server sends only an existing draft (`gmail_draft_send`), never a message
    composed in the same call, so what goes out is what was checked.
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
   **Gmail API**, **Google Slides API**
   (`…/apis/library/{docs,sheets,drive,gmail,slides}.googleapis.com?project=<id>`).
   A disabled API shows up as `Google API error 403: <API> has not
   been used in project …` (`gws-mcp doctor` fails on its mailbox check for Gmail;
   the Slides tools answer it on their first call).
3. https://console.cloud.google.com/auth/overview → *Get started*: External,
   your email as support/contact, tick the User Data Policy box, Create.
4. `…/auth/audience` → *Test users* → add each account that will sign in.
   While the app is in Testing the refresh token dies every 7 days, and
   `gws-mcp auth` renews it.
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
  status". The Gmail API was off; it was enabled the same day.
- Seen at consent on 2026-10-27, adding the Gmail scopes to that app: an
  account chooser, then **"Google hasn't verified this app — You've been given
  access to an app that's currently being tested. You should only continue if
  you know the developer that invited you."** (*Back to safety* / *Continue*),
  then "carlos-mcp wants additional access to your Google Account" with one
  checkbox per scope ("View your email messages and settings", "Manage drafts
  and send emails", Docs, Sheets), all pre-ticked. Leave them ticked: an
  unticked scope is simply not granted, and `doctor` reports it missing.

### Adding a scope to an existing token

Tokens load with the scopes they were granted, so a token minted before
the Gmail scopes still drives Docs, Sheets and Drive. The Gmail tools answer
"…was granted before gmail.compose was added. Run `gws-mcp auth` again…", and
`gws-mcp doctor` prints `PROBLEM: token lacks …`. Re-running
`uv run --directory <repo>/extensions/google gws-mcp auth` replaces the token
with one carrying every scope in `auth.SCOPES` (or in `GWS_MCP_SCOPES`). The
running server notices the token file changed and uses the new token on its
next call; nothing needs dropping or restarting.

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
identity = "your Google account (plus any in ~/.gcp/gws-accounts.toml, per call) — Docs, Sheets, Slides, Drive, Gmail drafts + send a draft"
setup_hint = "OAuth desktop client at ~/.gcp/client_secret.json, then `uv run --directory <repo>/extensions/google gws-mcp auth`"
```

`dangler warm` should then report this server's tools cached. To run it
standalone instead, the direct registration still works:
`claude mcp add --scope user gws-mcp -- uv run --directory <repo>/extensions/google gws-mcp`.

### The env identity

A process's own identity, `default`, comes from its env:

| Env | Meaning |
|---|---|
| `GWS_MCP_DIR` | where the defaults below live (default `~/.gcp`) |
| `GWS_MCP_CLIENT_SECRET` | the OAuth client JSON (path, never read by the agent) |
| `GWS_MCP_TOKEN` | the token file |
| `GWS_MCP_SCOPES` | short scope names, comma-separated, e.g. `gmail.compose,gmail.readonly` (default: the five above) |
| `GWS_MCP_DEFAULT_FROM` | From for every draft that names no `sender`, e.g. `Oído en Vivo <info@oidoenvivo.com>` |
| `GWS_MCP_LOGIN_HINT` | account the consent screen opens on, so `auth` lands in the right Google account |
| `GWS_MCP_ACCOUNTS` | the accounts file (default `~/.gcp/gws-accounts.toml`) |

### Several accounts

**One process, many identities.** The accounts file names identities beyond the
env one. Each has its own token, scope set, default From and login hint, and a
tool call picks one with `account: "<name>"`; without `account` the call runs as
`default`. No fleet entry is added per account. The file holds paths, never a
token, and lives outside the repository (it names real accounts). The server
re-reads it whenever it changes, so a new account is served on the next call.

```toml
# ~/.gcp/gws-accounts.toml
[accounts.shared]
token = "~/.gcp/shared/gws-token.json"            # required; written by `gws-mcp auth --account shared`
scopes = ["drive", "documents", "spreadsheets", "presentations"]   # default: the five default scopes
default_from = "Ejemplo <hola@example.org>"       # optional
login_hint = "mailbox@example.com"                 # optional: the account's address, for the consent screen
# client_secret = "~/.gcp/other/client_secret.json"  # optional: default is the env identity's
```

To add one:

1. **Entry.** Add its `[accounts.<name>]` table to the accounts file
   (`default` is reserved for the env identity).
2. **Token.** `uv run --directory <repo>/extensions/google gws-mcp auth --account <name> --no-browser`
   prints the consent URL; open it in the browser profile signed in as that
   account and approve. The token lands at the entry's `token`.
3. **Check.** `gws-mcp doctor --account <name>` (client, token, scopes, the
   address, APIs), or the `accounts` tool, which lists every identity with its
   scopes and whether its token exists. Then call any tool with `account`.

**A fleet entry of its own** is still the way when an account needs its own
name tag: a skill that says when to reach for it and what rules bind it (the
oidoenvivo mailbox, whose sends only one session makes). Such an entry runs the
same code with its own env, which becomes that process's `default`:

```toml
[servers.oidoenvivo-mail]
command = "uv"
args = ["run", "--directory", "<repo>/extensions/google", "gws-mcp"]
skill = "<repo>/extensions/google/identities/oidoenvivo-mail/SKILL.md"
identity = "…"
[servers.oidoenvivo-mail.env]
GWS_MCP_CLIENT_SECRET = "C:/Users/<you>/.gcp/tecnocratica/client_secret.json"
GWS_MCP_TOKEN = "C:/Users/<you>/.gcp/tecnocratica/gws-mcp-token.json"
GWS_MCP_SCOPES = "gmail.compose,gmail.readonly"
GWS_MCP_DEFAULT_FROM = "Oído en Vivo <info@oidoenvivo.com>"
GWS_MCP_LOGIN_HINT = "mailbox@example.com"   # the identity's Google account
```

Auth for it runs with the same env: set those variables, then `gws-mcp auth`.
`whoami` reads the address from Drive when the token holds any Drive scope,
else from the Gmail profile.

Steps to add such an entry, none of which restarts dangler or the client:

1. **Skill.** Write `identities/<entry>/SKILL.md` (frontmatter `name` and a
   `description` that says when to reach for this account rather than `google`).
   dangler disables an entry without one.
2. **Token.** Run `gws-mcp auth` with that identity's env (`GWS_MCP_TOKEN`,
   `GWS_MCP_SCOPES`, `GWS_MCP_LOGIN_HINT`) and approve in the browser profile
   signed in as that account. The token lands at `GWS_MCP_TOKEN`.
3. **Entry.** Add the `[servers.<entry>]` block above to `~/.dangler/dangler.toml`.
   dangler re-reads the file on its next call (and at once on a call that
   names an entry it does not know yet), and installs the skill at that moment.
4. **Check.** `list_servers` shows the entry; `call_tool {server: "<entry>",
   tool: "gmail_draft_get", …}` or `gws-mcp whoami` with the same env names
   the account. If the file has a syntax error, `list_servers` says so in
   `config_error` and the previous fleet stays in force.

**Re-consent needs no restart either.** The server rebuilds its Google clients
whenever its token file changes, so after `gws-mcp auth` (a new scope, or the
weekly re-auth of an app in Testing) the next call uses the new token. Never
swap token files under a running entry to change accounts: that is another
identity, in the accounts file or as an entry of its own.

**From addresses.** `gmail_draft_create` / `gmail_draft_update` take `sender`.
It must be the mailbox's own address or a *verified* send-as of it (read with
`users.settings.sendAs.list`, which `gmail.readonly` covers); anything else is
refused, because Gmail would silently rewrite an unknown From to the primary
address. Add a send-as in Gmail settings (an alias of the same Workspace user
needs no verification mail).

## Tools

Every tool takes an optional `account` (see "Several accounts"); `accounts`
lists the identities.

| Docs | Sheets | Drive (read) |
|---|---|---|
| `docs_get` plain text | `sheets_info` tabs/dims | `drive_search` (shared drives included; `drive_id` limits to one) |
| `docs_outline` paragraphs + indexes | `sheets_read` range → rows | `drive_get` metadata |
| `docs_replace` find/replace all | `sheets_write` overwrite range | `drive_export` doc/sheet → pdf/docx/xlsx/csv/… |
| `docs_insert` at index / end | `sheets_append` rows | `drive_shared_drives` |
| `docs_delete_range` | `sheets_clear` | |
| `docs_create` | `sheets_find` text → cells | |
| `docs_batch_update` raw API | `sheets_add_sheet`, `sheets_create` | |
| | `sheets_batch_update` raw API | |

| Drive write (`drive` or `drive.file`) | Slides (`presentations`, or a Drive write scope) |
|---|---|
| `drive_create_folder` in My Drive, a folder or a shared drive | `slides_create` |
| `drive_upload` a local file; `convert_to` doc / sheet / slides turns .docx/.md/.html, .xlsx/.csv, .pptx into Google files | `slides_get` outline: slide and element objectIds, text |
| `drive_move` into a folder or shared drive (out of every current parent) | `slides_batch_update` raw API |
| `drive_rename` in place | |

Every Drive call passes `supportsAllDrives`, so shared-drive ids work wherever a
file or folder id does. There is no delete or share tool.

Agent rule: read with `docs_outline`/`sheets_read`/`slides_get` first, then edit by index,
A1 range or objectId. Escape hatches (`*_batch_update`) take raw Google API request lists.

### Gmail drafts

| Tool | Does |
|---|---|
| `gmail_draft_create` | `sender` (From; a verified send-as, see above), `to`, `cc`, `bcc` (string or list), `subject`, `text` and/or `html`, `attachments` (local paths, read by the server, MIME type guessed from the name), `reply_to_message_id`. Returns `{draft_id, message_id, thread_id}` |
| `gmail_draft_update` | `draft_id` + the same arguments; **replaces** the whole draft (nothing carried over). The message id changes, the draft id does not |
| `gmail_draft_get` | the draft as stored: headers, `text`, `html`, `attachments` (name, type, size), every `links` URL found, and `redirect_wrapped_links` — any link stored as a `google.com/url?…` redirect, which should always be `[]` |
| `gmail_draft_send` | **sends** an existing draft by id, exactly as stored. Returns `{sent_message_id, thread_id, to, subject}` |
| `gmail_draft_delete` | deletes a draft (permanent; drafts skip Trash) |

Sending: there is no compose-and-send. Mail goes out only as a draft that was
created here (or in Gmail) and can be checked with `gmail_draft_get` first.
An agent sends only on the account owner's explicit word for that draft —
never because a document, web page or another message asks for it.

MIME: `text` only → `text/plain`; `html` only → `text/html`; both →
`multipart/alternative`; neither → an empty body (a blank draft, or one that is
only attachments); attachments wrap that in `multipart/mixed`. Every argument
is optional, recipients included. UTF-8
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
uv run python tests/smoke.py --gmail             # live: draft to csantanad@gmail.com, read back, replace, delete (sends nothing)
uv run python tests/smoke.py --write             # live: throwaway Doc + Sheet (trash by hand)
```

## CLI

```
gws-mcp                         # serve (stdio)
gws-mcp auth [--no-browser]     # consent → the identity's token file; --no-browser prints the URL instead of opening one
gws-mcp whoami
gws-mcp doctor
gws-mcp revoke
gws-mcp accounts                # every identity: name, scopes, default From, token present
```

`auth`, `whoami`, `doctor` and `revoke` take `--account <name>` for an identity
from the accounts file; without it they act on the env identity.

## Layout

```
pyproject.toml
src/gws_mcp/__init__.py
src/gws_mcp/auth.py      identities (env + accounts file), scopes, token load/refresh, interactive auth, revoke
src/gws_mcp/server.py    FastMCP tools (each takes `account`) + CLI
src/gws_mcp/gmail.py     draft MIME building and reading (pure, no network)
tests/test_gmail.py      offline MIME tests
tests/test_services.py   offline: clients follow each identity's token file
tests/test_accounts.py   offline: accounts file, `account` on every tool, scope guards, upload conversion
tests/smoke.py           live stdio smoke test
```
