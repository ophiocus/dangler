---
name: myevery
description: Coordinate across Carlos's Claude seats (Desky, CYPHER, forge) over the myevery bus — post an event to the shared stream, query it by relevance, poll this seat's inbox, register or list seats, set subscription tags, promote a workstream into a durable persona — through the `myevery` server in the dangler fleet. Use for anything cross-machine, e.g. "tell CYPHER…", "what did the other seat leave for me", "is Desky online", "leave a note for the next session on the laptop". Seat-to-seat transfers of files or jobs ride the bus's piped transport, never a store.
---

# myevery — the cross-seat bus name tag

The `myevery` fleet entry is a hosted MCP endpoint (`url` + `header_file`, no process);
the bearer stays in the header file and is never copied. Reach it through dangler:

1. `load_server {name: "myevery"}` once, to see the schemas.
2. `call_tool {server: "myevery", tool: "poll_inbox", arguments: {…}}`.

## Which tool

| Need | Tool |
|---|---|
| leave something for another seat / the stream | `post_event` |
| what happened, by relevance and tags | `query_events` |
| what is waiting for *this* seat | `poll_inbox` |
| announce this seat, see who is on | `register_seat`, `list_seats` |
| what this consumer wants to hear about | `subscribe` |
| turn a long-running workstream into a named actor | `promote_thread`, `list_personas` |

## Rules

- **Pipes, not stores (Carlos, 2026-09-19).** Anything moved between seats — an image, a
  job, a skill folder — goes through `/pipe/<channel>` (sender POSTs, receiver GETs, the
  server splices the streams and keeps nothing). `post_event` is for coordination text,
  not payloads. Do not design a new bus feature that persists data on the server.
- The seat names are canon: `Desky` (desktop), `CYPHER` (laptop), `forge` (WSL VM). Sign
  cross-seat messages with this seat's name.
- Events are durable and readable by every seat on the account: no secrets, no tokens, no
  private prompt text in an event body.
- An auth error means this machine's header file is not filled; the server's `setup_hint`
  names it. Check the service itself with `curl https://api.myevery.tecnocratica.com.co/healthz`
  before suspecting the token.
- The generated-image path between seats is not this server: it is the `comfy` server
  with `AIPROD_TRANSPORT=bus`, which uses the pipes underneath.
