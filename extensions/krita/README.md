# krita-mcp — Krita as the canvas

Two halves, one contract:

| Half | Where | Runtime | Job |
|---|---|---|---|
| `krita_mcp_bridge` (pykrita plugin) | `plugin/`, installed into `%APPDATA%\krita\pykrita\` | Krita's embedded Python (3.8 on 5.1, 3.10 on 5.2, 3.13 on 5.3/6.0), PyQt5 or PyQt6, **stdlib only** | loopback HTTP on `127.0.0.1:9797+`, per-session bearer token, a fixed command catalog executed on Krita's GUI thread with a deadline |
| `krita-mcp` (this package) | `src/krita_mcp/`, run by `uv` from the dangler fleet | Python ≥ 3.11, Anthropic's `mcp` SDK 1.x | stdio MCP server: 14 coarse tools → bridge commands; PNG previews as image content; export-root allow-list; starts Krita detached when nothing answers |

Generation is not here. The `comfy` fleet server makes images; this server exports regions for it (control images, masks) and lands its results as paint layers.

## Install (once per machine, Krita closed)

```bash
uv run --directory I:/dangler/extensions/krita krita-mcp install-plugin
```

That copies the plugin, writes `enable_krita_mcp_bridge=true` into kritarc, and creates `~/.krita-mcp/`. Then register the fleet entry (below) and call `krita_status` through dangler — it starts Krita if needed. `krita-mcp plugin-status` and `krita-mcp uninstall-plugin` exist too.

## Fleet entry

```toml
[servers.krita]
command = "uv"
args = ["run", "--directory", "I:/dangler/extensions/krita", "krita-mcp"]
identity = "Krita 5.3 on this workstation, as the signed-in desktop user — the canvas where images are composed, masked, edited and exported"
setup_hint = "run `uv run --directory I:/dangler/extensions/krita krita-mcp install-plugin` once with Krita closed; KRITA_EXE points at krita.exe so krita_status can start it"
[servers.krita.env]
KRITA_EXE = "C:/Program Files/Krita (x64)/bin/krita.exe"
KRITA_EXPORT_ROOTS = "C:/Users/Carlos/Pictures/krita-mcp;I:/"
```

Environment: `KRITA_MCP_HOME` (discovery dir, default `~/.krita-mcp`), `KRITA_MCP_URL`, `KRITA_MCP_TOKEN_FILE`, `KRITA_EXE`, `KRITA_EXPORT_ROOTS` (`;`-separated), `KRITA_READ_ONLY=1`, `KRITA_ALLOW_EXEC=1` (plus the plugin-side file `~/.krita-mcp/allow_exec`) for `krita_run_python`. The plugin reads `KRITA_MCP_PORT`, `KRITA_MCP_HOME` from Krita's own environment, and the files `~/.krita-mcp/allow_exec` / `any_action` as switches — a user-launched Krita carries no env from us.

## Wire protocol

`GET /health` (no auth) → `{ok, bridge_version, krita_version, python, qt_major, capabilities, commands, port, pid}`.
`POST /call` with `Authorization: Bearer <~/.krita-mcp/bridge.token>` and `{"cmd": "layer.create", "args": {...}, "timeout": 30}` → `{"ok": true, "result": ...}` or `{"ok": false, "error": ..., "traceback": ...}`; `503` + `krita_busy: …` when the GUI thread did not get to it in time. Non-loopback `Host` headers are refused (DNS rebinding); bodies are capped at 64 MB.

## Rules paid for by the fifteen bridges before this one

- **Never `krita.exe --export` while a Krita window is open** — it hangs forever (Krita 5 is single-instance and does not forward `--export`). The bridge exports from inside.
- **Every libkis call on the GUI thread, every wait bounded.** Worker threads only enqueue.
- **Wrappers never cross threads or requests.** Documents and layers are addressed by UUID; results are JSON.
- **Pixel tools need RGBA/U8** (`document.convert` fixes it). Krita's raw RGBA/U8 buffer is B,G,R,A — byte-identical to `QImage.Format_ARGB32`, so QImage ↔ Krita needs no swizzle.
- **Opacity is 0–1 here, degrees here** — libkis mixes 0–255, radians and degrees; the catalog normalises.
- **Save before close.** Krita itself crashes on `Document.close()` about one time in five after a long session (reproducible from File → Close); the `close` op refuses unsaved changes unless told to discard.
- **`krita_draw` bypasses undo and the brush engine** by design (QPainter → `setPixelData`); `krita_paint` is the real thing and needs Krita ≥ 5.3.
- **No arbitrary Python by default.** `krita_run_python` is double-gated (server env + plugin file). The QAction allow-list excludes anything that opens a dialog.

## Why `mcp` 1.x and the low-level `Server`

The two Python siblings (`comfy`, `google`) pin `mcp>=1.2,<2` and hand-write their schemas on `mcp.server.lowlevel.Server`, and that stack is proven through dangler on this Windows box. `ImageContent` exists in 1.x, progress is not needed (calls are bounded and synchronous), so the 2.x rewrite buys nothing here yet; each extension has its own `uv` venv, so moving later is a one-line pin.

## Tested on

Krita 5.3.4 (Python 3.13, PyQt5) on Windows 11. The plugin is written to 3.8 syntax with scoped Qt enums so it also loads on 5.1/5.2 (no `krita_paint` there) and on 6.x (PyQt6), but only 5.3.4 has been exercised.
