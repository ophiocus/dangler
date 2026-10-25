# dangler-unreal

Epic's Unreal MCP server (the UE 5.8 `ModelContextProtocol` + `ToolsetRegistry` plugins, enabled
through `AllToolsets`) as one dangler fleet entry, `unreal`. It replaces every per-project
`.mcp.json` that registered `http://127.0.0.1:8000/mcp` with the client directly.

## Why a wrapper, when Epic already ships a lazy server

Schema size is not the reason; Epic's tool search already hides its 800+ tools. The reasons are
the ones the dangler rule names — identity, lifecycle and the keychain:

| Problem with a direct registration | What the wrapper does |
|---|---|
| Every project registered the same URL on the same port, so a client talked to whichever editor bound `:8000` first | One port per project; every call proves the port's owner from the listening process's command line and refuses on a mismatch |
| A client connects to project servers once, at session start, so a closed editor stayed failed for the whole session | Connects on first use; `unreal_launch` starts the editor mid-session, detached, with the server armed on its own port |
| Nothing stopped an editor from a different engine build | `unreal_launch` refuses anything but the pin (default `5.8.2-56702186`) |
| Raw `describe_toolset` reaches 278k characters and clients cut the middle silently | Condensed to one line per tool; one tool's schema on request; any cap is announced |
| Overlapping calls deadlock the game thread | Calls to one editor are serialised |

There is no secret: Epic's server has no authentication. The wrapper does not add any — an open
editor's port is still reachable by any process on the machine. Fronting it with a token, or
reaching an editor on another seat over the myevery bus, is the next step and not built yet.

## Fleet entry

```toml
[servers.unreal]
command = "uv"
args = ["run", "--directory", "I:/dangler/extensions/unreal", "dangler-unreal"]
identity = "Unreal Editor 5.8.2 on <seat> — the configured projects' editors, through Epic's ModelContextProtocol plugin; no auth, loopback only"
setup_hint = "list each project in [servers.unreal.env] UNREAL_PROJECTS as name=<path>.uproject@<port>, one port per project; ..."
[servers.unreal.env]
UNREAL_PROJECTS = "skeleton=I:/ue5-skeleton/UE5Skeleton.uproject@8001;game=F:/path/to/Game.uproject@8002"
```

Paths and ports are per seat; the live entry is in that seat's `~/.dangler/dangler.toml`.

## Which port an editor serves on

- `unreal_launch` passes `-ModelContextProtocolStartServer -ModelContextProtocolPort=<port>`, so a
  launched editor serves on its project's own port.
- An editor opened by hand serves nothing until someone runs `ModelContextProtocol.StartServer`
  in its console. With no argument that command binds Epic's constant `DefaultServerPort`, 8000 —
  not the `.ini` `ServerPortNumber`, which only auto-start reads (5.8.2 source,
  `ModelContextProtocolModule.cpp`). `ModelContextProtocol.StartServer <port>` binds that port.
- A call for a project goes to its own port when the editor there is that project; otherwise to
  `:8000` when the editor there is that project. Any other owner is refused, never guessed. So a
  hand-opened editor keeps working exactly as it did under the old `.mcp.json`, and two editors
  never answer for each other.
- `unreal_launch` will not open a second editor on a project that is already open without a
  server; it says which console command to run instead.

## Wire format, from the 5.8.2 source

`initialize`, `tools/list` and an ordinary `tools/call` answer with one `application/json` body
framed by `Content-Length`. Only a call that carries a `progressToken` streams
`text/event-stream` (no length, no chunking), and the server closes the socket after the final
event. An earlier 5.8 build streamed every result that way and kept the socket open, which made
strict HTTP clients read an empty body. `epic.py` reads raw HTTP over a socket and handles every
case, so it does not care which build answers. An unknown `Mcp-Session-Id` is a `404`, which is
what an editor restart on the same port looks like; the session is reopened once.

## Things that would put a `.mcp.json` back

- `ModelContextProtocol.GenerateClientConfig ClaudeCode` in the editor console writes one. Do not
  run it; this entry is the route.
- Epic's Claude Code plugin (`unreal-engine-skills-for-claude-code`) ships no `.mcp.json`, but its
  `/unreal-mcp` skill names Epic's raw tools. Through dangler they are `unreal_toolsets`,
  `unreal_describe` and `unreal_call`.

## Tools

`unreal_status`, `unreal_launch`, `unreal_toolsets`, `unreal_describe`, `unreal_call`,
`unreal_skills`. The skill (`SKILL.md`) says when to use which.

## Test

```
uv run --directory I:/dangler/extensions/unreal python tests/offline.py
uv run --directory I:/dangler/extensions/unreal python tests/smoke.py
uv run --directory I:/dangler/extensions/unreal python tests/smoke.py --live <project>
```

`offline.py` needs nothing: a fake Epic server plays every framing above, plus the command-line
parse. `smoke.py` lists the tools over stdio with nothing configured; `--live <project>` launches
that project (from `UNREAL_PROJECTS` in the environment) and runs a read-only pass through every
tool.
