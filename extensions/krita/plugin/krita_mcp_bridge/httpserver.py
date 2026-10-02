"""Loopback HTTP listener for the bridge (stdlib only — Krita ships Qt5Network but no
QtWebSockets, and nothing third-party can be pip-installed into the embedded Python).

Security stance for an unauthenticated desktop port, as the MCP spec's local-server guidance
and every surviving Krita bridge converged on:
  * bind 127.0.0.1 only (never 0.0.0.0);
  * per-Krita-session random bearer token, minted into ~/.krita-mcp/bridge.token (0600 where
    the OS honours it) and required on every call except GET /health;
  * reject any Host header that is not loopback (DNS-rebinding guard);
  * cap request bodies (64 MB) and run every Krita call with a deadline.

Wire protocol (JSON):
  GET  /health                       -> {"ok": true, "service": "krita-mcp-bridge", ...}   no auth
  POST /call  {"cmd","args","timeout"} -> {"ok": true, "result": ...}
                                          {"ok": false, "error": "...", "traceback": "..."} (500)
                                          {"ok": false, "error": "krita_busy: ..."}          (503)
"""
import hmac
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 600.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _write_private(path, text):
    d = os.path.dirname(path)
    if not os.path.isdir(d):
        os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


class BridgeServer(object):
    def __init__(self, invoker, log, home, port, scan=24, info=None):
        self.invoker = invoker
        self.log = log
        self.home = home
        self.token = secrets.token_urlsafe(32)
        self.port = None
        self.info = info or {}
        self._httpd = None
        self._thread = None
        self._port_pref = port
        self._scan = scan
        self.started = time.time()

    # ---- lifecycle ----------------------------------------------------------------
    def start(self):
        outer = self
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            server_version = "krita-mcp-bridge/0.1"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):     # keep Krita's console quiet; we have our own log
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(body)

            def _loopback(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                if host.startswith("[::1]"):
                    return True
                return host in ("127.0.0.1", "localhost")

            def _authed(self):
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("Bearer "):
                    return False
                return hmac.compare_digest(auth[7:].strip(), token)

            def do_GET(self):
                if not self._loopback():
                    return self._json(403, {"ok": False, "error": "non-loopback Host header"})
                if self.path == "/health":
                    return self._json(200, dict(outer.info, ok=True, service="krita-mcp-bridge",
                                                pid=os.getpid(), port=outer.port,
                                                uptime=round(time.time() - outer.started, 1)))
                return self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if not self._loopback():
                    return self._json(403, {"ok": False, "error": "non-loopback Host header"})
                if not self._authed():
                    return self._json(401, {"ok": False, "error": "missing or wrong bearer token"})
                if self.path != "/call":
                    return self._json(404, {"ok": False, "error": "not found"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._json(400, {"ok": False, "error": "bad Content-Length"})
                if n <= 0 or n > MAX_BODY:
                    return self._json(413 if n > MAX_BODY else 400, {"ok": False, "error": "body size %d not allowed" % n})
                try:
                    req = json.loads(self.rfile.read(n).decode("utf-8"))
                    cmd = req["cmd"]
                    args = req.get("args") or {}
                    timeout = float(req.get("timeout") or DEFAULT_TIMEOUT)
                except (ValueError, KeyError, TypeError) as e:
                    return self._json(400, {"ok": False, "error": "bad request: %s" % e})
                timeout = max(1.0, min(timeout, MAX_TIMEOUT))
                job = outer.invoker.call(cmd, args, timeout)
                if job.ok:
                    return self._json(200, {"ok": True, "result": job.result})
                if job.cancelled:
                    return self._json(503, {"ok": False, "error": job.error})
                code = 404 if (job.error or "").startswith("KeyError: 'unknown command") else 500
                return self._json(code, {"ok": False, "error": job.error, "traceback": job.trace})

        last_err = None
        for port in range(self._port_pref, self._port_pref + self._scan + 1):
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                self._httpd.daemon_threads = True
                self.port = port
                break
            except OSError as e:
                last_err = e
                continue
        if self._httpd is None:
            raise OSError("no free loopback port in %d..%d: %s" % (self._port_pref, self._port_pref + self._scan, last_err))
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="krita-mcp-bridge", daemon=True)
        self._thread.start()
        self._publish()
        self.log("listening on 127.0.0.1:%d" % self.port)

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None
        self._unpublish()

    # ---- discovery files ----------------------------------------------------------
    def _publish(self):
        _write_private(os.path.join(self.home, "bridge.token"), self.token)
        _write_private(os.path.join(self.home, "bridge.json"), json.dumps(dict(
            self.info, port=self.port, pid=os.getpid(), host="127.0.0.1", started=self.started,
            hostname=socket.gethostname()), indent=1))

    def _unpublish(self):
        for name in ("bridge.json",):
            try:
                os.remove(os.path.join(self.home, name))
            except OSError:
                pass
