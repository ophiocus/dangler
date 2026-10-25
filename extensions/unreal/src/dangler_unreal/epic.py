"""Client for Epic's Unreal MCP server (ModelContextProtocol plugin, UE 5.8).

Epic's server speaks streamable HTTP at http://127.0.0.1:<port>/mcp and nothing else. This is
a deliberately small stdlib client. What it relies on, and where each fact comes from:

- Clients must target the /mcp path. A client on the bare port sees the connection drop.
- The Accept header must list BOTH application/json and text/event-stream, or the server 406s.
- Framing, read from the 5.8.2 source (ModelContextProtocolServer.cpp): initialize, tools/list
  and an ordinary tools/call answer with ONE application/json body framed by Content-Length.
  Only a call that carries a progressToken streams text/event-stream, with no Content-Length and
  no chunking, and the server closes the socket after the final event. The studio's 2026-08-22
  measurement (an SSE frame with no length on every call, so urllib read an empty body) was taken
  on an earlier 5.8 build. This client reads raw HTTP over a socket and handles all three cases
  — length-framed JSON, SSE read up to the event that answers our id, and read-to-close — so it
  does not depend on which build answers.
- Keep-alive: the request says Connection: keep-alive. With Connection: close an SSE answer can
  be torn down before the event is written (same measurement).
- Sessions: every request after initialize names its Mcp-Session-Id. An unknown id is a 404
  ("client should reinitialize"), which is what an editor restart on the same port looks like;
  the session is reopened once, before anything ran. One session is kept per port.
- Tool search is on by default: tools/list returns only list_toolsets, describe_toolset and
  call_tool. Arguments are toolset_name / tool_name / arguments — not toolset / tool.
- Every call runs on the editor's game thread. Overlapping calls deadlock or fail, so this
  module serialises all calls to one port behind a lock, whatever the caller does.

No secrets: Epic's server has no authentication at all.
"""
from __future__ import annotations

import json
import socket
import threading
import time

HOST = "127.0.0.1"
PATH = "/mcp"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "dangler-unreal", "version": "0.1.0"}

_locks: dict[int, threading.Lock] = {}
_sessions: dict[int, "Session"] = {}
_guard = threading.Lock()


def port_lock(port: int) -> threading.Lock:
    with _guard:
        return _locks.setdefault(port, threading.Lock())


class EpicError(RuntimeError):
    """The server answered and said no (JSON-RPC error, HTTP error)."""


class Unreachable(ConnectionError):
    """Nothing answered on the port."""


class StaleSession(EpicError):
    """The server does not know our Mcp-Session-Id (404). Nothing ran; reinitialize."""


def url_for(port: int) -> str:
    return f"http://{HOST}:{port}{PATH}"


# ---------------------------------------------------------------- wire

def _sse_messages(text: str) -> list[dict]:
    """The JSON payloads of every complete event in an SSE body."""
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(ln[5:].lstrip() for ln in block.splitlines() if ln.startswith("data:"))
        if not data:
            continue
        try:
            out.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return out


def _dechunk(body: bytes) -> tuple[bytes, bool]:
    """Decode a chunked body as far as it has arrived. Returns (data, complete)."""
    out, i = b"", 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            return out, False
        try:
            size = int(body[i:j].split(b";")[0], 16)
        except ValueError:
            return out, False
        if size == 0:
            return out, True
        if len(body) < j + 2 + size + 2:
            return out, False
        out += body[j + 2:j + 2 + size]
        i = j + 2 + size + 2


