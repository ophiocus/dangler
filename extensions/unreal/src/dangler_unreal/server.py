"""dangler-unreal: Epic's Unreal MCP server, wrapped as one dangler fleet entry.

Epic's ModelContextProtocol plugin already serves 800+ editor tools behind tool search. This
wrapper adds what the plugin cannot, and nothing that duplicates it:

- identity — every configured project has its own port, and every call first proves which
  .uproject owns that port (by the listening process's command line), so a client can never
  talk to the wrong editor because it happened to bind :8000 first;
- lifecycle — unreal_launch starts an editor detached, with the server armed on that
  project's port, from the pinned engine build only;
- context safety — describe_toolset is condensed to one line per tool (raw payloads reach
  278k characters and clients silently drop the middle), one tool's schema on request, and
  any oversized result is cut with an explicit notice instead of silently;
- discipline — calls to one editor are serialised (the game thread runs them one at a time),
  and UNREAL_READ_ONLY refuses anything that does not read as a query.

There is no secret to hold: Epic's server has no authentication. Settings are in editor.py.
Logging goes to stderr; stdout is the MCP transport.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import editor, epic
from .editor import EditorError, log
from .epic import EpicError, Unreachable

T_QUICK, T_CALL = 30.0, 600.0
MAX_CHARS_DEFAULT = 60_000
AGENT_SKILLS = "ToolsetRegistry.AgentSkillToolset"


def _j(o: Any) -> str:
    return json.dumps(o, indent=1, ensure_ascii=False)


def _cap(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return (text[:max_chars] + f"\n\n[TRUNCATED by dangler-unreal: {len(text):,} characters, first {max_chars:,} shown. "
            f"Narrow the call, or raise max_chars.]")


def _meta(p: editor.Project, tool: str, args: dict, timeout: float = T_CALL) -> dict:
    port = editor.verify(p)
    result = epic.run(port, timeout, tool, args)
    if result.get("isError"):
        raise EpicError(epic.text_of(result) or f"{tool} returned isError with no text")
    return result


def _call_tool(p: editor.Project, toolset: str, tool: str, arguments: dict) -> dict:
    return _meta(p, "call_tool", {"toolset_name": toolset, "tool_name": tool, "arguments": arguments})


# ---------------------------------------------------------------- tools

def t_status(a: dict) -> str:
    lines: list[str] = []
    try:
        root = editor.engine_root()
        build = editor.engine_build(root)
        flag = "OK" if build == editor.PIN else f"DRIFT — pinned {editor.PIN}; launch refused"
        lines.append(f"engine  {root}  build {build}  [{flag}]")
    except EditorError as exc:
        lines.append(f"engine  unresolved: {exc}")
    lines.append(f"mode    {'READ-ONLY (unreal_call runs query tools only)' if editor.READ_ONLY else 'read-write'}")
    ps = editor.projects()
    if not ps:
        return "\n".join(lines + ["", "no projects configured. " + editor.SETUP])
    only = a.get("project")
    lines.append("")
    for p in ps.values():
        if only and p.name != only:
            continue
        head = f"{p.name:12} port {p.port:<5} {p.uproject}" + ("" if p.uproject.is_file() else "  [.uproject MISSING]")
        o = editor.owner(p.port)
        if o and not o["uproject"]:
            lines.append(f"{head}\n             port held by pid {o['pid']} with no .uproject on its command line — not trusted")
            continue
        if o and not editor.owns(p, o):
            lines.append(f"{head}\n             WRONG EDITOR: port held by pid {o['pid']} running {o['uproject']}")
            continue
        port = editor.serving_port(p)
        if not port:
            lines.append(f"{head}\n             not running")
            continue
        o = editor.owner(port)
        via = "" if port == p.port else f" — opened by hand, served on the default port {port}"
        try:
            pr = epic.probe(port)
            # Epic's 5.8.2 server sends an empty serverInfo name; show it only when there is one.
            srv = " ".join(x for x in (pr["server"].get("name"), pr["server"].get("version")) if x)
            lines.append(f"{head}\n             running, pid {o['pid']}{via}; MCP{' ' + srv if srv else ''} "
                         f"(protocol {pr['protocol']}); tools/list = {', '.join(pr['tools'])}")
        except (Unreachable, EpicError) as exc:
            lines.append(f"{head}\n             editor pid {o['pid']} owns port {port}{via}, but MCP did not answer ({exc}) — still loading?")
    if editor.DEFAULT_PORT not in {p.port for p in ps.values()}:
        o = editor.owner(editor.DEFAULT_PORT)
        if o and not any(editor.owns(p, o) for p in ps.values()):
            lines.append(f"\nport {editor.DEFAULT_PORT} (the hand-opened default): held by pid {o['pid']} running "
                         f"{o['uproject'] or 'an unknown command line'}, which is no configured project. No call goes through it.")
    return "\n".join(lines)


def t_launch(a: dict) -> str:
    p = editor.pick(a.get("project"))
    port = editor.serving_port(p)
    if port:
        o = editor.owner(port)
        return f"{p.name} is already running (pid {o['pid']}, port {port}). Nothing started."
    if editor.owner(p.port):
        editor.verify(p)  # raises: some other editor holds this project's port
    already = [e["pid"] for e in editor.editors() if e["uproject"] and editor._norm(e["uproject"]) == p.key]
    if already:
        return (f"{p.name} is already open (pid {', '.join(map(str, already))}) but serves no MCP. Nothing started: a "
                f"second editor on one project fights over its files. In that editor's console run "
                f"ModelContextProtocol.StartServer {p.port}, then call unreal_status.")
    t0 = time.time()
    cmd = editor.start(p)
    wait = float(a.get("wait_seconds", editor.LAUNCH_WAIT))
    if wait <= 0:
        return f"started {p.name}: {' '.join(cmd)}\nNot waiting; poll unreal_status."
    if not editor.wait_for_port(p.port, t0 + wait):
        return (f"started {p.name}, but port {p.port} was not listening after {wait:.0f}s. A first open compiles "
                f"shaders and can take much longer; poll unreal_status rather than launching again.")
    port_up = time.time() - t0
    # Toolsets register a little after the port opens; wait until the listing stops growing.
    last, stable, listing = -1, 0, ""
    while time.time() < t0 + wait + 120 and stable < 2:
        try:
            listing = epic.text_of(_meta(p, "list_toolsets", {}, T_QUICK))
            n = listing.count("\n")
            stable = stable + 1 if n == last else 0
            last = n
        except (Unreachable, EpicError, EditorError):
            stable = 0
        time.sleep(5)
    toolsets = sum(1 for line in listing.splitlines() if line.lstrip().startswith("- "))
    return (f"{p.name} is up: port {p.port} listening after {port_up:.0f}s, "
            f"{toolsets} toolsets registered after {time.time() - t0:.0f}s.\n{' '.join(cmd)}")


def t_toolsets(a: dict) -> str:
    p = editor.pick(a.get("project"))
    return epic.text_of(_meta(p, "list_toolsets", {}))


def t_describe(a: dict) -> str:
    p = editor.pick(a.get("project"))
    payload = epic.text_of(_meta(p, "describe_toolset", {"toolset_name": a["toolset"]}))
    if a.get("tool"):
        return epic.one_tool(payload, a["tool"])
    if a.get("full"):
        return _cap(payload, int(a.get("max_chars", MAX_CHARS_DEFAULT)))
    return epic.condense_toolset(payload)


def t_call(a: dict) -> list[types.TextContent | types.ImageContent]:
    p = editor.pick(a.get("project"))
    tool = a["tool"]
    if editor.READ_ONLY and not editor.is_read_only_tool(tool):
        raise EditorError(f"UNREAL_READ_ONLY is set — {tool!r} does not read as a query tool and is refused")
    result = _call_tool(p, a["toolset"], tool, a.get("arguments") or {})
    max_chars = int(a.get("max_chars", MAX_CHARS_DEFAULT))
    out: list[types.TextContent | types.ImageContent] = []
    for c in result.get("content", []):
        if c.get("type") == "image" and c.get("data"):
            out.append(types.ImageContent(type="image", data=c["data"], mimeType=c.get("mimeType", "image/png")))
        elif c.get("type") == "text":
            out.append(types.TextContent(type="text", text=_cap(c.get("text", ""), max_chars)))
    if "structuredContent" in result and not any(isinstance(x, types.TextContent) for x in out):
        out.append(types.TextContent(type="text", text=_cap(_j(result["structuredContent"]), max_chars)))
    return out or [types.TextContent(type="text", text="(the tool returned no content — for a setter that is success)")]


def t_skills(a: dict) -> str:
    p = editor.pick(a.get("project"))
    paths = a.get("paths") or []
    if paths:
        r = _call_tool(p, AGENT_SKILLS, "GetSkills", {"SkillPaths": paths})
    else:
        r = _call_tool(p, AGENT_SKILLS, "ListSkills", {})
    return _cap(epic.text_of(r) or _j(r.get("structuredContent")), int(a.get("max_chars", MAX_CHARS_DEFAULT)))


PROJECT = {"type": "string", "description": "Project name from the fleet config. Optional when only one is configured or only one is running."}
MAXC = {"type": "integer", "description": f"Cut the result at this many characters, with an explicit notice. Default {MAX_CHARS_DEFAULT:,}; 0 = no cap."}

TOOLS = {
    "unreal_status": (t_status, types.Tool(
        name="unreal_status",
        description="Every configured Unreal project: its port, whether an editor is running, WHICH project owns the "
                    "port (proved from the listening process), and whether its MCP server answers. Also the engine "
                    "build against the studio pin. Starts nothing. Call this first.",
        inputSchema={"type": "object", "properties": {"project": PROJECT}})),
    "unreal_launch": (t_launch, types.Tool(
        name="unreal_launch",
        description="Open a project's editor with its MCP server armed on that project's own port, detached so it "
                    "outlives this server. Refuses an engine build other than the pin, and does nothing if the "
                    "project is already running. Waits for the port and for the toolsets to finish registering "
                    "(a warm open is about 45 s; a first open compiles shaders and is much slower).",
        inputSchema={"type": "object", "properties": {"project": PROJECT,
            "wait_seconds": {"type": "number", "description": "How long to wait for the port. Default 240; 0 = return at once."}}})),
    "unreal_toolsets": (t_toolsets, types.Tool(
        name="unreal_toolsets",
        description="Epic's list_toolsets for a project: every registered toolset (Epic's and the project's own) with "
                    "its one-line description. Toolsets are addressed by their FULL dotted name.",
        inputSchema={"type": "object", "properties": {"project": PROJECT}})),
    "unreal_describe": (t_describe, types.Tool(
        name="unreal_describe",
        description="A toolset's tools, CONDENSED to one signature line each (`name?: type = default`, ref<X> for asset "
                    "references) — safe for any toolset, including ones whose raw schema is hundreds of thousands of "
                    "characters. Pass tool for that one tool's full input schema. full=true returns the raw payload, capped.",
        inputSchema={"type": "object", "required": ["toolset"], "properties": {"project": PROJECT,
            "toolset": {"type": "string", "description": "Full dotted toolset name from unreal_toolsets."},
            "tool": {"type": "string", "description": "One tool's name: return its full input schema only."},
            "full": {"type": "boolean"}, "max_chars": MAXC}})),
    "unreal_call": (t_call, types.Tool(
        name="unreal_call",
        description="Run one toolset tool in the project's editor and return its result (images pass through). Runs on "
                    "the editor's game thread: one at a time, and slow while the editor compiles, loads a level or "
                    "plays in editor. Asset arguments are {\"refPath\": \"/Game/Path/Asset.Asset\"}. A wrong argument "
                    "returns the tool's full input schema in the error — read it and retry.",
        inputSchema={"type": "object", "required": ["toolset", "tool"], "properties": {"project": PROJECT,
            "toolset": {"type": "string", "description": "Full dotted toolset name."},
            "tool": {"type": "string", "description": "Tool name, without the toolset prefix."},
            "arguments": {"type": "object", "description": "The tool's arguments, as its schema names them."},
            "max_chars": MAXC}})),
    "unreal_skills": (t_skills, types.Tool(
        name="unreal_skills",
        description="The editor's Agent Skills — the knowledge Epic and the studio ship for working in a domain "
                    "(lighting, Blueprints, materials, Niagara, PCG, studio conventions). No paths: list them with their "
                    "descriptions. paths: load those skills' full instructions. Check here before working by hand.",
        inputSchema={"type": "object", "properties": {"project": PROJECT,
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Skill paths from the listing."},
            "max_chars": MAXC}})),
}

server = Server("dangler-unreal")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [tool for _, tool in TOOLS.values()]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent | types.ImageContent]:
    fn = TOOLS[name][0]
    # Raising is how the SDK marks a result isError, which a client acts on; text alone it does not.
    try:
        out = await asyncio.to_thread(fn, arguments or {})
    except Unreachable as exc:
        raise RuntimeError(f"ERROR (editor unreachable): {exc}\nCall unreal_status, then unreal_launch if it is not running.") from None
    except (EditorError, EpicError, KeyError, ValueError) as exc:
        raise RuntimeError(f"ERROR: {exc}") from None
    if isinstance(out, list):
        return out
    return [types.TextContent(type="text", text=str(out))]


def main() -> None:
    if sys.argv[1:]:
        print(__doc__)
        print(editor.__doc__)
        sys.exit(0 if sys.argv[1] in ("-h", "--help") else 2)

    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()
