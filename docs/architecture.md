# Architecture

## Dangler is an MCP server and an MCP client at once

```
                    upstream (one registration)        downstream (the fleet, lazy)
  ┌────────────┐   MCP stdio / (later) HTTP   ┌─────────┐   spawn-on-demand, stdio
  │ MCP client │ ───────────────────────────▶ │ dangler │ ───▶ mongodb-mcp-server
  │  (Claude)  │ ◀─────────────────────────── │         │ ───▶ google-drive-overdrive
  └────────────┘   5 meta-tools, tiny schema  └─────────┘ ───▶ …every other server
```

- **Upstream**: dangler serves MCP (rmcp `ServerHandler`, implemented *manually* — the tool
  surface is dynamic by nature, so no static `#[tool]` macros).
- **Downstream**: dangler is an MCP client to each configured server — a stdio child
  (`rmcp` client + `TokioChildProcess`) or a hosted endpoint
  (`StreamableHttpClientTransport`). Children are spawned on first touch
  (`load_server` / `call_tool`), kept warm, and reaped by `drop_server`.

## HTTP downstream (shipped 2026-09-21)

A fleet entry carries either a `command` (stdio child) or a `url` (streamable HTTP);
`transport()` enforces the exclusive-or once, at load, rather than at every call site.
Two ways to authenticate, both by reference:

- **Static bearer / API key** — `header_file`, a file of `Header: value` lines merged
  under the inline `headers` map. This is `env_file`'s rule applied to HTTP: the secret
  is named by the config, held by the file, and never logged.
- **OAuth** (`auth = "oauth"`) — for an endpoint that answers `401` with a
  `WWW-Authenticate` challenge. `oauth.rs` owns the two things the library cannot: where
  tokens live (`~/.dangler/oauth/<server>.json`, outside every repo) and how the operator
  says yes (`dangler auth <server>`, consent in their own browser, loopback redirect on
  port 8899). A session never mints an authorization code for itself.

An HTTP server has no process, so nothing is spawned, nothing is reaped, and `status` is
warm as soon as its schemas are cached. Everything above it — `load_server`,
`search_tools`, `call_tool`, `identity`, `setup_hint` — behaves identically, which is the
point: the caller should not have to know where a tool physically runs.

What this changes for ingestion: a hosted MCP endpoint is now an ordinary fleet entry
(`LIST`), not a service that needs a first-party extension. `WRAP` narrows to what it
always should have meant — lifecycle, a bundled toolkit, or an identity dangler must
supply. `myevery` was the first ingestion on this path (`url` + `header_file`,
2026-09-21), and it retired a client registration that had put a bearer in a
project-shaped file.

## The dangle

The point is asymmetry: the model sees a ~5-tool schema up front instead of the fleet's
hundreds. Discovery is pull-based:

1. `list_servers` / `search_tools` — browse capability cheaply (cached schemas, no spawn
   needed once warm).
2. `load_server {name}` — the dangle: full tool schemas for one server, on demand.
3. `call_tool {server, tool, arguments}` — dispatch. Result relayed verbatim.

This mirrors the deferred-tool / ToolSearch pattern Claude Code applies to its own MCP
registrations — but client-agnostic, self-hosted, and under your config control.

## Modules

- `config.rs` — `dangler.toml` (`[servers.<name>] command/args/env/cwd`).
- `fleet.rs` — the downstream fleet: lazy spawn, running-client registry, schema cache
  (in-memory v0), search over cached tools.
- `server.rs` — upstream `ServerHandler`: the 5 meta-tools, hand-written JSON schemas,
  dispatch into the fleet.
- `skills.rs` — every entry's `SKILL.md` resolved, validated, installed into the
  client's skills directory, stale copies pruned; entries without one disabled.
- `main.rs` — load config, sync skills, serve stdio (`warm`, `auth`, `skills` subcommands).

## Battle-scar: always drain stderr (resolved 2026-07-22)

A `drop_server` "hang" with wsl-bridged children turned out to be the *test harness*:
it redirected dangler's stderr to a pipe it never read. Downstream children inherit that
stderr, and their login/npx noise filled the pipe buffer, wedging teardown. With stderr
drained, `drop_server` completes in ~300ms and the child exits gracefully — even through
`wsl.exe`. Two durable rules:
1. Any client embedding dangler (and dangler embedding children) must **drain or file-sink
   stderr**, never leave it a dangling pipe.
2. The bounded cancel in `fleet::drop_server` (3s timeout → `kill_on_drop`) stays as
   defense-in-depth against children that genuinely ignore stdin close.

## Idle reaping (shipped 2026-07-27)

Every warm child carries `{last_used, inflight}`. `acquire`/`release` bracket each
downstream request (spawn counts as acquire); a background task scans every 30s and
cancels children where `inflight == 0 && idle >= timeout`. Timeout resolution:
per-server `idle_timeout_secs` → global `idle_timeout_secs` → 600s default; `0` disables
reaping at either level. The in-flight guard means a slow downstream call can never be
reaped mid-request, no matter how stale `last_used` looks. Reaped ≠ forgotten: cached
schemas stay, so `search_tools` still answers and the next `call_tool` respawns.
Verified live: 30s-timeout child auto-reaped ~58s after spawn (timeout + scan phase),
status warm → cold, schemas intact.

## Extensions (first-party fleet servers)

`extensions/<name>/` is a standalone stdio MCP server we own, meant to be listed
in `dangler.toml` like any third-party server — **no coupling to dangler at
runtime**. That independence is the whole contract: an extension is a normal MCP
server that happens to live in this repo, and it keeps working if dangler
disappears.

