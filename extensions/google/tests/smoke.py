"""End-to-end stdio smoke test for gws-mcp (needs a valid token: `gws-mcp auth`).

  uv run python tests/smoke.py [--write]

Without --write: initialize, tools/list, drive_search, sheets_info/read on the
first spreadsheet found (read-only).
With --write: additionally creates a throwaway spreadsheet "gws-mcp smoke
(safe to delete)", writes/appends/reads/clears cells, and creates+edits a
throwaway Doc — proving the CRUD path. It cannot delete them (no Drive write
scope by design); trash them by hand.
"""
import json, subprocess, sys, threading, time, os

WRITE = "--write" in sys.argv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cmd = ["uv", "run", "--directory", ROOT, "gws-mcp"]
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, encoding="utf-8", errors="replace")
err = []
threading.Thread(target=lambda: [err.append(l.rstrip()) for l in p.stderr], daemon=True).start()
_id = 0

def rpc(method, params=None, timeout=90):
    global _id
    _id += 1
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}) + "\n"); p.stdin.flush()
    dl = time.time() + timeout
    while time.time() < dl:
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                raise SystemExit("server died:\n" + "\n".join(err[-20:]))
            time.sleep(0.05); continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("id") == _id:
            return o
    raise SystemExit(f"timeout on {method}")

def call(name, **args):
    r = rpc("tools/call", {"name": name, "arguments": args})
    res = r.get("result") or {}
    txt = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
    flag = "ERR " if res.get("isError") or txt.startswith("Google API error") or txt.startswith("RuntimeError") else "ok  "
    print(f"{flag}{name}: {txt[:220].replace(chr(10), ' ')}")
    return txt

fails = 0
def check(cond, what):
    global fails
    if not cond:
        fails += 1; print("FAIL:", what)

rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}})
p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); p.stdin.flush()
tools = rpc("tools/list")["result"]["tools"]
print(f"tools/list: {len(tools)} -> " + ", ".join(t["name"] for t in tools))
check(len(tools) >= 19, "expected >= 19 tools")

found = call("drive_search", query="mimeType = 'application/vnd.google-apps.spreadsheet'", page_size=3)
files = json.loads(found) if found.startswith("[") else []
check(isinstance(files, list), "drive_search returns list")
if files:
    sid = files[0]["id"]
    info = call("sheets_info", spreadsheet_id=sid)
    check(info.startswith("{"), "sheets_info json")
    first_tab = json.loads(info)["sheets"][0]["title"]
    rows = call("sheets_read", spreadsheet_id=sid, range=f"'{first_tab}'!A1:D3")
    check(rows.startswith("{"), "sheets_read json")

if WRITE:
    created = call("sheets_create", title="gws-mcp smoke (safe to delete)", sheet_titles=["t"])
    ss = json.loads(created)["id"]
    check(bool(ss), "sheets_create id")
    call("sheets_write", spreadsheet_id=ss, range="t!A1:C2", values=[["a", "b", "c"], [1, 2, "=A2+B2"]])
    call("sheets_append", spreadsheet_id=ss, range="t!A1", values=[["appended", "row", "x"]])
    got = json.loads(call("sheets_read", spreadsheet_id=ss, range="t!A1:C3"))["values"]
    check(got[1][2] == "3" and got[2][0] == "appended", f"round-trip values: {got}")
    hits = json.loads(call("sheets_find", spreadsheet_id=ss, query="appended"))
    check(hits and hits[0]["cell"] == "A3", f"sheets_find -> {hits}")
    call("sheets_add_sheet", spreadsheet_id=ss, title="second")
    call("sheets_clear", spreadsheet_id=ss, range="t!A3:C3")
    doc = json.loads(call("docs_create", title="gws-mcp smoke doc (safe to delete)", text="Hello WORLD.\nSecond line.\n"))
    did = doc["id"]
    call("docs_replace", doc_id=did, find="WORLD", replace="world")
    call("docs_insert", doc_id=did, text="Appended tail.\n")
    txt = call("docs_get", doc_id=did)
    check("Hello world." in txt and "Appended tail." in txt, f"docs round-trip: {txt!r}")
    outline = json.loads(call("docs_outline", doc_id=did))
    check(len(outline["paragraphs"]) >= 3, "docs_outline paragraphs")
    out = os.path.join(ROOT, "tests", "_smoke_export.pdf")
    call("drive_export", file_id=did, fmt="pdf", out_path=out)
    check(os.path.exists(out) and os.path.getsize(out) > 500, "drive_export pdf")
    try: os.remove(out)
    except OSError: pass
    print(f"\nthrowaway files to trash by hand: spreadsheet {ss}, doc {did}")

p.kill()
print("\nRESULT:", "PASS" if fails == 0 else f"{fails} FAILURE(S)")
sys.exit(1 if fails else 0)
