"""Install / remove the pykrita bridge plugin on this machine (Windows paths; Linux/macOS
resource paths are noted but untested).

What Krita needs, per its PythonPluginManager: `<lib>.desktop` and the `<lib>/` package under the
resource folder's `pykrita/`, and `enable_<lib>=true` in the `[python]` group of kritarc. kritarc
is rewritten by Krita on exit, so it is only edited while Krita is closed.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .bridge import HOME, krita_running

LIB = "krita_mcp_bridge"
SRC = Path(__file__).resolve().parents[2] / "plugin"


def resource_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "krita"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "krita"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "krita"


def kritarc() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "kritarc"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "kritarc"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kritarc"


def _set_enabled(enabled: bool) -> str:
    rc = kritarc()
    key = f"enable_{LIB}"
    lines = rc.read_text(encoding="utf-8").splitlines() if rc.is_file() else []
    out, in_python, seen_group, done = [], False, False, False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            if in_python and not done:
                out.append(f"{key}={'true' if enabled else 'false'}")
                done = True
            in_python = s == "[python]"
            seen_group = seen_group or in_python
        elif in_python and s.split("=", 1)[0].strip() == key:
            if done:
                continue
            line = f"{key}={'true' if enabled else 'false'}"
            done = True
        out.append(line)
    if not done:
        if not seen_group:
            if out and out[-1].strip():
                out.append("")
            out.append("[python]")
        out.append(f"{key}={'true' if enabled else 'false'}")
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(rc)


def install(force: bool = False) -> str:
    if krita_running():
        return "ERROR: close Krita first (kritarc is rewritten on exit, and the plugin is loaded at startup)."
    if not (SRC / f"{LIB}.desktop").is_file():
        return f"ERROR: plugin source not found at {SRC}"
    dest = resource_dir() / "pykrita"
    dest.mkdir(parents=True, exist_ok=True)
    pkg = dest / LIB
    if pkg.exists():
        shutil.rmtree(pkg)
    shutil.copytree(SRC / LIB, pkg, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(SRC / f"{LIB}.desktop", dest / f"{LIB}.desktop")
    rc = _set_enabled(True)
    HOME.mkdir(parents=True, exist_ok=True)
    return (f"installed {LIB} into {dest}\nenabled in {rc}\n"
            f"discovery dir {HOME}\nStart Krita (or call krita_status) - the bridge log is {HOME / 'bridge.log'}.")


def uninstall() -> str:
    if krita_running():
        return "ERROR: close Krita first."
    dest = resource_dir() / "pykrita"
    removed = []
    for p in (dest / LIB, dest / f"{LIB}.desktop"):
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
            removed.append(str(p))
    rc = _set_enabled(False)
    return "removed:\n" + "\n".join(removed) + f"\ndisabled in {rc}"


def status() -> str:
    dest = resource_dir() / "pykrita"
    rc = kritarc()
    enabled = "unknown"
    if rc.is_file():
        txt = rc.read_text(encoding="utf-8")
        enabled = "true" if f"enable_{LIB}=true" in txt else "false"
    return "\n".join([
        f"plugin source : {SRC}",
        f"installed     : {(dest / LIB).is_dir() and (dest / f'{LIB}.desktop').is_file()}  ({dest / LIB})",
        f"enabled       : {enabled}  ({rc})",
        f"krita running : {krita_running()}",
        f"discovery     : {HOME / 'bridge.json'} exists={ (HOME / 'bridge.json').is_file() }",
    ])
