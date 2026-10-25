"""Which editors exist, which one owns a port, and how to start one.

Environment (all optional — the server starts and lists its tools with none of them set):
  UNREAL_PROJECTS      ';'-separated  name=<path to .uproject>@<port>  entries, e.g.
                       skeleton=I:/ue5-skeleton/UE5Skeleton.uproject@8001;fps_1=F:/.../fps_1.uproject@8000
                       The port is the project's identity: two editors cannot share one, and Epic's
                       server gives a client nothing else to tell projects apart.
  UNREAL_ENGINE_ROOT   engine install dir (contains Engine/). Default: the Epic launcher manifest
                       entry for the pinned major.minor (the Windows registry is not reliable here).
  UNREAL_PIN           the exact engine build the studio is frozen at. Default 5.8.2-56702186.
                       unreal_launch refuses to start an editor from any other build.
  UNREAL_LAUNCH_WAIT   seconds unreal_launch waits for the server. Default 240.
  UNREAL_READ_ONLY=1   unreal_call refuses every tool whose name does not read as a query.
  UNREAL_DEFAULT_PORT  the port a hand-opened editor binds. Default 8000. Read from the 5.8.2
                       source: the console command ModelContextProtocol.StartServer with no
                       argument binds Epic's constant DefaultServerPort (8000), not the .ini
                       ServerPortNumber; only auto-start and -ModelContextProtocolPort=N use
                       another. Give configured projects other ports, so two editors never
                       contend for one, and so a hand-opened editor still serves its project:
                       when nothing holds a project's own port, a call goes to this port, but
                       only if the editor holding it proves to be that project.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

LAUNCHER_MANIFEST = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Epic/UnrealEngineLauncher/LauncherInstalled.dat"
PIN = os.environ.get("UNREAL_PIN", "5.8.2-56702186")
LAUNCH_WAIT = float(os.environ.get("UNREAL_LAUNCH_WAIT", "240"))
READ_ONLY = os.environ.get("UNREAL_READ_ONLY", "") not in ("", "0", "false")
# The port an editor binds when opened by hand (ServerPortNumber in the project's .ini).
DEFAULT_PORT = int(os.environ.get("UNREAL_DEFAULT_PORT", "8000"))

# Tool names that only read. Under UNREAL_READ_ONLY everything else is refused. A name
# heuristic, stated as one: it errs toward refusing.
READ_PREFIXES = ("get", "list", "find", "describe", "read", "search", "is", "has", "query",
                 "count", "inspect", "discover", "validate", "audit", "report", "lookup")

SETUP = ("Configure the projects in the fleet entry: [servers.unreal.env] "
         "UNREAL_PROJECTS = \"name=<path to .uproject>@<port>;...\" (one port per project).")


def log(*a) -> None:
    print("[dangler-unreal]", *a, file=sys.stderr, flush=True)


class EditorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Project:
    name: str
    uproject: Path
    port: int

    @property
    def key(self) -> str:
        return _norm(str(self.uproject))


def _norm(s: str) -> str:
    return s.replace("\\", "/").strip().strip('"').lower()


def projects() -> dict[str, Project]:
    out: dict[str, Project] = {}
    used: dict[int, str] = {}
    for entry in (os.environ.get("UNREAL_PROJECTS") or "").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise EditorError(f"UNREAL_PROJECTS entry {entry!r} has no name=. " + SETUP)
        name, rest = (x.strip() for x in entry.split("=", 1))
        m = re.match(r"^(.*)@(\d{2,5})$", rest)
        if not m:
            raise EditorError(f"UNREAL_PROJECTS entry {entry!r} has no @<port>. " + SETUP)
        port = int(m.group(2))
        if port in used:
            raise EditorError(f"projects {used[port]!r} and {name!r} share port {port}; each project needs its own")
        used[port] = name
        out[name] = Project(name, Path(m.group(1).strip()), port)
    return out


def pick(name: str | None) -> Project:
    ps = projects()
    if not ps:
        raise EditorError("no projects configured. " + SETUP)
    if name:
        if name not in ps:
            raise EditorError(f"unknown project {name!r}; configured: {', '.join(ps)}")
        return ps[name]
    if len(ps) == 1:
        return next(iter(ps.values()))
    live = [p for p in ps.values() if serving_port(p)]
    if len(live) == 1:
        return live[0]
    raise EditorError(f"say which project: {', '.join(ps)}" + (f" (running: {', '.join(p.name for p in live)})" if live else " (none running)"))


# ---------------------------------------------------------------- engine and the pin

def engine_root() -> Path:
    env = os.environ.get("UNREAL_ENGINE_ROOT")
    if env:
        return Path(env)
    want = "UE_" + ".".join(PIN.split("-")[0].split(".")[:2])
    try:
        data = json.loads(LAUNCHER_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EditorError(f"cannot read {LAUNCHER_MANIFEST} ({exc}); set UNREAL_ENGINE_ROOT") from None
    for item in data.get("InstallationList", []):
        if item.get("AppName") == want:
            return Path(item["InstallLocation"])
    raise EditorError(f"no {want} in the Epic launcher manifest; set UNREAL_ENGINE_ROOT")


def engine_build(root: Path) -> str:
    try:
        v = json.loads((root / "Engine/Build/Build.version").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EditorError(f"no readable Engine/Build/Build.version under {root} ({exc})") from None
    return f"{v['MajorVersion']}.{v['MinorVersion']}.{v['PatchVersion']}-{v['Changelist']}"


def editor_exe(root: Path) -> Path:
    return root / "Engine/Binaries/Win64/UnrealEditor.exe"


# ---------------------------------------------------------------- who owns a port

def listener_pid(port: int) -> int | None:
    """PID listening on 127.0.0.1/0.0.0.0:<port>, from netstat (no elevation needed)."""
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


_cmdlines: dict[int, str] = {}


def command_line(pid: int) -> str:
    if pid in _cmdlines:
        return _cmdlines[pid]
    ps = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    cl = (r.stdout or "").strip()
    if cl:
        _cmdlines[pid] = cl
    return cl


def uproject_of(cl: str) -> str | None:
    """The .uproject argument of an editor command line. Windows argv rules, as far as paths need
    them: a quoted run is one argument, otherwise whitespace splits. Matching a regex over the whole
    line would swallow the unquoted editor path in front of it."""
    for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', cl):
        arg = quoted or bare
        if arg.lower().endswith(".uproject"):
            return arg
    return None


def owner(port: int) -> dict:
    """{'pid', 'command_line', 'uproject'} of whatever listens on the port, or {} when nothing does."""
    pid = listener_pid(port)
    if not pid:
        return {}
    cl = command_line(pid)
    return {"pid": pid, "command_line": cl, "uproject": uproject_of(cl)}


def editors() -> list[dict]:
    """Every running UnrealEditor process: [{'pid', 'uproject'}], listening or not."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='UnrealEditor.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        rows = json.loads(r.stdout) if (r.stdout or "").strip() else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    rows = rows if isinstance(rows, list) else [rows]
    return [{"pid": x.get("ProcessId"), "uproject": uproject_of(x.get("CommandLine") or "")} for x in rows]