def post(port: int, payload: dict, session_id: str | None, protocol: str, timeout: float) -> tuple[int, dict, dict | None]:
    """One POST to /mcp. Returns (status, headers, message).

    message is the JSON-RPC answer to payload's id, or None for a notification / empty body.
    Returns as soon as that answer is complete, whatever the server does with the socket after.
    """
    body = json.dumps(payload).encode("utf-8")
    lines = [f"POST {PATH} HTTP/1.1", f"Host: {HOST}:{port}", "Content-Type: application/json",
             "Accept: application/json, text/event-stream", f"MCP-Protocol-Version: {protocol}"]
    if session_id:
        lines.append(f"Mcp-Session-Id: {session_id}")
    lines += [f"Content-Length: {len(body)}", "Connection: keep-alive", "", ""]
    want = payload.get("id")
    try:
        sock = socket.create_connection((HOST, port), timeout=min(timeout, 10.0))
    except OSError as exc:
        raise Unreachable(f"nothing answers on {url_for(port)} ({exc})") from None
    buf, status, headers, start = b"", 0, {}, -1
    deadline = time.monotonic() + timeout
    closed = False
    try:
        sock.sendall("\r\n".join(lines).encode("ascii") + body)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise EpicError(f"{payload.get('method')}: no complete answer within {timeout:.0f}s "
                                f"(HTTP {status or '—'}, {max(len(buf) - start, 0)} body bytes so far)")
            sock.settimeout(left)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                if start < 0:
                    raise Unreachable(f"{url_for(port)} dropped the connection ({exc})") from None
                closed = True
                chunk = b""
            if not chunk:
                closed = True
            buf += chunk
            if start < 0:
                end = buf.find(b"\r\n\r\n")
                if end < 0:
                    if closed:
                        raise Unreachable(f"{url_for(port)} closed the connection before answering")
                    continue
                head = buf[:end].decode("iso-8859-1").split("\r\n")
                status = int(head[0].split()[1])
                for ln in head[1:]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                start = end + 4
            raw = buf[start:]
            done = closed
            if "chunked" in headers.get("transfer-encoding", "").lower():
                raw, done = _dechunk(raw)
                done = done or closed
            elif "content-length" in headers:
                n = int(headers["content-length"])
                done = len(raw) >= n
                raw = raw[:n]
            text = raw.decode("utf-8", errors="replace")
            if status in (202, 204) and ("content-length" in headers or done):
                return status, headers, None
            if "text/event-stream" in headers.get("content-type", ""):
                for msg in _sse_messages(text):
                    if want is None or msg.get("id") == want:
                        return status, headers, msg
                if done:
                    return status, headers, None
                continue
            if not done:
                continue
            if status >= 400:
                try:
                    detail = json.loads(text)
                except json.JSONDecodeError:
                    detail = None
                return status, headers, detail if isinstance(detail, dict) else {"_http": text[:500]}
            if not text.strip():
                return status, headers, None
            try:
                return status, headers, json.loads(text)
            except json.JSONDecodeError:
                raise EpicError(f"HTTP {status}: not JSON: {text[:300]!r}") from None
    finally:
        sock.close()


# ---------------------------------------------------------------- protocol

class Session:
    """One MCP session against one editor port."""

    def __init__(self, port: int):
        self.port = port
        self.session_id: str | None = None
        self.protocol = PROTOCOL_VERSION
        self.server_info: dict = {}
        self._id = 0

    def rpc(self, method: str, params: dict | None, timeout: float) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        status, headers, msg = post(self.port, payload, self.session_id, self.protocol, timeout)
        sid = headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if status == 404 and self.session_id and method != "initialize":
            raise StaleSession(f"{method}: the editor does not know session {self.session_id}")
        if msg is None:
            raise EpicError(f"{method}: HTTP {status} with no JSON-RPC answer")
        if "error" in msg:
            raise EpicError(f"{method}: {json.dumps(msg['error'])[:800]}")
        if "_http" in msg or status >= 400:
            raise EpicError(f"{method}: HTTP {status} from {url_for(self.port)}: {msg.get('_http', msg)}")
        return msg.get("result", {})

    def open(self, timeout: float) -> "Session":
        self.session_id = None
        result = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                         "clientInfo": CLIENT_INFO}, timeout)
        self.server_info = result.get("serverInfo", {})
        self.protocol = result.get("protocolVersion") or PROTOCOL_VERSION
        try:
            post(self.port, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                 self.session_id, self.protocol, timeout)
        except (EpicError, Unreachable):
            pass
        return self


