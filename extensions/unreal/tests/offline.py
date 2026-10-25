"""Offline checks for dangler-unreal: no editor, no network beyond loopback.

    uv run --directory I:/dangler/extensions/unreal python tests/offline.py

A fake Epic server on a loopback port plays each framing the 5.8 server can produce:
length-framed JSON, an SSE event on a socket it never closes (the 2026-08-22 measurement),
SSE that closes after the event (5.8.2 progress calls), 202 for a notification, and a 404
for a stale session. Plus the command-line parse that once took the editor's own path for
the .uproject. Exit 0 only if every check passes.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dangler_unreal import editor, epic  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"\n    {detail}" if detail and not ok else ""), flush=True)


# ---------------------------------------------------------------- command lines

EXE = r"F:\EpicEngines\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe"
cases = [
    (rf"{EXE} I:\ue5-skeleton\UE5Skeleton.uproject -ModelContextProtocolStartServer -ModelContextProtocolPort=8001",
     r"I:\ue5-skeleton\UE5Skeleton.uproject"),
    (rf'"{EXE}" "F:\Unreal Projects\Sandbox\Sandbox.uproject"', r"F:\Unreal Projects\Sandbox\Sandbox.uproject"),
    (rf'"{EXE}" -log', None),
]
for cl, want in cases:
    got = editor.uproject_of(cl)
    check(f"uproject_of -> {want}", got == want, f"got {got!r}")

# ---------------------------------------------------------------- fake server

MODE = {"m": "json"}
SID = "sess-1"


def answer(req: dict, headers: dict) -> bytes:
    m = MODE["m"]
    if "id" not in req:
        return b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n"
    if req["method"] != "initialize" and headers.get("mcp-session-id") != SID:
        body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32600, "message": "Unknown session id"}})
        return f"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode()
    result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake", "version": "0"}} \
        if req["method"] == "initialize" else {"content": [{"type": "text", "text": f"ok:{req['method']}"}]}
    msg = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result})
    if m == "json" or req["method"] == "initialize":
        return (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nMcp-Session-Id: {SID}\r\n"
                f"Content-Length: {len(msg)}\r\n\r\n{msg}").encode()
    progress = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
    return (f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: keep-alive\r\nMcp-Session-Id: {SID}\r\n\r\n"
            f"event: message\ndata: {progress}\n\nevent: message\ndata: {msg}\n\n").encode()


def handle(conn: socket.socket) -> None:
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += conn.recv(65536)
    head, body = buf.split(b"\r\n\r\n", 1)
    hdrs = {}
    for ln in head.decode().split("\r\n")[1:]:
        k, v = ln.split(":", 1)
        hdrs[k.strip().lower()] = v.strip()
    while len(body) < int(hdrs.get("content-length", 0)):
        body += conn.recv(65536)
    conn.sendall(answer(json.loads(body), hdrs))
    if MODE["m"] == "sse-open":
        time.sleep(3)  # keep the socket open well past the client's return
    conn.close()


srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen()
PORT = srv.getsockname()[1]
threading.Thread(target=lambda: [threading.Thread(target=handle, args=(srv.accept()[0],), daemon=True).start()
                                 for _ in iter(int, 1)], daemon=True).start()

for mode in ("json", "sse-open", "sse-close"):
    MODE["m"] = mode
    epic.forget(PORT)
    t0 = time.monotonic()
    try:
        out = epic.text_of(epic.run(PORT, 10, "list_toolsets"))
    except Exception as exc:  # noqa: BLE001
        out = f"{type(exc).__name__}: {exc}"
    dt = time.monotonic() - t0
    check(f"{mode}: tools/call answered in {dt:.2f}s", out == "ok:tools/call" and dt < 2, out)

MODE["m"] = "json"
epic._sessions[PORT].session_id = "stale"
try:
    out = epic.text_of(epic.run(PORT, 10, "list_toolsets"))
except Exception as exc:  # noqa: BLE001
    out = f"{type(exc).__name__}: {exc}"
check("stale session (404) reopens once and answers", out == "ok:tools/call", out)

dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()
try:
    epic.run(dead_port, 3, "list_toolsets")
    check("closed port raises Unreachable", False, "no exception")
except epic.Unreachable:
    check("closed port raises Unreachable", True)

check("read-only heuristic", editor.is_read_only_tool("ListSkills") and editor.is_read_only_tool("get_actor")
      and not editor.is_read_only_tool("spawn_actor") and not editor.is_read_only_tool("SetProperty"))

sys.exit(1 if failures else 0)
