#!/usr/bin/env python3
"""mcp_census — find every MCP registration that bypasses dangler, and say what to do with it.

The rule (docs/ingestion.md): dangler is the funnel for ALL MCP usage. A `.mcp.json`
found in a project is therefore not configuration, it is an ingestion ticket: its
target service should be wrapped by dangler and the file should then stop registering it.

This script is the dumb half of that rule. It walks the dev drives, reads every
`.mcp.json`, compares against the live fleet in ~/.dangler/dangler.toml, and prints
one verdict per registration:

    DELETE  the target is retired; remove the entry
    REMOVE  the target is already served by a dangler fleet entry; remove the entry
    LIST    a server dangler can front as-is: a stdio `command`, or (since HTTP downstream
            shipped) a `url` with its bearer in a `header_file` / `auth = "oauth"`
    WRAP    needs a first-party extension: lifecycle, a toolkit, or an identity dangler
            must supply that a plain registration cannot
    SKIP    out of scope (plugin marketplaces, vendored trees, paths in the allowlist)

Exit status is 1 while any DELETE / REMOVE / LIST / WRAP verdict remains, 0 when the
machine is clean. That exit code is the assurance: "the per-project .mcp.json is gone"
is a thing this script proves, not a thing someone remembers.

Secrets: values are never printed. Env and header entries are reported by KEY NAME and
by shape only (empty / placeholder / LITERAL). A LITERAL in a git-tracked file is
flagged loudly, because that is a credential in a repository.

Stdlib only. Python 3.11+ (tomllib).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_ROOTS = ["I:/", "F:/", "D:/", str(Path.home())]
PRUNE = {"node_modules", ".git", "$RECYCLE.BIN", "System Volume Information", "AppData", "WSL",
         "Engine", "EpicEngines", "Intermediate", "Binaries", "Saved", "DerivedDataCache",
         ".venv", "venv", "site-packages", "target", "Windows", "Program Files",
         "Program Files (x86)"}

# Path fragments that are never ours to ingest. Extend with --skip or in ~/.dangler/census.skip.
DEFAULT_SKIP = ["/.claude/plugins/", "/claude-plugins-official/"]

# Targets the studio has retired. A registration matching one is deleted, not wrapped.
RETIRED = [(re.compile(r"GenerativeAISupport", re.I), "GenerativeAISupport bridge (:9877), retired")]

# How a registration's target maps onto a fleet entry when the names differ.
# Left: regex over the target string. Right: the dangler.toml server name that wraps it.
WRAPPED_BY = [
    (re.compile(r"127\.0\.0\.1:8000/mcp|localhost:8000/mcp"), "unreal"),
    (re.compile(r"myevery", re.I), "myevery"),
    (re.compile(r"gws-mcp|workspace-mcp", re.I), "google"),
]

NON_SECRET_KEY = re.compile(
    r"(HOST|PORT|URL|HOME|DIR|PATH|MODE|READ_ONLY|TRANSPORT|SEAT|CONFIG|CREDENTIALS|PROJECT_ID|_FILE)$", re.I)
# A filesystem path is a pointer, not a credential — the file it names may be one.
LOOKS_LIKE_PATH = re.compile(r"^([A-Za-z]:[\\/]|[\\/]|~[\\/]|\./)")
PLACEHOLDER = re.compile(r"^\s*(Bearer\s+)?\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}\s*$")


def find_files(roots: list[str], max_depth: int):
    for root in roots:
        if not os.path.isdir(root):
            continue
        base = root.replace("\\", "/").rstrip("/").count("/")
        for cur, dirs, files in os.walk(root):
            depth = cur.replace("\\", "/").rstrip("/").count("/") - base
            dirs[:] = [d for d in dirs if d not in PRUNE and not d.startswith("_PURGE")]
            if depth >= max_depth:
                dirs[:] = []
            if ".mcp.json" in files:
                yield os.path.join(cur, ".mcp.json").replace("\\", "/")


def git_tracked(path: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", os.path.dirname(path), "ls-files", "--error-unmatch", ".mcp.json"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def load_fleet(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with open(path, "rb") as fh:
        return set((tomllib.load(fh).get("servers") or {}).keys())


def shape(key: str, value: str) -> str:
    v = str(value)
    if not v.strip():
        return "empty"
    if PLACEHOLDER.match(v):
        return "placeholder"
    if LOOKS_LIKE_PATH.match(v.strip()):
        return "path"
    return "literal" if NON_SECRET_KEY.search(key) else "LITERAL"


def target_of(server: dict) -> tuple[str, str]:
    if "url" in server or server.get("type") in ("http", "sse"):
        url = str(server.get("url", ""))
        return "http", (url.split("?", 1)[0] + "?<redacted>") if "?" in url else url
    parts = [str(server.get("command", ""))] + [str(a) for a in server.get("args", [])]
    return "stdio", " ".join(parts)


def verdict(name: str, kind: str, target: str, fleet: set[str]) -> tuple[str, str]:
    # dangler itself is the funnel, not a ticket: it is the one registration a
    # client is supposed to hold.
    if name == "dangler" or "dangler" in Path(target.split()[0]).name.lower():
        return "FUNNEL", "dangler itself — the one registration a client should hold"
    for pat, why in RETIRED:
        if pat.search(target):
            return "DELETE", why
    if name in fleet:
        return "REMOVE", f"fleet already serves `{name}`"
    for pat, fleet_name in WRAPPED_BY:
        if pat.search(target):
            if fleet_name in fleet:
                return "REMOVE", f"fleet serves this as `{fleet_name}`"
            return "WRAP", f"needs extension `{fleet_name}` (not in the fleet yet)"
    if kind == "stdio":
        return "LIST", "stdio: list in dangler.toml, move env secrets to referenced files"
    # Since HTTP downstream shipped, a hosted endpoint is an ordinary fleet entry: a
    # `url`, with a static bearer in `header_file` or `auth = "oauth"` when the endpoint
    # answers 401 with a challenge. It only earns WRAP if it needs more than a request.
    return "LIST", "http: list in dangler.toml as `url` + `header_file` (static bearer) or `auth = \"oauth\"`"


def client_registrations(path: Path):
    """The client's OWN registrations, which no `.mcp.json` shows.

    `~/.claude.json` carries a top-level `mcpServers` map and one per project
    under `projects.<dir>.mcpServers`. These are the servers a session actually
    loads, so a machine can pass a clean `.mcp.json` sweep while every real tool
    still bypasses dangler. Yields (label, servers) like a file would.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return
    if doc.get("mcpServers"):
        yield f"{path}  (client, user scope)", doc["mcpServers"]
    for project, cfg in (doc.get("projects") or {}).items():
        if isinstance(cfg, dict) and cfg.get("mcpServers"):
            yield f"{path}  (client, project scope: {project})", cfg["mcpServers"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", help="directory to scan (repeatable); default: the dev drives")
    ap.add_argument("--skip", action="append", default=[], help="path fragment to treat as out of scope")
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--fleet", default=str(Path.home() / ".dangler" / "dangler.toml"))
    ap.add_argument("--client", default=str(Path.home() / ".claude.json"),
                    help="the client config whose own registrations count too (default ~/.claude.json)")
    ap.add_argument("--quiet-skips", action="store_true", help="do not print SKIP rows")
    args = ap.parse_args()

    skips = DEFAULT_SKIP + args.skip
    skipfile = Path.home() / ".dangler" / "census.skip"
    if skipfile.is_file():
        skips += [ln.strip() for ln in skipfile.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]

    fleet = load_fleet(Path(args.fleet))
    print(f"fleet ({args.fleet}): {', '.join(sorted(fleet)) or '(none)'}\n")

    open_tickets = leaked = 0
    sources: list[tuple[str, dict, bool, bool]] = []  # label, servers, tracked, worktree
    for label, servers in client_registrations(Path(args.client)):
        sources.append((label, servers, False, False))
    for path in find_files(args.root or DEFAULT_ROOTS, args.depth):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                servers = json.load(fh).get("mcpServers") or {}
        except Exception as exc:  # noqa: BLE001
            print(f"{path}\n    UNREADABLE ({type(exc).__name__})\n")
            continue
        if servers:
            sources.append((path, servers, git_tracked(path), "/.claude/worktrees/" in path))

    for path, servers, tracked, worktree in sources:
        skipped = next((s for s in skips if s.lower() in path.lower()), None)
        if skipped and args.quiet_skips:
            continue
        client = "(client," in path
        tags = ", ".join(t for t in ("client registration" if client else ("tracked" if tracked else "untracked"),
                                     "worktree copy, follows its parent" if worktree else "") if t)
        print(f"{path}  ({tags})" if not client else f"{path}")
        for name, server in servers.items():
            kind, target = target_of(server)
            if skipped:
                v, why = "SKIP", f"out of scope ({skipped})"
            else:
                v, why = verdict(name, kind, target, fleet)
                if not worktree and v != "FUNNEL":
                    open_tickets += 1
            print(f"    {v:6} {name:16} {kind:5} {target[:120]}")
            print(f"           {why}")
            for block in ("env", "headers"):
                for key, value in (server.get(block) or {}).items():
                    s = shape(key, value)
                    flag = ""
                    if s == "LITERAL":
                        flag = "   <-- CREDENTIAL IN A TRACKED FILE" if tracked else "   <-- move to a referenced file"
                        leaked += 1
                    print(f"           {block}.{key}: {s}{flag}")
        print()

    print(f"{open_tickets} open ingestion ticket(s); {leaked} literal secret(s)")
    return 1 if (open_tickets or leaked) else 0


if __name__ == "__main__":
    sys.exit(main())
