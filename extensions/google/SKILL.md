---
name: google-workspace
description: Read and edit Google Docs and Google Sheets in place, search or export files on Google Drive (read-only), and create Gmail drafts whose links are stored exactly as given (with attachments and in-thread replies) — as the signed-in Google account, through the `google` server in the dangler fleet. Use whenever a task names a Doc, a Sheet or a Drive file, e.g. "update the budget sheet", "replace the intro paragraph in this doc", "append these rows", "find the contract in Drive", "export that doc as text"; and for every email draft that carries a link or an attachment, e.g. "draft the outreach email", "prepare a reply to this thread with the PDF". This is the WRITE path for Docs and Sheets; never recreate a document to change it. It never sends mail.
---

# google-workspace — the Docs / Sheets / Drive / Gmail-drafts name tag

The `google` fleet server (`gws-mcp`, own code, vendor-official SDKs only) edits Docs and
Sheets **in place** — same id, same URL, revision history intact — reads Drive, and writes
Gmail **drafts**. Reach it through dangler:

1. `load_server {name: "google"}` once, to see the schemas.
2. `call_tool {server: "google", tool: "sheets_read", arguments: {spreadsheet_id: "…", range: "Sheet1!A1:D20"}}`.

Tools take no email argument: the token *is* the identity, and it is per machine.

## Which tool

| Need | Tool |
|---|---|
| read a Doc, or its heading outline to find where to edit | `docs_get`, `docs_outline` |
| change text | `docs_replace` (find → replace), `docs_insert`, `docs_delete_range`, `docs_batch_update` |
| new Doc | `docs_create` |
| read a Sheet | `sheets_info` (tabs, sizes), `sheets_read`, `sheets_find` |
| change cells | `sheets_write` (range), `sheets_append` (rows), `sheets_clear`, `sheets_batch_update` |
| new Sheet or tab | `sheets_create`, `sheets_add_sheet` |
| find or fetch a file | `drive_search`, `drive_get`, `drive_export` (Doc → text/markdown, Sheet → CSV) |
| email draft (new, or a reply in its thread), with links and attachments | `gmail_draft_create` |
| change a draft | `gmail_draft_update` (replaces it whole: pass every field again) |
| check a draft before the user sends it | `gmail_draft_get` |
| remove a draft this workflow made | `gmail_draft_delete` |

## Gmail drafts: when this server, not the claude.ai Gmail connector

- **Any draft with a link or an attachment goes through `gmail_draft_create` here.** The
  claude.ai Gmail connector's `create_draft` stores every link (URL, link object, even a bare
  domain) as a `https://www.google.com/url?q=…&source=gmail&ust=…` redirect in both the text and
  the HTML part, and the recipient gets the redirect (root-caused 2026-10-27; why it does it is
  unverified). This server builds the MIME itself and stores it raw, so links stay as written.
- The connector is still fine for **reading** mail (search, threads, message ids) and for
  link-free drafts. Its message ids are Gmail ids, usable as `reply_to_message_id` here.
- **Verify before handing over:** after creating or updating, call `gmail_draft_get` and check
  that `redirect_wrapped_links` is `[]` and that `links` holds the URLs you meant.
- **There is no send tool, by design.** Sending is the user's act, in Gmail. Report the draft
  (subject, recipients, draft id) and stop.
- Replies: pass the Gmail message id as `reply_to_message_id`; the server sets `In-Reply-To` /
  `References`, files the draft in the thread, and defaults the subject to "Re: <original>" and
  `to` to the original's Reply-To/From. Keep the "Re:" subject — Gmail threads on it.
- Send `html` and `text` together for outreach: the plain part is what text-only clients and many
  spam filters read. Attachments are local paths the server reads; check they exist first.
- `gmail_draft_delete` is permanent (drafts skip Trash). Delete only drafts this workflow made.

## Rules

- **Edit in place.** `docs_replace` / `sheets_write` on the existing file. Creating a fresh
  copy to "update" something loses the link everyone has and the revision history.
- Read the target range or outline first, then write the narrowest change. Say what will
  change before a write that touches more than a few cells or paragraphs.
- Scopes are `documents`, `spreadsheets`, `drive.readonly`, `gmail.compose`, `gmail.readonly`
  (the last only to look up the message a reply answers) — there is no Drive write, so moving,
  sharing or deleting files is not available here by design.
- A "no token" error, or one saying the token "was granted before gmail.compose was added",
  means this machine needs `uv run --directory <dangler>/extensions/google gws-mcp auth`; the
  server's `setup_hint` says so. Auth is explicit and blocking — never hand the user a consent
  URL; ask for their go, then run that command yourself and let them approve in the browser.
  Afterwards `drop_server` + `load_server` the `google` entry so the new token is used.
- While the OAuth app is in Testing, the token dies every 7 days; the same command renews it.
- Ids come from URLs: `docs.google.com/document/d/<id>/…`, `docs.google.com/spreadsheets/d/<id>/…`.
