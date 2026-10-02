---
name: krita
description: Compose, mask, layer, retouch, annotate and export images in Krita (the open-source painting app running on this workstation) through the `krita` server in the dangler fleet — and SEE the canvas as a PNG between steps. Use whenever an image needs editing rather than generating - "put these two renders side by side", "mask out the background", "add a label / a border / a grid", "blur / sharpen / recolour that layer", "paint a rough sketch as a control image", "open this .kra and export a PNG", "resize / crop / rotate", "make a layered file". Generation itself stays with the `generate-image` skill (comfy); Krita is where its results land, get corrected and get exported.
---

# krita — the canvas

Krita runs as a normal desktop app with the `krita_mcp_bridge` plugin inside; the `krita`
fleet server drives it over a token-protected loopback port. Same tools whether Krita was
already open or `krita_status` started it.

## How to call it

1. `load_server {name: "krita"}` once, to see the schemas.
2. `call_tool {server: "krita", tool: "krita_status"}` — starts Krita if needed, lists open
   documents with their ids and says whether native painting is available.
3. Work: `krita_document {op:"new", width, height}` → `krita_import_image {path}` → `krita_layer`,
   `krita_selection`, `krita_filter`, `krita_draw` / `krita_paint` → `krita_preview` to look →
   `krita_export {path}` to finish (and `krita_document {op:"save", path:"….kra"}` to keep layers).

If `krita` is not in `list_servers`, run the install line in the server's `setup_hint` (Krita closed).

## Which tool

| Need | Tool | Writes to the document |
|---|---|---|
| is Krita up, what is open, ids, capabilities | `krita_status` | no (may start Krita) |
| look at the canvas / a layer / a region (PNG, ≤1024 px) | `krita_preview` | no |
| new / open / save (.kra keeps layers) / close / convert colour space | `krita_document` | yes |
| full-resolution file out (png, jpg, webp, tif, kra); a layer alone; a control image for comfy | `krita_export` | writes a file |
| the layer tree with ids | `krita_layers` | no |
| create / set / delete / move / reorder / duplicate / merge / activate a layer | `krita_layer` | yes |
| a PNG in as a new paint layer (comfy results land here); replace a layer | `krita_import_image` | yes |
| rect / ellipse / all / none / invert / grow / shrink / feather; mask PNG in or out | `krita_selection` | selection |
| resize canvas, scale, rotate, crop, shear, flatten; same per layer | `krita_transform` | yes |
| blur, sharpen, levels, HSV, desaturate, noise… (list, params, apply) | `krita_filter` | yes |
| flat shapes, lines, text, pasted images via QPainter (fast, no undo) | `krita_draw` | yes |
| real brush strokes with a preset, pressure (Krita ≥ 5.3) | `krita_paint` | yes |
| brush preset / size / opacity / colours; list presets | `krita_color` | view state |
| undo / redo / a safe menu action | `krita_action` | yes |

## The comfy round trip

1. `krita_export {path: "…/control.png"}` a sketch or a region → `comfy.image_generate {control_image, workflow: "image/z-image-turbo-control.json"}`.
2. `krita_import_image {path: <result>, x, y}` lands it as a layer; `krita_layer {op:"set", opacity, blending_mode}` to blend.
3. For inpainting: `krita_selection {op:"to_mask", path}` gives the mask; the region export + mask go to comfy; the result comes back with `krita_import_image {layer, replace: true}` or at the region offset.

## Rules paid for in use

- **Preview before you claim.** `krita_preview` is cheap; a step is not done until you have looked.
- **Ids, not names.** `krita_layers` gives UUIDs; names collide. Pass `doc` when several documents are open.
- **Pixel tools want RGBA/U8** (`krita_import_image`, `krita_draw`, layer previews, masks). `krita_document {op:"convert"}` fixes other spaces; 16-bit documents can be exported and previewed but not pixel-edited through the bridge.
- **`krita_draw` is not painting.** It bypasses the brush engine and undo — right for layouts, control images, masks, labels; wrong for anything that should look hand-made. `krita_paint` uses the real brush engine and is undoable, but needs Krita 5.3+ (the status call says).
- **Exports go under the export roots** (`KRITA_EXPORT_ROOTS` in the fleet config; `~/Pictures/krita-mcp` by default). Anything else is refused, not silently redirected.
- **Save before close**, and save `.kra` for anything with layers you may return to. Krita can crash on close after a long session; the bridge refuses to close unsaved work unless told `discard:true`.
- **`krita_busy`** means a dialog is open in Krita or a long filter is running: it is not a failure of the call, retry when it clears (the user can dismiss the dialog).
- **Never run `krita.exe --export` from a shell while Krita is open** — it hangs. Export through the bridge.
- `KRITA_READ_ONLY=1` in the fleet env refuses every tool that changes a document or writes a file.
