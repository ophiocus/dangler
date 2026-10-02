"""HTTP client for the krita_mcp_bridge plugin, plus the "is Krita up, start it if not" logic.

Discovery: the plugin writes ~/.krita-mcp/bridge.json (port, pid) and ~/.krita-mcp/bridge.token
(a per-session bearer) when it starts. Both are read by reference on every call — the token is
never cached, so a Krita restart (new token) needs no server restart.

Environment (all optional; the server starts and lists tools with none of them):
  KRITA_MCP_HOME         discovery dir (default ~/.krita-mcp) — must match the plugin's
  KRITA_MCP_URL          override the bridge URL (default: port from bridge.json, else 9797)
  KRITA_MCP_TOKEN_FILE   override the token path
  KRITA_EXE              krita.exe to launch when nothing answers (default Program Files)
  KRITA_EXPORT_ROOTS     ';'-separated folders exports may be written under (default ~/Pictures/krita-mcp)
  KRITA_READ_ONLY=1      refuse every tool that changes a document or writes a file
  KRITA_ALLOW_EXEC=1     expose krita_run_python (the plugin must also have ~/.krita-mcp/allow_exec)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("KRITA_MCP_HOME") or Path.home() / ".krita-mcp").expanduser()
TOKEN_FILE = Path(os.environ.get("KRITA_MCP_TOKEN_FILE") or HOME / "bridge.token").expanduser()
DISCOVERY = HOME / "bridge.json"
URL_OVERRIDE = os.environ.get("KRITA_MCP_URL", "").rstrip("/")
KRITA_EXE = Path(os.environ.get("KRITA_EXE") or r"C:\Program Files\Krita (x64)\bin\krita.exe")
READ_ONLY = os.environ.get("KRITA_READ_ONLY", "") not in ("", "0", "false")
ALLOW_EXEC = os.environ.get("KRITA_ALLOW_EXEC", "") == "1"
EXPORT_ROOTS = [Path(p).expanduser().resolve() for p in
                (os.environ.get("KRITA_EXPORT_ROOTS") or str(Path.home() / "Pictures" / "krita-mcp")).split(";") if p.strip()]
LAUNCH_WAIT = float(os.environ.get("KRITA_LAUNCH_WAIT", "90"))

SETUP = ("Krita's bridge plugin is not answering. Install it once with "
         "`uv run --directory I:/dangler/extensions/krita krita-mcp install-plugin` (Krita closed), "
         "then start Krita (krita_status starts it for you when KRITA_EXE exists). If Krita is open and "
         "this persists, enable 'Krita MCP Bridge' in Settings > Configure Krita > Python Plugin Manager "
         "and restart Krita; the plugin log is ~/.krita-mcp/bridge.log.")


def log(*a: Any) -> None:
    print("[krita-mcp]", *a, file=sys.stderr, flush=True)


class BridgeError(RuntimeError):
    """The bridge answered, and said no (bad args, Krita refused, plugin error)."""


class BusyError(BridgeError):
    """The bridge answered krita_busy: the GUI thread did not get to the request in time."""


def url() -> str:
    if URL_OVERRIDE:
        return URL_OVERRIDE
    try:
        port = int(json.loads(DISCOVERY.read_text(encoding="utf-8")).get("port") or 9797)
    except (OSError, ValueError):
        port = 9797
    return f"http://127.0.0.1:{port}"


def _token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        raise ConnectionError(f"no bridge token at {TOKEN_FILE}; " + SETUP) from None


def health(timeout: float = 3.0) -> dict:
    req = urllib.request.Request(url() + "/health", headers={"Host": "127.0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ConnectionError(str(e)) from None


def call(cmd: str, args: dict | None = None, timeout: float = 30.0) -> Any:
    """One bridge command. Raises ConnectionError (bridge down), BusyError, or BridgeError."""
    body = json.dumps({"cmd": cmd, "args": args or {}, "timeout": timeout}).encode()
    req = urllib.request.Request(url() + "/call", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + _token(),
                                          "Content-Type": "application/json", "Host": "127.0.0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            data = {"error": f"HTTP {e.code}"}
        msg = data.get("error") or f"HTTP {e.code}"
        if e.code == 503 or str(msg).startswith("krita_busy"):
            raise BusyError(msg) from None
        if e.code == 401:
            raise BridgeError(f"{msg} — Krita was probably restarted; the token file is re-read on every call, "
                              f"so retry once. If it persists, {SETUP}") from None
        tb = data.get("traceback")
        raise BridgeError(msg + (f"\n{tb.strip().splitlines()[-1]}" if tb else "")) from None
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(str(e)) from None
    if not data.get("ok"):
        raise BridgeError(str(data.get("error")))
    return data.get("result")


# ------------------------------------------------------------------ process management

def krita_running() -> bool:
    if os.name != "nt":
        return False
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq krita.exe", "/NH", "/FO", "CSV"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return "krita.exe" in (r.stdout or "").lower()


def _spawn_detached_windows(cmd: list[str], cwd: str) -> None:
    """Start a process that outlives this one on Windows (lifted from the comfy extension).

    MCP clients hold their servers in a job object that kills every descendant, and dangler
    reaps idle servers; a Krita started as our child would die with us. WMI creates the
    process from its own provider host, outside our job and our tree."""
    q = lambda v: "'" + v.replace("'", "''") + "'"  # noqa: E731
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]1}; "
          f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine={q(subprocess.list2cmdline(cmd))}; CurrentDirectory={q(cwd)}; ProcessStartupInformation=$si}}; "
          "if ($r.ReturnValue -ne 0) { throw \"Win32_Process.Create returned $($r.ReturnValue)\" }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        raise BridgeError("could not start Krita: " + (r.stderr or r.stdout).strip()[:400])


def ensure(launch: bool = True) -> dict:
    """Return the bridge's /health; start Krita if nothing answers and that is allowed."""
    try:
        return health()
    except ConnectionError:
        pass
    if krita_running():
        raise BridgeError("Krita is running but the bridge is not answering on " + url() + ". " + SETUP)
    if not launch:
        raise BridgeError("Krita is not running. " + SETUP)
    if not KRITA_EXE.is_file():
        raise BridgeError(f"Krita is not running and KRITA_EXE ({KRITA_EXE}) does not exist. " + SETUP)
    log("starting Krita:", KRITA_EXE)
    cmd = [str(KRITA_EXE), "--nosplash"]
    if os.name == "nt":
        _spawn_detached_windows(cmd, str(KRITA_EXE.parent))
    else:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    end = time.time() + LAUNCH_WAIT
    while time.time() < end:
        time.sleep(1.0)
        try:
            return health()
        except ConnectionError:
            continue
    raise BridgeError(f"started Krita but the bridge did not answer within {LAUNCH_WAIT:.0f}s. " + SETUP)


def check_export_path(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = EXPORT_ROOTS[0] / p
    p = p.resolve()
    for root in EXPORT_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise BridgeError(f"{p} is outside the export roots {[str(r) for r in EXPORT_ROOTS]} "
                      f"(widen KRITA_EXPORT_ROOTS in the fleet config, or write under {EXPORT_ROOTS[0]})")
