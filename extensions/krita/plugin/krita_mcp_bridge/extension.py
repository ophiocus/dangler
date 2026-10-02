"""The Krita Extension that hosts the bridge.

Lifecycle facts (from Krita's pykrita loader): an enabled plugin is imported at startup with
no document open; ``setup()`` runs from the plugin constructor on the GUI thread; and
``createActions(window)`` fires once per main window. The listener is started from
``setup()`` so it is up before any window exists, and guarded so a second window cannot
start a second listener.

Configuration is read from Krita's *process* environment (the MCP server passes it when it
launches Krita) or from files in ~/.krita-mcp — a user-launched Krita has no env from us:
  KRITA_MCP_PORT        preferred port (default 9797; scans upward if taken)
  KRITA_MCP_HOME        discovery/log directory (default ~/.krita-mcp)
  KRITA_MCP_ALLOW_EXEC  "1" enables the run_python command — or an empty file
                        ~/.krita-mcp/allow_exec, for a Krita the user started
  KRITA_MCP_ANY_ACTION  "1" lifts the QAction allow-list — or the file ~/.krita-mcp/any_action
"""
import os
import sys
import time

from krita import Extension, Krita

from . import commands
from .httpserver import BridgeServer
from .mainthread import Invoker
from .qtcompat import QT_MAJOR

VERSION = "0.1.0"


def _home():
    return os.environ.get("KRITA_MCP_HOME") or os.path.join(os.path.expanduser("~"), ".krita-mcp")


class _Log(object):
    def __init__(self, path):
        self.path = path

    def __call__(self, msg):
        line = "%s [krita-mcp-bridge] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 — Krita may have no stderr at all
            pass


class KritaMcpBridgeExtension(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self._server = None
        self._invoker = None
        self._log = None

    def setup(self):
        home = _home()
        if not os.path.isdir(home):
            try:
                os.makedirs(home)
            except OSError:
                pass
        self._log = _Log(os.path.join(home, "bridge.log"))
        try:
            flags = {
                "allow_exec": os.environ.get("KRITA_MCP_ALLOW_EXEC", "") == "1" or os.path.isfile(os.path.join(home, "allow_exec")),
                "any_action": os.environ.get("KRITA_MCP_ANY_ACTION", "") == "1" or os.path.isfile(os.path.join(home, "any_action")),
            }
            catalog = commands.build(flags, self._log)
            self._invoker = Invoker(catalog, self._log)
            self._invoker.start()
            info = {
                "bridge_version": VERSION,
                "krita_version": Krita.instance().version(),
                "python": "%d.%d.%d" % sys.version_info[:3],
                "qt_major": QT_MAJOR,
                "capabilities": commands.capabilities(flags),
                "commands": sorted(catalog.keys()),
            }
            port = int(os.environ.get("KRITA_MCP_PORT") or 9797)
            self._server = BridgeServer(self._invoker, self._log, home, port, info=info)
            self._server.start()
        except Exception as e:  # noqa: BLE001 — never let the plugin take Krita's startup down
            self._log("bridge failed to start: %r" % (e,))

    def createActions(self, window):
        # Nothing to add to the menus for v0; the listener already runs. Kept so the loader is
        # happy and so a later version can add a "Bridge status" action here.
        pass

    def shutdown(self):
        if self._server is not None:
            self._server.stop()
        if self._invoker is not None:
            self._invoker.stop()
