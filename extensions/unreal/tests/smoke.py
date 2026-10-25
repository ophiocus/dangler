"""Smoke test for dangler-unreal over real stdio MCP — the same path dangler uses.

    uv run --directory I:/dangler/extensions/unreal python tests/smoke.py
        Cold: the server starts and lists its six tools with nothing configured.
    uv run --directory I:/dangler/extensions/unreal python tests/smoke.py --live <project>
        Live: launches that project (from UNREAL_PROJECTS in the environment) and runs a
        read-only pass through every tool. Exit 0 only if every step passes.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent.parent
EXPECTED = {"unreal_status", "unreal_launch", "unreal_toolsets", "unreal_describe", "unreal_call", "unreal_skills"}
STUDIO = "studiocore_toolset.toolsets.characters.StudioCharacterTools"


def text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def main(live: str | None) -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "dangler_unreal.server"],
                                   env=dict(os.environ), cwd=str(HERE / "src"))
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"\n{detail}" if detail else ""), flush=True)

    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            check("tools/list returns the six tools", names == EXPECTED, f"  got {sorted(names)}")
            if not live:
                st = text(await s.call_tool("unreal_status", {}))
                check("unreal_status answers with nothing required", "engine" in st, st)
                return failures

            async def step(label: str, tool: str, args: dict, must: str, show: int = 1200) -> str:
                t0 = time.time()
                out = text(await s.call_tool(tool, args))
                ok = must in out and not out.startswith("ERROR")
                check(f"{label}  ({time.time() - t0:.1f}s)", ok, out[:show])
                return out

            await step("status", "unreal_status", {}, "engine")
            await step("launch", "unreal_launch", {"project": live}, live, 800)
            await step("status after launch", "unreal_status", {"project": live}, "running, pid")
            ts = await step("toolsets", "unreal_toolsets", {"project": live}, "Toolset", 600)
            await step("describe studio toolset (condensed)", "unreal_describe",
                       {"project": live, "toolset": STUDIO}, "audit_character_conformance")
            await step("describe one tool (full schema)", "unreal_describe",
                       {"project": live, "toolset": STUDIO, "tool": "audit_character_conformance"}, "inputSchema")
            await step("describe the largest toolset stays small", "unreal_describe",
                       {"project": live, "toolset": "NiagaraToolsets.NiagaraToolset_System"}, "tools)", 400)
            await step("call a read-only studio tool", "unreal_call",
                       {"project": live, "toolset": STUDIO, "tool": "audit_character_conformance", "arguments": {}}, "", 1500)
            await step("agent skills", "unreal_skills", {"project": live}, "", 1500)
            check("toolset listing names the studio toolset", STUDIO in ts)
    return failures


if __name__ == "__main__":
    live = sys.argv[sys.argv.index("--live") + 1] if "--live" in sys.argv else None
    sys.exit(1 if asyncio.run(main(live)) else 0)