def owns(p: Project, o: dict) -> bool:
    return bool(o and o.get("uproject") and _norm(o["uproject"]) == p.key)


def serving_port(p: Project) -> int | None:
    """The port an editor of p's project serves MCP on: its own port, else the hand-opened default
    when the editor holding that proves to be p. None when neither does."""
    if owns(p, owner(p.port)):
        return p.port
    if p.port != DEFAULT_PORT and owns(p, owner(DEFAULT_PORT)):
        return DEFAULT_PORT
    return None


def verify(p: Project) -> int:
    """The port to call for p, or raise. Every call proves which .uproject holds the port first —
    the identity check that a shared .mcp.json pointing every project at :8000 could never make."""
    port = serving_port(p)
    if port:
        return port
    o = owner(p.port)
    if not o:
        d = owner(DEFAULT_PORT) if p.port != DEFAULT_PORT else {}
        held = (f" Port {DEFAULT_PORT} is held by {d.get('uproject') or 'pid ' + str(d['pid'])}, not by this project."
                if d else "")
        raise EditorError(f"no editor of {p.name!r} serves MCP (its port {p.port} is free).{held} Call unreal_launch "
                          f"(project={p.name!r}), or in its own editor's console run ModelContextProtocol.StartServer "
                          f"{p.port}.")
    if not o["uproject"]:
        raise EditorError(f"port {p.port} is held by pid {o['pid']}, whose command line names no .uproject "
                          f"({o['command_line'][:200]!r}); refusing to guess which project it is.")
    other = next((q.name for q in projects().values() if q.key == _norm(o["uproject"])), None)
    raise EditorError(f"port {p.port} belongs to {p.name!r} but is held by {o['uproject']}"
                      + (f" (configured as {other!r})" if other else "")
                      + ". Some other editor bound this project's port. Close one of them.")


# ---------------------------------------------------------------- launching

def _spawn_detached_windows(cmd: list[str], cwd: str) -> None:
    """Start a process that outlives this one (pattern from the comfy and krita extensions).

    MCP clients hold their servers in a job object that kills every descendant, and dangler
    reaps idle servers; an editor started as our child would die with us, taking unsaved work
    with it. WMI creates the process from its own provider host, outside our job and tree."""
    q = lambda v: "'" + v.replace("'", "''") + "'"  # noqa: E731
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]1}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine={q(subprocess.list2cmdline(cmd))}; CurrentDirectory={q(cwd)}; ProcessStartupInformation=$si}}; "
          "if ($r.ReturnValue -ne 0) { throw \"Win32_Process.Create returned $($r.ReturnValue)\" }; $r.ProcessId")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise EditorError("could not start the editor: " + (r.stderr or r.stdout).strip()[:400])


def launch_command(p: Project, root: Path) -> list[str]:
    return [str(editor_exe(root)), str(p.uproject),
            "-ModelContextProtocolStartServer", f"-ModelContextProtocolPort={p.port}"]


def start(p: Project) -> list[str]:
    root = engine_root()
    build = engine_build(root)
    if build != PIN:
        raise EditorError(f"engine at {root} is {build}, not the pinned {PIN}. The studio is frozen at {PIN}; "
                          f"a different build is a recorded decision, not a launch option.")
    if not p.uproject.is_file():
        raise EditorError(f"{p.uproject} does not exist")
    exe = editor_exe(root)
    if not exe.is_file():
        raise EditorError(f"{exe} does not exist")
    cmd = launch_command(p, root)
    log("starting", p.name, "on port", p.port)
    if os.name == "nt":
        _spawn_detached_windows(cmd, str(p.uproject.parent))
    else:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, cwd=str(p.uproject.parent))
    return cmd


def wait_for_port(port: int, deadline: float) -> bool:
    while time.time() < deadline:
        if listener_pid(port):
            return True
        time.sleep(3)
    return False


def is_read_only_tool(tool: str) -> bool:
    """True when the tool's first word is a query verb: get_x, ListSkills, is_first_person."""
    first = re.sub(r"(?<!^)(?=[A-Z])", "_", tool).lower().lstrip("_").split("_", 1)[0]
    return first in READ_PREFIXES