def _with_session(port: int, timeout: float, fn):
    """Run fn(session) on the port's session, opening it on first use and once more if stale.
    Serialised per port. A StaleSession is raised before the server ran anything, so the retry
    can never run a tool twice."""
    with port_lock(port):
        s = _sessions.get(port)
        fresh = s is None
        if fresh:
            s = _sessions[port] = Session(port).open(timeout)
        try:
            return fn(s)
        except StaleSession:
            if fresh:
                raise
            s = _sessions[port] = Session(port).open(timeout)
            return fn(s)
        except Unreachable:
            _sessions.pop(port, None)
            raise


def run(port: int, timeout: float, name: str, arguments: dict | None = None) -> dict:
    """One tools/call; returns the raw MCP result."""
    return _with_session(port, timeout, lambda s: s.rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout))


def probe(port: int, timeout: float = 5.0) -> dict:
    """tools/list on the port's session. Returns {'server': ..., 'tools': [...]} or raises Unreachable."""
    def go(s: Session) -> dict:
        return {"server": s.server_info, "protocol": s.protocol,
                "tools": [t.get("name") for t in s.rpc("tools/list", {}, timeout).get("tools", [])]}
    return _with_session(port, timeout, go)


def forget(port: int) -> None:
    with _guard:
        _sessions.pop(port, None)


def text_of(result: dict) -> str:
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------- condensing

def condense_toolset(payload: str) -> str:
    """describe_toolset, condensed to one signature line per tool.

    Raw describe_toolset is the complete JSON Schema of every tool. On 5.8.2 that runs from
    ~2k characters to ~278k (NiagaraToolset_System), and clients silently drop the middle of an
    oversized result, so an agent sees a partial list with no sign anything is missing. One line
    per tool fits every client; describe one tool to get its full schema.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    name = data.get("name", "?")
    lines = [f"{name}  ({len(data.get('tools', []))} tools)"]
    desc = (data.get("description") or "").strip().splitlines()
    if desc:
        lines.append(f"  {desc[0].strip()}")
    lines.append("")
    prefix = name + "."
    for tool in data.get("tools", []):
        tname = tool.get("name", "?").removeprefix(prefix)
        schema = tool.get("inputSchema") or {}
        required = set(schema.get("required") or [])
        params = []
        for pname, spec in (schema.get("properties") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            if "enum" in spec:
                kind = "enum(" + "|".join(str(v) for v in spec["enum"][:8]) + ("|…" if len(spec["enum"]) > 8 else "") + ")"
            elif "refPath" in (spec.get("properties") or {}):
                kind = "ref<" + ((spec.get("title") or "Object").split(".")[-1]) + ">"
            else:
                kind = spec.get("type") or spec.get("title") or "any"
                if kind == "array":
                    items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
                    inner = "ref" if "refPath" in (items.get("properties") or {}) else items.get("type", "any")
                    kind = f"[{inner}]"
            p = f"{pname}: {kind}" if pname in required else f"{pname}?: {kind}"
            if "default" in spec:
                p += f" = {json.dumps(spec['default'])}"
            params.append(p)
        first = (tool.get("description") or "").strip().splitlines()
        lines.append(f"- {tname}({', '.join(params)})")
        if first:
            lines.append(f"    {first[0].strip()[:160]}")
    lines += ["", "ref<X> means {\"refPath\": \"/Game/Path/Asset.Asset\"}; a class is {\"refPath\": \"/Script/Module.Class\"}.",
              "Pass tool=<name> for one tool's full schema."]
    return "\n".join(lines)


def one_tool(payload: str, tool: str) -> str:
    """The full input schema of one tool from a describe_toolset payload."""
    data = json.loads(payload)
    name = data.get("name", "")
    for t in data.get("tools", []):
        tname = t.get("name", "")
        if tname == tool or tname.removeprefix(name + ".") == tool:
            return json.dumps({"name": tname, "description": t.get("description"),
                               "inputSchema": t.get("inputSchema")}, indent=1)
    names = ", ".join(t.get("name", "").removeprefix(name + ".") for t in data.get("tools", []))
    raise EpicError(f"{tool!r} is not a tool of {name}. Its tools: {names}")
