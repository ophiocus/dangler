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
- **Downstream**: dangler is an MCP client to each configured server
  (`rmcp` client + `TokioChildProcess` transport). Children are spawned on first touch
  (`load_server` / `call_tool`), kept warm, and reaped by `drop_server`.

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
- `main.rs` — load config, serve stdio.

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
- **An `identity` in the config** — whose account the server acts as. A fleet
  wearing several different hats is the normal case, and the caller should know
  which hat before invoking, not after.

**Language is not part of the contract.** Rust extensions are Cargo workspace
members built by `cargo build --release --workspace`; others carry their own
toolchain and are launched by their own runner. `[workspace] members` lists only
the Rust ones.

| Path | Server | Language | Wraps |
| --- | --- | --- | --- |
| `extensions/godaddy` | `dangler-godaddy` | Rust | GoDaddy domains, DNS and subscriptions, plus a `raw_api` escape hatch for the long tail |
| `extensions/google` | `gws-mcp` | Python, run by `uv` | Google Docs and Sheets read/write and Drive read-only, on your own OAuth desktop client |
| `extensions/ytdl` | `dangler-ytdl` | Rust | A bundled yt-dlp / ffmpeg / deno toolkit: local video, MP3 and transcript capture |

Two concessions worth knowing: `extensions/google` needs `uv` on PATH and a
one-time `gws-mcp auth` per machine, and `extensions/ytdl` ships no toolchain at
all. `YTDL_HOME` points at the toolkit directory, which stays outside the repo
because it holds large binaries and a cookie jar that is a credential.

## Roadmap (v0 → useful)

- [x] **Persistent schema cache** (`~/.dangler/cache.json`) + `dangler warm` (2026-07-22).
- [x] **Idle reaping** — per-server/global `idle_timeout_secs`, in-flight guard (2026-07-27).
- [ ] **Streamable HTTP upstream** (`transport-streamable-http-server` feature) so
      claude.ai custom connectors can use dangler too.
- [ ] **HTTP downstream** (`transport-streamable-http-client`) for remote MCP servers.
- [ ] **Namespaced passthrough mode** — optionally re-advertise a loaded server's tools as
      real upstream tools (`<server>__<tool>`) via `tools/list_changed` notifications, so
      clients that support dynamic tool lists skip the `call_tool` indirection.
- [ ] Auth passthrough for downstream servers needing OAuth (hard; see rmcp `auth` feature).
- [x] Tests: config parsing, cache persistence, schema search, reap decision logic
      (fleet lifecycle against a toy MCP server still pending).
