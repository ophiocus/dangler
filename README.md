# dangler

**An MCP pre-loader.** Dangler sits between an MCP client (Claude Code, claude.ai, anything)
and a fleet of MCP servers, and *dangles* their tools in front of the model — advertising
what's available without paying for it until it's used.

## The problem

Every MCP server you register costs you twice before you've called a single tool:

1. **Context** — every tool schema from every server is injected into the model's context
   up front. Ten servers × thirty tools each and you've burned thousands of tokens on
   schemas for tools this conversation will never touch.
2. **Processes** — stdio servers are spawned at client startup and sit resident whether
   used or not.

## The idea

Register **one** server — dangler. It exposes a tiny meta-surface and proxies everything else:

| Tool | What it does |
|---|---|
| `list_servers` | The configured fleet: name, status (cold/warm), tool count |
| `search_tools {query}` | Search tool names/descriptions across the fleet's cached schemas |
| `load_server {name}` | Spawn the server if cold, return its full tool schemas — *the dangle* |
| `call_tool {server, tool, arguments}` | Proxy a call to a downstream tool (lazy-spawns) |
| `drop_server {name}` | Reap a downstream server's process |

The model discovers capability through `search_tools`/`load_server` (paying schema cost
only for what it needs), then drives real work through `call_tool`. Downstream servers are
spawned on first touch and reaped when dropped.

## Config

`dangler.toml` (see [dangler.example.toml](dangler.example.toml)) — one entry per
downstream server, mirroring the usual MCP client config shape:

```toml
[servers.mongodb]
command = "npx"
args = ["-y", "mongodb-mcp-server@latest", "--readOnly"]

[servers.drive-overdrive]
command = "node"
args = ["D:/Projects/google-drive-overdrive/dist/index.js"]
[servers.drive-overdrive.env]
MCP_TRANSPORT = "stdio"
```

## Run

```
cargo build --release
dangler                       # serves MCP over stdio; config from ./dangler.toml
DANGLER_CONFIG=path.toml dangler
```

Register in Claude Code: `claude mcp add dangler -- path/to/dangler.exe`.

## Status (2026-07-22)

**v0.1 working end-to-end.** Stdio upstream · child-process stdio downstream (incl. the
`wsl.exe` bridge for Node servers on a Node-less Windows host) · **persistent schema
cache** at `~/.dangler/cache.json` · `dangler warm` pre-loader subcommand. Proven against
the real fleet: load → search → call → drop round-trips, and cold-start `search_tools`
answers across all warmed servers without spawning anything. Registered user-scope in
Claude Code. See [docs/architecture.md](docs/architecture.md) for the design, the
stderr-drain battle-scar, and the roadmap (idle reaping, Streamable HTTP, passthrough mode).

```
dangler warm        # spawn each configured server once, harvest schemas, reap
dangler             # serve MCP over stdio (search works cold, from the cache)
```

## Stack

Rust · [`rmcp`](https://crates.io/crates/rmcp) (official MCP SDK — server *and* client,
since dangler is both at once) · tokio. Personal project (`ophiocus`).
