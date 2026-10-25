---
name: unreal
description: Drive a live Unreal Editor (UE 5.8, Epic's built-in MCP server) through the `unreal` server in the dangler fleet — open a project's editor, list and describe its toolsets, call any editor tool (actors, Blueprints, materials, levels, Niagara, Sequencer, PCG, GAS, automation tests, Live Coding, the studio's own toolsets), and read its Agent Skills. Use whenever a task changes or inspects something inside an Unreal project rather than its files on disk, e.g. "spawn a light in the testbed", "what toolsets does fps_1 have", "audit the characters", "compile and run the tests", "read this Blueprint graph", "open the skeleton editor". Never register Epic's server in a project .mcp.json; this entry is the one route.
---

# unreal — the editor, through one door

Epic's ModelContextProtocol plugin serves 800+ editor tools behind three meta-tools. This fleet
entry wraps it: each configured project has its own port, every call first proves which
`.uproject` owns that port, editors are started detached from the pinned engine build, and
toolset descriptions come back condensed. Epic's server has no authentication; treat any open
editor as reachable by anything on this machine.

## How to call it

1. `load_server {name: "unreal"}` once.
2. `unreal_status` — which projects are configured, which are running, who owns each port.
3. `unreal_launch {project}` if the one you need is not running (~45 s warm, much longer cold).
4. `unreal_skills {project}` — **before working by hand in a domain**. Epic ships skills for
   lighting, Blueprints, materials, Niagara and PCG, and the studio ships its own. Load the one
   that matches with `paths`. A fix rebuilt by hand that a shipped skill already described is
   this studio's scar, not a hypothetical.
5. `unreal_toolsets` → `unreal_describe {toolset}` → `unreal_call {toolset, tool, arguments}`.

## Which tool

| Need | Tool |
|---|---|
| is it up, which editor holds which port, engine build vs pin | `unreal_status` |
| open an editor with its server armed | `unreal_launch` |
| what knowledge the editor ships for this kind of task | `unreal_skills` |
| every registered toolset | `unreal_toolsets` |
| one toolset's tools, one line each | `unreal_describe {toolset}` |
| one tool's exact schema | `unreal_describe {toolset, tool}` |
| do the thing | `unreal_call` |

## Rules paid for in use

- **One call at a time.** Calls run on the editor's game thread; this server serialises them per
  editor, so do not fan them out in parallel either.
- **Save before and after any bulk change.** Not every MCP edit is undoable, least of all across
  a compile.
- **Read the result.** Many tools report failure in their text without raising. A setter that
  returns nothing has succeeded.
- **Mind Play-In-Editor.** Asset-creation tools misbehave while PIE runs; stop it first.
- **Never guess names.** Toolsets are addressed by their full dotted name; describe before
  calling. A wrong argument returns the full schema in the error — read it, then retry once.
- **Asset arguments are objects:** `{"refPath": "/Game/Path/Asset.Asset"}`; a class is
  `{"refPath": "/Script/Module.ClassName"}`; a Blueprint's defaults object is
  `/Game/.../BP_X.Default__BP_X_C`.
- **C++ over Blueprint for logic you write.** Use Blueprints to drive C++. To read or edit a
  graph, use Epic's graph DSL tools rather than node-by-node calls.
- **New C++ tools need a full editor restart.** Live Coding patches bodies, not new `UFUNCTION`s.
- **`execute_tool_script` cannot `import unreal`.** It can only call registered tools.
- **The engine is frozen at 5.8.2.** `unreal_launch` refuses any other build; changing the pin is
  Carlos's recorded decision, never a workaround.
- **An editor Carlos opened by hand works too.** After `ModelContextProtocol.StartServer` in its
  console it serves on `:8000`, and calls for that project go there, once the editor holding the
  port proves to be that project. `ModelContextProtocol.StartServer <port>` puts it on the
  project's own port instead.
- **A `WRONG EDITOR` line in `unreal_status`** means some editor holds another project's port.
  Close one; do not call through it.
- **A project's own scripts default to `:8000`** (in a stamped game, `tools/mcp_probe.py`,
  `level_audit.py`, `stage_level.py`, …). For an editor `unreal_launch` opened, pass
  `--url http://127.0.0.1:<port>/mcp` with the port `unreal_status` shows.
- **Never run `ModelContextProtocol.GenerateClientConfig`**, and never write a `.mcp.json` for
  Epic's server. Epic's `/unreal-mcp` skill names the raw tools; here they are
  `unreal_toolsets`, `unreal_describe` and `unreal_call`.

## Configuration

`[servers.unreal.env] UNREAL_PROJECTS = "name=<path>.uproject@<port>;..."` — one port per
project. `UNREAL_READ_ONLY=1` limits `unreal_call` to tools whose first word is a query verb
(get, list, find, describe, read, search, is, has, query, count, inspect, discover, validate,
audit, report, lookup). Full settings: `src/dangler_unreal/editor.py`.
