---
name: oidoenvivo-mail
description: Draft, read and send Oído en Vivo's outreach mail as «Oído en Vivo <info@oidoenvivo.com>» — the Workspace mailbox where every oidoenvivo.com alias lands — through the `oidoenvivo-mail` server in the dangler fleet (the google extension's code running as that mailbox). Use for every Oído en Vivo outreach draft or send, for listing the mailbox's drafts, and for reading replies and bounces that came back to the aliases. Sends an existing draft only, and only from envío central (the outreach octopus), which sends finished, rule-following drafts at warm-up pace under Carlos's standing approval; every other session drafts and hands off.
---

# oidoenvivo-mail — the Oído en Vivo mailbox name tag

The `oidoenvivo-mail` fleet entry is the `google` extension (`I:\dangler\extensions\google`, `gws-mcp`) started a
second time with another identity: the Workspace mailbox that receives the oidoenvivo.com aliases (its address is
`GWS_MCP_LOGIN_HINT` in the entry's env in `~/.dangler/dangler.toml`), Gmail scopes only (`gmail.compose`,
`gmail.readonly`), default From `Oído en Vivo <info@oidoenvivo.com>`. Same tools and schemas as `google`; read that
skill for the tool table. Reach it with `load_server {name: "oidoenvivo-mail"}` and `call_tool`.

## Rules paid for in use
- **From is info@oidoenvivo.com**, a verified send-as of the mailbox (DKIM `d=oidoenvivo.com`, DMARC pass since
  2026-11-01). `gmail_draft_create` refuses a sender that is not the mailbox's own address or a verified send-as.
- **Send = `gmail_draft_send {draft_id}` on an existing, checked draft.** Before each: `gmail_draft_get` and check
  `headers.From`, the recipients and `redirect_wrapped_links == []`.
- **Who sends:** the envío central session (the outreach octopus) only
  (`I:\oidoenvivo\outreach\salida-es-419\envio-central\`). Every other session drafts and hands the draft to it. Log
  every send with `python I:/oidoenvivo/outreach/tools/envio_registrar.py <draft_id>`.
- **Sending needs no per-draft approval** (Carlos, 2026-11-04: «stop asking to send, tell all sessions to stop asking
  to send»). Envío central sends a finished draft at warm-up pace when it follows every rule: no blank field or
  placeholder; the address was seen on the counterpart's own published channel; it goes from the thread's origin
  account; it offers no prices, audience figures or invented products. A draft that breaks a rule is fixed, never
  sent and never asked about. Reports say what is ready or what went out, never "may I send". Pacing rules and the
  holds Carlos has set still apply (e.g. no replies to the Ministry or FUGA yet). Source: oidoenvivo-outreach memory
  `sends-need-no-approval`.
- **A bounce is registered and tagged, and sending moves on** (Carlos, 2026-11-02: «REGISTER, TAG, MOVE ON»).
  Re-running a send over drafts already sent is a no-op: a sent draft no longer exists.
- Driving the mailbox's Gmail web page to send in bulk was blocked by Claude Code's auto-mode classifier
  («Auto-Mode Bypass», 2026-11-02); this API entry is the route for sending.

## Identity files (by reference, never read)
- OAuth client: `~/.gcp/client_secret.json` (the same desktop client as `google`, GCP project carlos-mcp).
- Token: `~/.gcp/tecnocratica/gws-mcp-token.json`, minted by running
  `uv run --directory I:/dangler/extensions/google gws-mcp auth` with the entry's env from `dangler.toml`
  (`GWS_MCP_TOKEN`, `GWS_MCP_SCOPES=gmail.compose,gmail.readonly`, `GWS_MCP_LOGIN_HINT`). The consent is granted in
  the Chrome profile signed in as the mailbox (Profile 14 on Desky); the entry's `setup_hint` repeats this.
