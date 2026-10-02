"""krita-mcp: a running Krita as the canvas for any Claude session.

The server owns no pixels. It forwards each tool call as one command to the krita_mcp_bridge
plugin inside Krita (loopback HTTP, per-session bearer token, every call bounded by a deadline
on Krita's GUI thread) and returns JSON — or, for krita_preview, a PNG the model can look at.

Generation is NOT here: the `comfy` fleet server makes images; this server exports regions for
it (control images, masks) and lands its results as paint layers (krita_import_image).

CLI: `krita-mcp` (stdio MCP) · `krita-mcp install-plugin` · `krita-mcp uninstall-plugin` ·
`krita-mcp plugin-status`. Logging goes to stderr; stdout is the MCP transport.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import bridge
from .bridge import ALLOW_EXEC, READ_ONLY, BridgeError, BusyError, check_export_path, log

T_QUICK, T_MED, T_LONG = 30.0, 120.0, 240.0


def _ro(op: str) -> None:
    if READ_ONLY:
        raise BridgeError(f"KRITA_READ_ONLY is set — '{op}' changes the document or writes a file and is refused")


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False)


def _pick(a: dict, *keys: str) -> dict:
    return {k: a[k] for k in keys if k in a and a[k] is not None}


# ------------------------------------------------------------------ tools

def t_status(a: dict) -> str:
    h = bridge.ensure(launch=bool(a.get("launch", True)))
    st = bridge.call("status", {}, T_QUICK)
    caps = st.get("capabilities", {})
    docs = st.get("documents", [])
    lines = [f"Krita {st['krita_version']} (Python {st['python']}, Qt{st['qt_major']}) — bridge {h.get('bridge_version')} "
             f"on {bridge.url()}, pid {h.get('pid')}, up {h.get('uptime')}s"
             f"{'; READ-ONLY' if READ_ONLY else ''}",
             f"capabilities: native paint={'yes' if caps.get('paint') else 'NO (Krita < 5.3: use krita_draw)'}, "
             f"run_python={'on' if caps.get('run_python') else 'off'}, any_action={'on' if caps.get('any_action') else 'off'}",
             f"export roots: {[str(r) for r in bridge.EXPORT_ROOTS]}",
             f"open documents: {len(docs)}" + (f" (active {st.get('active_document')})" if docs else "")]
    for d in docs:
        lines.append(f"  - {d['id']}  {d['name']!r}  {d['width']}x{d['height']} {d['color_model']}/{d['color_depth']}"
                     f"  layers={d['layer_count']}  file={d['file'] or '-'}{'  *modified' if d['modified'] else ''}")
    return "\n".join(lines)


def t_preview(a: dict) -> list[types.TextContent | types.ImageContent]:
    bridge.ensure(launch=False)
    r = bridge.call("preview", _pick(a, "doc", "layer", "x", "y", "w", "h", "max_size", "format"), T_MED)
    mime = "image/jpeg" if r.get("format") == "jpeg" else "image/png"
    note = (f"{r['width']}x{r['height']} preview of region {r['region']}"
            + (f" (downscaled from {r['scaled_from'][0]}x{r['scaled_from'][1]})" if r.get("scaled_from") else "")
            + (f", layer {a['layer']}" if a.get("layer") else ", flattened document"))
    return [types.ImageContent(type="image", data=r["base64"], mimeType=mime),
            types.TextContent(type="text", text=note)]


def t_document(a: dict) -> str:
    op = a["op"]
    if op in ("new", "open", "save", "close", "convert"):
        _ro(f"document.{op}")
    if op == "save" and a.get("path"):
        a["path"] = str(check_export_path(a["path"]))
    args = _pick(a, "doc", "path", "name", "width", "height", "color_model", "color_depth", "color_profile",
                 "dpi", "background", "save", "discard", "model", "depth", "profile")
    r = bridge.call(f"document.{op}", args, T_MED if op in ("open", "save", "close", "convert") else T_QUICK)
    if op == "close":
        return "closed: " + _j(r) + "\n(note: Krita itself occasionally crashes on close after long sessions — save first)"
    return _j(r)


def t_export(a: dict) -> str:
    _ro("export")
    path = check_export_path(a["path"])
    r = bridge.call("export", dict(_pick(a, "doc", "layer", "options", "region"), path=str(path)), T_LONG)
    return _j(r)


def t_layers(a: dict) -> str:
    if a.get("layer"):
        return _j(bridge.call("layer.info", _pick(a, "doc", "layer"), T_QUICK))
    return _j(bridge.call("layers", _pick(a, "doc"), T_QUICK))


def t_layer(a: dict) -> str:
    op = a["op"]
    if op != "activate":
        _ro(f"layer.{op}")
    if op in ("create", "set", "delete", "move", "reorder", "duplicate", "merge_down", "activate"):
        args = {k: v for k, v in a.items() if k != "op"}
        if op == "create" and args.get("path"):
            pass  # file layers read anywhere; only exports are root-restricted
        return _j(bridge.call(f"layer.{op}", args, T_MED))
    raise BridgeError("op must be one of create|set|delete|move|reorder|duplicate|merge_down|activate")


def t_import_image(a: dict) -> str:
    _ro("import_image")
    return _j(bridge.call("import_image", dict(a), T_MED))


def t_selection(a: dict) -> str:
    op = a["op"]
    if op != "get":
        _ro(f"selection.{op}")
    if op in ("invert", "grow", "shrink", "feather", "border", "smooth"):
        return _j(bridge.call("selection.modify", dict(_pick(a, "doc", "radius", "edge_lock"), op=op), T_MED))
    if op == "to_mask":
        a["path"] = str(check_export_path(a["path"]))
    if op in ("get", "rect", "ellipse", "all", "none", "from_mask", "to_mask"):
        return _j(bridge.call(f"selection.{op}", {k: v for k, v in a.items() if k != "op"}, T_MED))
    raise BridgeError("op must be one of get|rect|ellipse|all|none|invert|grow|shrink|feather|border|smooth|from_mask|to_mask")


def t_transform(a: dict) -> str:
    _ro(f"transform.{a.get('op')}")
    return _j(bridge.call("transform", dict(a), T_LONG))


def t_filter(a: dict) -> str:
    op = a.get("op", "apply")
    if op == "list":
        return _j(bridge.call("filter.list", {}, T_QUICK))
    if op == "params":
        return _j(bridge.call("filter.params", _pick(a, "name"), T_QUICK))
    _ro("filter.apply")
    return _j(bridge.call("filter.apply", _pick(a, "doc", "layer", "name", "params", "x", "y", "w", "h"), T_LONG))


def t_draw(a: dict) -> str:
    _ro("draw")
    return _j(bridge.call("draw", dict(a), T_MED))


def t_paint(a: dict) -> str:
    _ro("paint")
    return _j(bridge.call("paint", dict(a), T_LONG))


def t_color(a: dict) -> str:
    op = a.get("op", "get")
    if op == "get":
        return _j(bridge.call("brush.get", _pick(a, "doc"), T_QUICK))
    if op == "presets":
        return _j(bridge.call("presets.list", _pick(a, "filter", "limit"), T_QUICK))
    if op == "set":
        return _j(bridge.call("brush.set", {k: v for k, v in a.items() if k != "op"}, T_QUICK))
    raise BridgeError("op must be get|set|presets")


def t_action(a: dict) -> str:
    op = a.get("op", "list")
    if op == "list":
        return _j(bridge.call("action.list", _pick(a, "filter", "limit"), T_QUICK))
    _ro("action")
    aid = {"undo": "edit_undo", "redo": "edit_redo", "trigger": a.get("id")}.get(op)
    if not aid:
        raise BridgeError("op must be list|trigger|undo|redo (trigger needs id)")
    return _j(bridge.call("action.trigger", dict(_pick(a, "doc"), id=aid), T_MED))


def t_run_python(a: dict) -> str:
    _ro("run_python")
    r = bridge.call("run_python", _pick(a, "code"), T_LONG)
    out = r.get("stdout") or ""
    return (out + ("\n" if out and not out.endswith("\n") else "") + (f"result: {_j(r['result'])}" if r.get("result") is not None else "")) or "(no output)"


# ------------------------------------------------------------------ schemas (hand-written, house style)

DOC = {"type": "string", "description": "Document id from krita_status (default: the active document)."}
LAYER = {"type": "string", "description": "Layer id (or exact name) from krita_layers. Default: the active layer."}
REGION = {"x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer"}, "h": {"type": "integer"}}
COLOR = {"type": "string", "description": "'#rrggbb', '#rrggbbaa' or a colour name."}

TOOLS: dict[str, tuple[Any, types.Tool]] = {
    "krita_status": (t_status, types.Tool(
        name="krita_status",
        description="Is Krita up with the bridge answering? Starts Krita when nothing answers (WMI-detached, so it "
                    "outlives this server). Reports version, capability flags (native paint needs Krita >= 5.3), open "
                    "documents with their ids, and the export roots. Call first; also the fix when other tools say the "
                    "bridge is unreachable.",
        inputSchema={"type": "object", "properties": {
            "launch": {"type": "boolean", "description": "Start Krita if it is not running. Default true."}}})),
    "krita_preview": (t_preview, types.Tool(
        name="krita_preview",
        description="Look at the canvas: returns a PNG of the flattened document (or one layer, or a region), "
                    "downscaled so the long edge is <= max_size (default 1024). Use it between steps to see your own "
                    "work; use krita_export for the full-resolution file.",
        inputSchema={"type": "object", "properties": dict(REGION, doc=DOC, layer=LAYER,
            max_size={"type": "integer", "description": "Long edge cap in pixels. Default 1024."},
            format={"type": "string", "enum": ["png", "jpeg"], "description": "jpeg is smaller for photographic canvases."})})),
    "krita_document": (t_document, types.Tool(
        name="krita_document",
        description="Document lifecycle. op=new (width, height, name, dpi, background colour/'transparent'; RGBA/U8 "
                    "unless told otherwise), open (path: .kra/.png/.jpg/.psd/...), save (path optional — .kra keeps layers; "
                    "other formats flatten), close (save:true or discard:true), info, list, activate, convert (model, depth: "
                    "pixel tools need RGBA/U8).",
        inputSchema={"type": "object", "required": ["op"], "properties": {
            "op": {"type": "string", "enum": ["new", "open", "save", "close", "info", "list", "activate", "convert"]},
            "doc": DOC, "path": {"type": "string"}, "name": {"type": "string"},
            "width": {"type": "integer"}, "height": {"type": "integer"}, "dpi": {"type": "number", "description": "Default 300."},
            "color_model": {"type": "string", "description": "RGBA (default), GRAYA, CMYKA, LABA, XYZA, YCbCrA."},
            "color_depth": {"type": "string", "description": "U8 (default), U16, F16, F32."},
            "background": {"type": "string", "description": "new: fill for the Background layer — colour or 'transparent'. Default Krita's (white)."},
            "save": {"type": "boolean", "description": "close: save first (needs a file name)."},
            "discard": {"type": "boolean", "description": "close: drop unsaved changes."},
            "model": {"type": "string"}, "depth": {"type": "string"}}})),
    "krita_export": (t_export, types.Tool(
        name="krita_export",
        description="Write the flattened document (or one layer) to a file under the export roots: .png, .jpg, .webp, "
                    ".tif, .bmp, .kra... Extension picks the format. Options are Krita's export keys (PNG: alpha, "
                    "compression 1-9, forceSRGB; JPEG: quality, progressive). Returns path and size. This is how a "
                    "region reaches comfy.image_generate as a control_image.",
        inputSchema={"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute, or relative to the first export root."},
            "doc": DOC, "layer": {"type": "string", "description": "Export just this layer (its own pixels, no compositing)."},
            "region": {"type": "object", "properties": REGION, "description": "Layer export only: crop rectangle."},
            "options": {"type": "object", "description": "Format options, e.g. {\"alpha\": true, \"compression\": 6}."}}})),
    "krita_layers": (t_layers, types.Tool(
        name="krita_layers",
        description="The layer tree with ids, types, visibility, opacity (0-1), blend mode, bounds and paint ability, "
                    "top of the stack last. Pass layer for one node's details.",
        inputSchema={"type": "object", "properties": {"doc": DOC, "layer": LAYER}})),
    "krita_layer": (t_layer, types.Tool(
        name="krita_layer",
        description="One layer operation. op=create (type: paint|group|vector|filter|filter_mask|clone|file|"
                    "transparency_mask|selection_mask|transform_mask; name; parent; above; position:'bottom'; fill colour; "
                    "filter+params for filter layers; source for clone; path for file), set (name, visible, opacity 0-1, "
                    "blending_mode e.g. normal|multiply|screen|overlay, locked, alpha_locked, inherit_alpha), delete, "
                    "move (x,y absolute or dx,dy), reorder (parent, above, position), duplicate, merge_down, activate.",
        inputSchema={"type": "object", "required": ["op"], "properties": {
            "op": {"type": "string", "enum": ["create", "set", "delete", "move", "reorder", "duplicate", "merge_down", "activate"]},
            "doc": DOC, "layer": LAYER, "type": {"type": "string"}, "name": {"type": "string"},
            "parent": {"type": "string"}, "above": {"type": "string", "description": "Insert directly above this layer id."},
            "position": {"type": "string", "enum": ["top", "bottom"]},
            "fill": COLOR, "filter": {"type": "string"}, "params": {"type": "object"}, "source": {"type": "string"},
            "path": {"type": "string"}, "visible": {"type": "boolean"}, "opacity": {"type": "number"},
            "blending_mode": {"type": "string"}, "locked": {"type": "boolean"}, "alpha_locked": {"type": "boolean"},
            "inherit_alpha": {"type": "boolean"}, "activate": {"type": "boolean"},
            "x": {"type": "integer"}, "y": {"type": "integer"}, "dx": {"type": "integer"}, "dy": {"type": "integer"}}})),
    "krita_import_image": (t_import_image, types.Tool(
        name="krita_import_image",
        description="Land an image file (PNG/JPEG/WebP...) as a NEW paint layer at (x,y) on top of the stack — the "
                    "landing tool for comfy.image_generate output. Optional w/h scales it; layer+replace:true swaps an "
                    "existing layer (new layer above, old removed — undoable); layer alone writes into that paint layer.",
        inputSchema={"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string"}, "doc": DOC, "x": {"type": "integer"}, "y": {"type": "integer"},
            "w": {"type": "integer"}, "h": {"type": "integer"}, "name": {"type": "string"},
            "layer": {"type": "string"}, "replace": {"type": "boolean"}, "parent": {"type": "string"},
            "above": {"type": "string"}, "opacity": {"type": "number"}, "blending_mode": {"type": "string"}}})),
    "krita_selection": (t_selection, types.Tool(
        name="krita_selection",
        description="The global selection (what filters, fills and comfy masks act on). op=get, rect (x,y,w,h), ellipse "
                    "(bounding x,y,w,h), all, none, invert, grow/shrink/feather/border (radius), smooth, from_mask "
                    "(grayscale PNG path; white=selected; x,y offset), to_mask (write the selection as a PNG mask for "
                    "inpainting; no selection = all white). mode replace|add|subtract|intersect for rect/ellipse/from_mask.",
        inputSchema={"type": "object", "required": ["op"], "properties": dict(REGION, doc=DOC,
            op={"type": "string", "enum": ["get", "rect", "ellipse", "all", "none", "invert", "grow", "shrink", "feather", "border", "smooth", "from_mask", "to_mask"]},
            mode={"type": "string", "enum": ["replace", "add", "subtract", "intersect"]},
            feather={"type": "integer", "description": "rect/ellipse: soften the edge by this many px."},
            radius={"type": "integer"}, edge_lock={"type": "boolean"}, path={"type": "string"},
            whole_canvas={"type": "boolean", "description": "to_mask: canvas-sized mask (default true) vs selection bounds."})})),
    "krita_transform": (t_transform, types.Tool(
        name="krita_transform",
        description="Geometry, in pixels and degrees. Image ops: resize_canvas (x,y offset + w,h — no scaling), "
                    "scale_image (w,h,strategy), rotate_image (degrees), shear_image (x,y degrees), crop_image (x,y,w,h), "
                    "flatten. Layer ops (layer): scale_layer (w,h,origin [x,y]), rotate_layer (degrees), crop_layer, shear_layer.",
        inputSchema={"type": "object", "required": ["op"], "properties": dict(REGION, doc=DOC, layer=LAYER,
            op={"type": "string", "enum": ["resize_canvas", "scale_image", "rotate_image", "shear_image", "crop_image", "flatten", "scale_layer", "rotate_layer", "crop_layer", "shear_layer"]},
            degrees={"type": "number"}, strategy={"type": "string", "description": "Bicubic (default), Lanczos3, Bilinear, Box, Hermite, Bell, BSpline, Mitchell."},
            origin={"type": "array", "items": {"type": "number"}})})),
    "krita_filter": (t_filter, types.Tool(
        name="krita_filter",
        description="Krita's filters on a layer (or a region of it). op=list (ids like 'gaussian blur', 'unsharp', "
                    "'levels', 'hsvadjustment', 'desaturate', 'colorbalance', 'noise', 'emboss'), params (name -> the "
                    "configurable keys with current values), apply (name, layer, params, optional x,y,w,h). Respects the "
                    "selection.",
        inputSchema={"type": "object", "properties": dict(REGION, doc=DOC, layer=LAYER,
            op={"type": "string", "enum": ["list", "params", "apply"], "description": "Default apply."},
            name={"type": "string"}, params={"type": "object"})})),
    "krita_draw": (t_draw, types.Tool(
        name="krita_draw",
        description="Flat vector-style marks rendered with QPainter straight into a paint layer's pixels (anti-aliased; "
                    "bypasses the brush engine and undo — use krita_paint for real brush strokes). ops: fill {color}, clear, "
                    "rect {x,y,w,h,fill,stroke,width,radius}, ellipse {x,y,w,h | cx,cy,rx,ry,...}, line {x1,y1,x2,y2,color,width}, "
                    "polyline/polygon/path {points:[[x,y],...],...}, text {x,y,text,color,size,font,bold}, image {path,x,y,w,h,opacity}. "
                    "region limits the pixels touched (default whole canvas). Good for layouts, masks, control images, labels.",
        inputSchema={"type": "object", "required": ["ops"], "properties": {
            "doc": DOC, "layer": LAYER, "ops": {"type": "array", "items": {"type": "object"}},
            "region": {"type": "object", "properties": REGION}, "antialias": {"type": "boolean"}}})),
    "krita_paint": (t_paint, types.Tool(
        name="krita_paint",
        description="Real brush strokes through Krita's brush engine with the current (or given) preset — Krita >= 5.3 "
                    "only; undoable, pressure-aware. strokes: [{op:'stroke', points:[[x,y],[x,y,pressure],...], pressure}, "
                    "{op:'rect'|'ellipse', x,y,w,h, fill_style}, {op:'polygon', points}]. brush: {preset, size px, opacity 0-1, "
                    "flow 0-1, color, blending_mode, eraser}. stroke_style/fill_style: ForegroundColor (default), "
                    "BackgroundColor, None, Pattern (fill only). Fails with a capability error below 5.3: use krita_draw then.",
        inputSchema={"type": "object", "required": ["strokes"], "properties": {
            "doc": DOC, "layer": LAYER, "strokes": {"type": "array", "items": {"type": "object"}},
            "brush": {"type": "object", "properties": {"preset": {"type": "string"}, "size": {"type": "number"},
                "opacity": {"type": "number"}, "flow": {"type": "number"}, "color": COLOR,
                "blending_mode": {"type": "string"}, "eraser": {"type": "boolean"}}},
            "stroke_style": {"type": "string"}, "fill_style": {"type": "string"}}})),
    "krita_color": (t_color, types.Tool(
        name="krita_color",
        description="Brush and colour state of the active view. op=get (preset, size, opacity, flow, blend, fg/bg "
                    "colours), set (any of those; color = foreground), presets (filter substring -> preset names, e.g. "
                    "'ink', 'pencil', 'airbrush', 'wet').",
        inputSchema={"type": "object", "properties": {
            "op": {"type": "string", "enum": ["get", "set", "presets"]}, "doc": DOC,
            "preset": {"type": "string"}, "size": {"type": "number"}, "opacity": {"type": "number"},
            "flow": {"type": "number"}, "color": COLOR, "background": COLOR, "blending_mode": {"type": "string"},
            "eraser": {"type": "boolean"}, "filter": {"type": "string"}, "limit": {"type": "integer"}}})),
    "krita_action": (t_action, types.Tool(
        name="krita_action",
        description="Krita's own menu actions. op=undo, redo, list (filter substring -> ids, with an 'allowed' flag), "
                    "trigger (id from an allow-list of safe edit/layer/tool actions: e.g. flatten_image, merge_layer, "
                    "select_all, deselect, KritaShape/KisToolBrush). Actions that open dialogs are not on the list.",
        inputSchema={"type": "object", "properties": {
            "op": {"type": "string", "enum": ["list", "trigger", "undo", "redo"]}, "id": {"type": "string"},
            "filter": {"type": "string"}, "limit": {"type": "integer"}, "doc": DOC}})),
}

if ALLOW_EXEC:
    TOOLS["krita_run_python"] = (t_run_python, types.Tool(
        name="krita_run_python",
        description="Escape hatch: run Python inside Krita with the full libkis API (`Krita`, `krita`, `doc` = active "
                    "document; set `result` to return a value; stdout is captured). Enabled only when both "
                    "KRITA_ALLOW_EXEC=1 (server) and ~/.krita-mcp/allow_exec (plugin) are set.",
        inputSchema={"type": "object", "required": ["code"], "properties": {"code": {"type": "string"}}}))

server = Server("krita-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [tool for _, tool in TOOLS.values()]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent | types.ImageContent]:
    fn = TOOLS[name][0]
    try:
        out = await asyncio.to_thread(fn, arguments or {})
    except BusyError as e:
        out = f"ERROR (krita_busy): {e}\nKrita's GUI thread is blocked — a dialog is open or a long operation is running. Retry after it clears."
    except ConnectionError as e:
        out = f"ERROR (bridge unreachable): {e}\n{bridge.SETUP}\nCall krita_status to start Krita."
    except (BridgeError, KeyError, ValueError, FileNotFoundError) as e:
        out = f"ERROR: {e}"
    if isinstance(out, list):
        return out
    return [types.TextContent(type="text", text=str(out))]


def main() -> None:
    argv = sys.argv[1:]
    if argv:
        from . import install
        cmd = argv[0]
        if cmd == "install-plugin":
            print(install.install(force="--force" in argv))
        elif cmd == "uninstall-plugin":
            print(install.uninstall())
        elif cmd == "plugin-status":
            print(install.status())
        else:
            print(__doc__)
            sys.exit(2)
        return

    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()
