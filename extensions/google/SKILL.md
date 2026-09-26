---
name: google-workspace
description: Read and edit Google Docs and Google Sheets in place, and search or export files on Google Drive (read-only), as the signed-in Google account — through the `google` server in the dangler fleet. Use whenever a task names a Doc, a Sheet or a Drive file, e.g. "update the budget sheet", "replace the intro paragraph in this doc", "append these rows", "find the contract in Drive", "export that doc as text". This is the WRITE path for Docs and Sheets; never recreate a document to change it.
---

# google-workspace — the Docs / Sheets / Drive name tag

The `google` fleet server (`gws-mcp`, own code, vendor-official SDKs only) edits Docs and
Sheets **in place** — same id, same URL, revision history intact — and reads Drive. Reach it
through dangler:

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

## Rules

- **Edit in place.** `docs_replace` / `sheets_write` on the existing file. Creating a fresh
  copy to "update" something loses the link everyone has and the revision history.
- Read the target range or outline first, then write the narrowest change. Say what will
  change before a write that touches more than a few cells or paragraphs.
- Scopes are `documents`, `spreadsheets`, `drive.readonly` — there is no Drive write, so
  moving, sharing or deleting files is not available here by design.
- A "no token" error means this machine has not run `uv run --directory <dangler>/extensions/google gws-mcp auth`;
  the server's `setup_hint` says so. Auth is explicit and blocking — never hand the user a
  consent URL, tell them to run that command.
- Ids come from URLs: `docs.google.com/document/d/<id>/…`, `docs.google.com/spreadsheets/d/<id>/…`.
