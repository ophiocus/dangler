# Ingestion — a `.mcp.json` is a ticket, not a configuration

**Rule (owner, 2026-09-20).** dangler is the funnel for *all* MCP usage. Whenever a
`.mcp.json` is found, the service it targets is to be wrapped by dangler, and the file
then stops registering that service. The only MCP registration a client should hold is
`dangler` itself.

Why this is a rule and not a tidy-up: an extension is also the **keychain**. It names an
`identity`, holds its secrets by reference (a credentials file, a per-machine token, a
bearer read out of `~/.claude.json` and never copied), and owns the service's lifecycle.
A project `.mcp.json` has none of that. It is where tokens end up in repositories, where
retired servers keep failing to connect for months, and where two projects silently
claim the same port.

## The trigger

Finding a `.mcp.json` — while exploring a repo, stamping a project, or running the
census — opens an ingestion ticket. Do not edit the file to "fix" it. Ingest its target.

```
python scripts/mcp_census.py            # every registration on the dev drives, one verdict each
python scripts/mcp_census.py --quiet-skips
```

Exit status 1 while any ticket is open, 0 when the machine is clean. Values are never
printed; secrets are reported by key name and shape only, and a literal in a tracked
file is flagged as a credential in a repository.

## The verdicts, and what each one costs

| Verdict | Meaning | Work |
|---|---|---|
| `DELETE` | The target is retired. | Remove the entry. No wrapping. |
| `REMOVE` | A fleet entry already serves it. | Remove the entry. |
| `LIST` | A server dangler can front as-is — a stdio server we do not own, or a hosted HTTP endpoint. | Add it to `dangler.toml`: `command` + `args` for stdio, or `url` + `header_file` (static bearer) / `auth = "oauth"` for HTTP. Always an `identity` and a `setup_hint`. Move every secret to a referenced file. Zero code. |
| `WRAP` | A service that needs lifecycle, a bundled toolkit, or an identity dangler must supply. | A first-party extension under `extensions/<name>/`, by the house rules in `architecture.md`. |

**Narrowed 2026-09-21.** `WRAP` used to catch every HTTP target, because dangler could
only front stdio children. HTTP downstream shipped, so a hosted endpoint is now an
ordinary `LIST` — `myevery` was the first, and took two config lines and no code. Only
reach for an extension when a plain request is genuinely not enough.
| `SKIP` | Not ours: plugin marketplaces, vendored trees, anything in `~/.dangler/census.skip`. | None. Scope is the owner's call, one path fragment per line. |

## The method — five gates, in order

An ingestion is not done when the extension works. It is done when the last gate passes.

1. **Roster first.** If a project family has a canonical roster for its MCP servers, the
   new fleet entry is proposed there before any code. A wrapper nobody agreed to is a
   second source of truth.
2. **Build or list.** Extension (`WRAP`) or a `dangler.toml` entry (`LIST`). It must start
   and answer `tools/list` with nothing configured, carry an `identity`, and have a
   read-only switch if the wrapped thing writes.
3. **Prove parity through dangler.** Run, over `call_tool`, the same calls the direct
   registration was used for, and record the output. Until this passes the `.mcp.json`
   stays exactly as it is. Removing the old path before the new one is proven is how a
   project loses its tools on a Friday.
4. **Retire the registration — every copy of it.** Remove the entry from the `.mcp.json`;
   delete the file if it is now empty. Then remove everything that would *put it back*:
   - any generator, stamper or sync step that writes the file into new or existing projects;
   - any health check that asserts the file exists — invert it, so the check FAILS when a
     project registers a server directly;
   - any doc or setup guide that tells a reader to create it;
   - vendor tooling that regenerates it (a "generate client config" command, a session
     hook that nags when the file is missing) — document that the nag is expected.
   A `.mcp.json` is Claude Code configuration. It is changed on the owner's word, per
   repository, never as a side effect and never because another session asked.
5. **The assurance.** `python scripts/mcp_census.py --quiet-skips` reports no ticket for
   that service and the inverted health check passes in every project of the family.
   That exit code is the proof. Record it with the date.

## What the census found when the rule was written (2026-09-20, Desky)

14 open tickets, 0 literal secrets.

| Service | Registrations | Verdict |
|---|---|---|
| Epic Unreal MCP (`127.0.0.1:8000/mcp`) | 4 projects, all the same URL on the same port | `WRAP` — extension `unreal` |
| GenerativeAISupport bridge | 1 project + 2 worktree copies | `DELETE` — retired |
| myevery bus | 1 (bearer is a `${…}` placeholder) | `WRAP` — extension `myevery` |
| A provider hub with five API-key env slots (all empty) | 1, tracked | `LIST`, and the slots become file references |
| Hosted SaaS servers (OAuth) | several, in course and client trees | owner to scope: `SKIP` or wait for HTTP downstream + auth passthrough |

Not covered by the census then, and now covered: user-level and project-level
registrations in `~/.claude.json` are read too (`--client`), so the client's own
configuration gets the same verdicts. claude.ai connectors stay outside any local funnel.

## Ingestion record — `myevery` (2026-09-21, Desky)

The first HTTP ingestion, kept as the worked example of the five gates.

| Gate | What happened |
|---|---|
| 1 · Roster | The bus's own docs are the roster; `docs/SEAT-SETUP-CYPHER.md` and `README.md` in the myevery repo were the thing to change, and were changed to name the fleet entry. |
| 2 · Build or list | `LIST`, not `WRAP`: `[servers.myevery]` with `url` + `header_file = ~/.dangler/myevery.headers`, `identity`, `setup_hint`. No code. The bearer was moved from the client config into that file, read once and never printed. |
| 3 · Parity | Through `call_tool` against the new binary: 16 tools dangled by `load_server`, `list_seats` and `list_nodes` read, `post_event` written and read back by `query_events` (seq 29). |
| 4 · Retire | `claude mcp remove myevery -s user`; `I:\myevery\.mcp.json` deleted; `.mcp.json.example` deleted and the README's "Connecting a seat" section inverted to the dangler route; `docs/PRODUCT.md`'s onboarding item now writes a fleet entry and is told never to write a `.mcp.json`. Two consumers read the bearer out of the retired registration and were repointed at the header file: the `comfy` extension's bus transport and the AIProd gateway's `bus.json` (`token_from`). |
| 5 · Assurance | `python scripts/mcp_census.py --quiet-skips` reports no ticket for myevery. It still exits 1 for the other open tickets on this machine, which is the tool working, not a failure of this ingestion. |