House rules, ordered by what they cost when broken:

- **stderr-only logging** — stdout is the transport (see the drain-stderr scar).
- **Lazy provisioning** — the server must start and answer `tools/list` with no
  credentials, no toolkit, nothing configured, so `dangler warm` harvests
  schemas cold. Every call that needs the missing piece fails with a setup hint
  naming it, and the config's `setup_hint` should say the same sentence.
- **Hand-written tool schemas** — a static surface is small enough to write out,
  and the prose a model reads is worth authoring rather than deriving. In Rust
  that means a manual `ServerHandler` with `match`-based dispatch, in dangler's
  own style, not the `#[tool]` macros.
- **A read-only switch** wherever the wrapped thing writes — remote state
  (`GODADDY_READ_ONLY=1`) or the local disk (`YTDL_READ_ONLY=1`). Mirrors the
  mongodb `--readOnly` convention.
- **Secrets by reference** — a wrapped server's credential lives in a file the
  config names with `env_file`, never as a literal in `dangler.toml`. The
  extension reads it through the environment dangler hands it, dangler never
  logs a value, and `setup_hint` names the file to fill. The inline `env` map is
  for paths, ids and switches, and it wins over the file on a clash.
- **An `identity` in the config** — whose account the server acts as. A fleet
  wearing several different hats is the normal case, and the caller should know
  which hat before invoking, not after.
- **A `SKILL.md` next to the code — the name tag (forced, 2026-09-26).** Hiding
  schemas until `load_server` has a blind spot: nothing in a fresh session says
  *when* to reach for a server. That sentence is a skill — frontmatter `name` +
  `description` the client indexes in every session — plus the craft the schemas
  cannot hold (which tool for which need, the rules paid for in use). Every
  fleet entry carries one; dangler installs them all into the client's skills
  directory (`~/.claude/skills/<name>/SKILL.md`) at every start and on
  `dangler skills`, so a `git pull` of this repo is also the skill's
  distribution. **An entry without a skill is not served** — it is listed as
  `disabled` with the path it was expected at. Resolution: `skill = <path>` in
  the config, else `<--directory>/SKILL.md` for a `uv run`-style command, else
  `<extensions_dir>/<server>/SKILL.md` (the checkout the binary was built in, or
  `extensions_dir` / `DANGLER_EXTENSIONS`). A hosted wrapper with no code still
  gets a folder for its tag (`extensions/myevery`). Installed copies carry a
  marker line; dangler overwrites or prunes only files that carry it, so a
  hand-written skill is never clobbered — a name clash is reported as a conflict.

**Language is not part of the contract.** Rust extensions are Cargo workspace
members built by `cargo build --release --workspace`; others carry their own
toolchain and are launched by their own runner. `[workspace] members` lists only
the Rust ones.

| Path | Server | Language | Wraps |
| --- | --- | --- | --- |
| `extensions/cloudflare` | `dangler-cloudflare` | Rust | The Cloudflare v4 API: zones, DNS, cache purge, Origin CA certificates, plus a `raw_api` escape hatch. Replaces the vendor plugin's arbitrary-JS `execute` with a named surface behind one scoped token |
| `extensions/godaddy` | `dangler-godaddy` | Rust | GoDaddy domains, DNS and subscriptions, plus a `raw_api` escape hatch for the long tail |
| `extensions/google` | `gws-mcp` | Python, run by `uv` | Google Docs and Sheets read/write and Drive read-only, on your own OAuth desktop client |
| `extensions/ytdl` | `dangler-ytdl` | Rust | A bundled yt-dlp / ffmpeg / deno toolkit: local video, MP3 and transcript capture |
| `extensions/comfy` | `comfy-mcp` | Python, run by `uv` | The AIProd image gateway: generated images from the one local ComfyUI service, identical tools on the GPU seat and on remote seats |
| `extensions/myevery` | — (hosted, `url`) | none — a name tag only | The myevery bus; the folder exists so the wrapper carries its `SKILL.md` like every other entry |

Two concessions worth knowing: `extensions/google` needs `uv` on PATH and a
one-time `gws-mcp auth` per machine, and `extensions/ytdl` ships no toolchain at
all. `YTDL_HOME` points at the toolkit directory, which stays outside the repo
because it holds large binaries and a cookie jar that is a credential.

## Roadmap (v0 → useful)

- [x] **Persistent schema cache** (`~/.dangler/cache.json`) + `dangler warm` (2026-07-22).
- [x] **Idle reaping** — per-server/global `idle_timeout_secs`, in-flight guard (2026-07-27).
- [ ] **Streamable HTTP upstream** (`transport-streamable-http-server` feature) so
      claude.ai custom connectors can use dangler too.
- [x] **HTTP downstream** (`transport-streamable-http-client-reqwest`) for hosted MCP
      servers: `url` + `header_file`, or `auth = "oauth"` with `dangler auth <server>`
      (2026-09-21).
- [ ] **Namespaced passthrough mode** — optionally re-advertise a loaded server's tools as
      real upstream tools (`<server>__<tool>`) via `tools/list_changed` notifications, so
      clients that support dynamic tool lists skip the `call_tool` indirection.
- [x] Auth passthrough for downstream servers needing OAuth — dangler holds the tokens
      and the operator grants them once (2026-09-21).
- [x] Tests: config parsing, cache persistence, schema search, reap decision logic
      (fleet lifecycle against a toy MCP server still pending).
