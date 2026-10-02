"""The fixed command catalog: every libkis call the bridge is willing to make, by name.

All functions run on Krita's GUI thread (see mainthread.py), take a plain dict and return
plain JSON-able data — never a Document/Node wrapper. Documents are addressed by the UUID
of their root node, nodes by their own UUID (or, as a convenience, an exact name), so no
wrapper is ever held across requests.

Unit conventions exposed to callers (normalised here, because libkis mixes them):
  opacity 0.0-1.0 (libkis Node.opacity is 0-255) · angles in degrees (libkis rotate* take
  radians, shear* already degrees) · colours '#rrggbb' / '#rrggbbaa' / [r,g,b,a] · all
  coordinates in document pixels, origin top-left.

Pixel-level commands (preview by layer, draw, import_image, selection masks) need an
RGBA/U8 document — the layout Krita's raw buffers share with QImage.Format_ARGB32. Other
colour spaces get a clear error and `document.convert` to fix it.
"""
import base64
import contextlib
import io
import math
import os
import sys
import time

from krita import InfoObject, Krita, ManagedColor, Selection

from .qtcompat import (ARGB32, GRAY8, QT_MAJOR, QBrush, QColor, QFont, QImage, QPainter, QPainterPath,
                       QPen, QPointF, QPolygonF, QRect, QRectF, Qt, QUuid, argb32_bytes, gray8_bytes,
                       image_from_bgra, image_from_gray, png_bytes, qcolor)

NODE_TYPES = {
    "paint": "paintlayer", "paintlayer": "paintlayer",
    "group": "grouplayer", "grouplayer": "grouplayer",
    "vector": "vectorlayer", "vectorlayer": "vectorlayer",
    "transparency_mask": "transparencymask", "transparencymask": "transparencymask",
    "selection_mask": "selectionmask", "selectionmask": "selectionmask",
    "transform_mask": "transformmask", "transformmask": "transformmask",
}
SCALE_STRATEGIES = ("Hermite", "Bicubic", "Box", "Bilinear", "Bell", "BSpline", "Lanczos3", "Mitchell")

ACTION_ALLOWLIST = {
    # edit / selection
    "edit_undo", "edit_redo", "select_all", "deselect", "invert_selection", "clear",
    "fill_selection_foreground_color", "fill_selection_background_color",
    "cut_selection_to_new_layer", "copy_selection_to_new_layer",
    # layers / image
    "add_new_paint_layer", "remove_layer", "duplicatelayer", "move_layer_up", "move_layer_down",
    "merge_layer", "flatten_layer", "flatten_image", "convert_to_paint_layer",
    "mirrorNodeX", "mirrorNodeY", "mirrorImageHorizontal", "mirrorImageVertical",
    "toggle_layer_visibility", "toggle_layer_lock", "toggle_layer_alpha_lock",
    # tool switches (no side effect on pixels by themselves)
    "KritaShape/KisToolBrush", "KritaFill/KisToolFill", "KritaSelected/KisToolColorSampler",
    "KisToolSelectRectangular", "KisToolSelectElliptical", "KisToolSelectContiguous", "KisToolSelectPolygonal",
    "KisToolTransform", "KritaTransform/KisToolMove", "KisToolCrop",
    "KritaShape/KisToolLine", "KritaShape/KisToolRectangle", "KritaShape/KisToolEllipse",
    "KritaShape/KisToolMultiBrush", "KritaShape/KisToolDyna", "KritaShape/KisToolSmartPatch",
}


class CommandError(Exception):
    pass


# ----------------------------------------------------------------------------- helpers

def _k():
    return Krita.instance()


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, QColor):
        return v.name()
    if isinstance(v, (QRect, QRectF)):
        return {"x": v.x(), "y": v.y(), "w": v.width(), "h": v.height()}
    if isinstance(v, QUuid):
        return v.toString()
    if hasattr(v, "toString"):
        return v.toString()
    return str(v)


def _rect(r):
    return {"x": r.x(), "y": r.y(), "w": r.width(), "h": r.height()}


def _doc_id(doc):
    return doc.rootNode().uniqueId().toString()


def _doc(args, key="doc"):
    ref = args.get(key)
    if not ref or ref == "active":
        doc = _k().activeDocument()
        if doc is None:
            docs = _k().documents()
            if not docs:
                raise CommandError("no document is open — use document.new or document.open first")
            doc = docs[0]
        return doc
    for d in _k().documents():
        if _doc_id(d) == ref or d.name() == ref or d.fileName() == ref:
            return d
    raise CommandError("no open document matches %r (use document.list)" % ref)


def _walk(node):
    yield node
    for c in node.childNodes():
        for n in _walk(c):
            yield n


def _node(doc, ref, key="layer"):
    if ref is None or ref == "":
        n = doc.activeNode()
        if n is None:
            raise CommandError("no active layer; pass '%s'" % key)
        return n
    ref = str(ref)
    if ref.startswith("{"):
        n = doc.nodeByUniqueID(QUuid(ref))
        if n is not None:
            return n
    for n in _walk(doc.rootNode()):
        if n.uniqueId().toString() == ref or n.name() == ref:
            return n
    raise CommandError("no layer matches %r (use layers to list ids)" % ref)


def _node_info(node, deep=False):
    b = node.bounds()
    info = {
        "id": node.uniqueId().toString(), "name": node.name(), "type": node.type(),
        "visible": node.visible(), "opacity": round(node.opacity() / 255.0, 3),
        "blending_mode": node.blendingMode(), "locked": node.locked(), "alpha_locked": node.alphaLocked(),
        "inherit_alpha": node.inheritAlpha(), "bounds": _rect(b), "index": node.index(),
        "color_model": node.colorModel(), "color_depth": node.colorDepth(),
    }
    if hasattr(node, "paintAbility"):
        try:
            info["paint_ability"] = node.paintAbility()
        except Exception:  # noqa: BLE001
            pass
    if deep:
        info["children"] = [_node_info(c, True) for c in node.childNodes()]
    else:
        info["child_count"] = len(node.childNodes())
    return info


def _doc_info(doc):
    sel = doc.selection()
    active = doc.activeNode()
    return {
        "id": _doc_id(doc), "name": doc.name(), "file": doc.fileName() or None,
        "width": doc.width(), "height": doc.height(), "dpi": doc.resolution(),
        "color_model": doc.colorModel(), "color_depth": doc.colorDepth(), "color_profile": doc.colorProfile(),
        "modified": doc.modified(), "active_layer": active.uniqueId().toString() if active else None,
        "layer_count": sum(1 for _ in _walk(doc.rootNode())) - 1,
        "selection": {"x": sel.x(), "y": sel.y(), "w": sel.width(), "h": sel.height()} if sel is not None else None,
        "rgba8": doc.colorModel() == "RGBA" and doc.colorDepth() == "U8",
    }


def _require_rgba8(doc, what):
    if not (doc.colorModel() == "RGBA" and doc.colorDepth() == "U8"):
        raise CommandError("%s needs an RGBA/U8 document; this one is %s/%s — run document.convert "
                           "{model:'RGBA', depth:'U8'} first" % (what, doc.colorModel(), doc.colorDepth()))


def _window():
    w = _k().activeWindow()
    if w is None:
        wins = _k().windows()
        if not wins:
            raise CommandError("Krita has no main window yet")
        w = wins[0]
    return w


def _view_for(doc, show=True):
    win = _window()
    want = doc.rootNode().uniqueId()
    for v in win.views():
        d = v.document()
        if d is not None and d.rootNode().uniqueId() == want:
            if show:
                win.showView(v)
            return v
    v = win.addView(doc)
    if show and v is not None:
        win.showView(v)
    return v


def _settle(doc):
    doc.waitForDone()
    doc.refreshProjection()


@contextlib.contextmanager
def _batch(doc):
    k = _k()
    kb, db = k.batchmode(), doc.batchmode()
    k.setBatchmode(True)
    doc.setBatchmode(True)
    try:
        yield
    finally:
        doc.setBatchmode(db)
        k.setBatchmode(kb)


def _region(doc, args):
    """(x, y, w, h) from args, default the whole canvas; clamped to it."""
    W, H = doc.width(), doc.height()
    x = int(args.get("x", 0) or 0)
    y = int(args.get("y", 0) or 0)
    w = int(args.get("w") or args.get("width") or (W - x))
    h = int(args.get("h") or args.get("height") or (H - y))
    x, y = max(0, x), max(0, y)
    w, h = max(1, min(w, W - x)), max(1, min(h, H - y))
    return x, y, w, h


def _add_on_top(parent, node, above=None):
    if above is None:
        kids = parent.childNodes()
        above = kids[-1] if kids else None
    if not parent.addChildNode(node, above):
        raise CommandError("Krita refused to add layer %r under %r" % (node.name(), parent.name()))


def _info_object(params):
    cfg = InfoObject()
    for k, v in (params or {}).items():
        if isinstance(v, str) and v.startswith("#") and len(v) in (7, 9):
            v = qcolor(v)
        cfg.setProperty(str(k), v)
    return cfg


def _points(pts):
    out = []
    for p in pts:
        if isinstance(p, dict):
            out.append((float(p["x"]), float(p["y"]), float(p.get("p", p.get("pressure", 1.0)))))
        else:
            out.append((float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 1.0))
    return out


# ----------------------------------------------------------------------------- status

def capabilities(flags):
    from krita import Node
    return {
        "paint": hasattr(Node, "paintLine"),
        "find_child_nodes": hasattr(Node, "findChildNodes"),
        "run_python": bool(flags.get("allow_exec")),
        "any_action": bool(flags.get("any_action")),
        "qt_major": QT_MAJOR,
    }


def build(flags, log):
    """Return the name -> callable catalog. `flags` gates the escape hatches."""

    def status(a):
        k = _k()
        active = k.activeDocument()
        return {
            "krita_version": k.version(), "python": "%d.%d.%d" % sys.version_info[:3], "qt_major": QT_MAJOR,
            "capabilities": capabilities(flags), "batchmode": k.batchmode(),
            "documents": [_doc_info(d) for d in k.documents()],
            "active_document": _doc_id(active) if active else None,
            "time": time.time(),
        }

    # ---- documents ----------------------------------------------------------------
    def document_new(a):
        w, h = int(a.get("width", 1024)), int(a.get("height", 1024))
        name = a.get("name") or "Untitled"
        model, depth = a.get("color_model", "RGBA"), a.get("color_depth", "U8")
        dpi = float(a.get("dpi", 300))
        doc = _k().createDocument(w, h, name, model, depth, a.get("color_profile", "") or "", dpi)
        if doc is None:
            raise CommandError("createDocument returned nothing (bad colour model/depth?)")
        _view_for(doc)
        bg = a.get("background")
        if bg is not None and model == "RGBA" and depth == "U8":
            kids = doc.rootNode().childNodes()
            if kids:
                img = QImage(w, h, ARGB32)
                img.fill(QColor(0, 0, 0, 0) if bg in ("transparent", "none") else qcolor(bg))
                kids[0].setPixelData(argb32_bytes(img), 0, 0, w, h)
        _settle(doc)
        return _doc_info(doc)

    def document_open(a):
        path = os.path.abspath(os.path.expanduser(a["path"]))
        if not os.path.isfile(path):
            raise CommandError("file not found: %s" % path)
        k = _k()
        with_batch = k.batchmode()
        k.setBatchmode(True)
        try:
            doc = k.openDocument(path)
        finally:
            k.setBatchmode(with_batch)
        if doc is None:
            raise CommandError("Krita could not open %s" % path)
        _view_for(doc)
        _settle(doc)
        return _doc_info(doc)

    def document_save(a):
        doc = _doc(a)
        path = a.get("path")
        with _batch(doc):
            if path:
                path = os.path.abspath(os.path.expanduser(path))
                ok = doc.saveAs(path)
            else:
                if not doc.fileName():
                    raise CommandError("document has no file name yet; pass 'path' (a .kra keeps layers)")
                ok = doc.save()
        if not ok:
            raise CommandError("save failed (unsupported extension or unwritable path)")
        return _doc_info(doc)

    def document_close(a):
        doc = _doc(a)
        info = _doc_info(doc)
        if a.get("save") and doc.fileName():
            with _batch(doc):
                doc.save()
        elif doc.modified() and not a.get("discard"):
            raise CommandError("document has unsaved changes; pass save:true (needs a file name) or discard:true")
        with _batch(doc):
            ok = doc.close()
        if not ok:
            raise CommandError("Krita refused to close the document")
        info["closed"] = True
        return info

    def document_info(a):
        return _doc_info(_doc(a))

    def document_list(a):
        k = _k()
        act = k.activeDocument()
        return {"documents": [_doc_info(d) for d in k.documents()], "active_document": _doc_id(act) if act else None}

    def document_activate(a):
        doc = _doc(a)
        _view_for(doc)
        return _doc_info(doc)

    def document_convert(a):
        doc = _doc(a)
        doc.setColorSpace(a.get("model", "RGBA"), a.get("depth", "U8"), a.get("profile", "") or "")
        _settle(doc)
        return _doc_info(doc)

    # ---- preview / export ---------------------------------------------------------
    def preview(a):
        doc = _doc(a)
        x, y, w, h = _region(doc, a)
        _settle(doc)
        if a.get("layer"):
            _require_rgba8(doc, "layer preview")
            node = _node(doc, a["layer"])
            img = image_from_bgra(node.projectionPixelData(x, y, w, h), w, h)
        else:
            img = doc.projection(x, y, w, h)
            if img.isNull():
                raise CommandError("projection returned no image")
            if img.format() != ARGB32:
                img = img.convertToFormat(ARGB32)
        if a.get("checkerboard", True) and not a.get("layer"):
            pass                                          # doc projection is already flattened over transparency
        maxs = int(a.get("max_size", 1024) or 1024)
        scaled_from = None
        if max(img.width(), img.height()) > maxs:
            scaled_from = [img.width(), img.height()]
            img = img.scaled(maxs, maxs, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        fmt = "JPEG" if a.get("format", "png").lower() in ("jpg", "jpeg") else "PNG"
        data = png_bytes(img, fmt)
        return {"format": fmt.lower(), "base64": base64.b64encode(data).decode("ascii"),
                "width": img.width(), "height": img.height(), "region": {"x": x, "y": y, "w": w, "h": h},
                "scaled_from": scaled_from, "document": _doc_id(doc)}

    def export(a):
        doc = _doc(a)
        path = os.path.abspath(os.path.expanduser(a["path"]))
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d)
        cfg = _info_object(a.get("options") or {})
        _settle(doc)
        with _batch(doc):
            if a.get("layer"):
                node = _node(doc, a["layer"])
                r = a.get("region") or {}
                rect = QRect(int(r.get("x", 0)), int(r.get("y", 0)), int(r.get("w", doc.width())), int(r.get("h", doc.height())))
                ok = node.save(path, doc.xRes() / 72.0, doc.yRes() / 72.0, cfg, rect)
            else:
                ok = doc.exportImage(path, cfg)
                if not (ok and os.path.isfile(path)):
                    # field-reported fallback when exportImage insists on a dialog
                    ok = doc.rootNode().save(path, doc.xRes() / 72.0, doc.yRes() / 72.0, cfg,
                                             QRect(0, 0, doc.width(), doc.height()))
        if not (ok and os.path.isfile(path)):
            raise CommandError("export failed for %s (extension unsupported or path unwritable)" % path)
        return {"path": path, "bytes": os.path.getsize(path), "width": doc.width(), "height": doc.height()}

    # ---- layers -------------------------------------------------------------------
    def layers(a):
        doc = _doc(a)
        act = doc.activeNode()
        return {"document": _doc_id(doc), "active_layer": act.uniqueId().toString() if act else None,
                "layers": [_node_info(c, True) for c in doc.rootNode().childNodes()]}

    def layer_info(a):
        doc = _doc(a)
        return _node_info(_node(doc, a.get("layer")), True)

    def layer_create(a):
        doc = _doc(a)
        t = str(a.get("type", "paint")).lower()
        name = a.get("name") or t
        parent = _node(doc, a["parent"]) if a.get("parent") else doc.rootNode()
        if t in NODE_TYPES:
            node = doc.createNode(name, NODE_TYPES[t])
        elif t in ("filter", "filterlayer", "filter_mask", "filtermask"):
            f = _k().filter(a["filter"])
            if f is None:
                raise CommandError("unknown filter %r" % a.get("filter"))
            if a.get("params"):
                f.setConfiguration(_info_object(a["params"]))
            sel = Selection()
            sel.select(0, 0, doc.width(), doc.height(), 255)
            node = doc.createFilterLayer(name, f, sel) if t.startswith("filterl") or t == "filter" else doc.createFilterMask(name, f, sel)
        elif t in ("clone", "clonelayer"):
            node = doc.createCloneLayer(name, _node(doc, a["source"], "source"))
        elif t in ("file", "filelayer"):
            path = os.path.abspath(os.path.expanduser(a["path"]))
            node = doc.createFileLayer(name, path, a.get("scaling", "None"), a.get("scaling_filter", "Bicubic"))
        else:
            raise CommandError("unknown layer type %r; one of %s, filter, filter_mask, clone, file" % (t, sorted(set(NODE_TYPES))))
        if node is None:
            raise CommandError("Krita did not create the node")
        above = _node(doc, a["above"]) if a.get("above") else None
        if a.get("position") == "bottom" and above is None:
            if not parent.addChildNode(node, None):
                raise CommandError("could not add layer")
        else:
            _add_on_top(parent, node, above)
        if a.get("fill") and NODE_TYPES.get(t) == "paintlayer":
            _require_rgba8(doc, "fill")
            img = QImage(doc.width(), doc.height(), ARGB32)
            img.fill(qcolor(a["fill"]))
            node.setPixelData(argb32_bytes(img), 0, 0, doc.width(), doc.height())
        for k in ("opacity", "blending_mode", "visible", "locked", "alpha_locked", "inherit_alpha"):
            if k in a:
                _apply_prop(node, k, a[k])
        if a.get("activate", True):
            doc.setActiveNode(node)
        _settle(doc)
        return _node_info(node, True)

    def _apply_prop(node, k, v):
        if k == "opacity":
            node.setOpacity(int(round(max(0.0, min(1.0, float(v))) * 255)))
        elif k == "blending_mode":
            node.setBlendingMode(str(v))
        elif k == "visible":
            node.setVisible(bool(v))
        elif k == "locked":
            node.setLocked(bool(v))
        elif k == "alpha_locked":
            node.setAlphaLocked(bool(v))
        elif k == "inherit_alpha":
            node.setInheritAlpha(bool(v))
        elif k == "name":
            node.setName(str(v))
        elif k == "color_label":
            node.setColorLabel(int(v))
        elif k == "collapsed":
            node.setCollapsed(bool(v))

    def layer_set(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        for k in ("opacity", "blending_mode", "visible", "locked", "alpha_locked", "inherit_alpha", "name", "color_label", "collapsed"):
            if k in a and a[k] is not None:
                _apply_prop(node, k, a[k])
        _settle(doc)
        return _node_info(node)

    def layer_delete(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        info = _node_info(node)
        if not node.remove():
            raise CommandError("Krita refused to remove %r" % info["name"])
        _settle(doc)
        info["deleted"] = True
        return info

    def layer_move(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        p = node.position()
        if "dx" in a or "dy" in a:
            x, y = p.x() + int(a.get("dx", 0)), p.y() + int(a.get("dy", 0))
        else:
            x, y = int(a.get("x", p.x())), int(a.get("y", p.y()))
        node.move(x, y)
        _settle(doc)
        return _node_info(node)

    def layer_reorder(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        parent = _node(doc, a["parent"]) if a.get("parent") else (node.parentNode() or doc.rootNode())
        above = _node(doc, a["above"]) if a.get("above") else None
        old = node.parentNode()
        if old is not None and not old.removeChildNode(node):
            raise CommandError("could not detach layer")
        if a.get("position") == "bottom" and above is None:
            parent.addChildNode(node, None)
        else:
            _add_on_top(parent, node, above)
        _settle(doc)
        return _node_info(node)

    def layer_duplicate(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        dup = node.duplicate()
        if a.get("name"):
            dup.setName(a["name"])
        parent = node.parentNode() or doc.rootNode()
        parent.addChildNode(dup, node)
        _settle(doc)
        return _node_info(dup)

    def layer_merge_down(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        merged = node.mergeDown()
        _settle(doc)
        return _node_info(merged) if merged is not None else {"merged": True}

    def layer_activate(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        doc.setActiveNode(node)
        return _node_info(node)

    # ---- import ---------------------------------------------------------------------
    def import_image(a):
        doc = _doc(a)
        _require_rgba8(doc, "import_image")
        path = os.path.abspath(os.path.expanduser(a["path"]))
        img = QImage(path)
        if img.isNull():
            raise CommandError("cannot read image: %s" % path)
        img = img.convertToFormat(ARGB32)
        if a.get("w") or a.get("h"):
            tw, th = int(a.get("w") or img.width()), int(a.get("h") or img.height())
            img = img.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio if a.get("w") and a.get("h") else Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        x, y = int(a.get("x", 0)), int(a.get("y", 0))
        name = a.get("name") or os.path.splitext(os.path.basename(path))[0]
        target = None
        if a.get("layer") and not a.get("replace"):
            target = _node(doc, a["layer"])
            if target.type() != "paintlayer":
                raise CommandError("target layer must be a paint layer")
        if target is None:
            target = doc.createNode(name, "paintlayer")
            old = _node(doc, a["layer"]) if a.get("layer") and a.get("replace") else None
            parent = (old.parentNode() if old is not None else None) or (_node(doc, a["parent"]) if a.get("parent") else doc.rootNode())
            _add_on_top(parent, target, old if old is not None else (_node(doc, a["above"]) if a.get("above") else None))
            if old is not None:
                old.remove()
        target.setPixelData(argb32_bytes(img), x, y, img.width(), img.height())
        for k in ("opacity", "blending_mode"):
            if k in a and a[k] is not None:
                _apply_prop(target, k, a[k])
        doc.setActiveNode(target)
        _settle(doc)
        info = _node_info(target)
        info["placed"] = {"x": x, "y": y, "w": img.width(), "h": img.height()}
        return info

    # ---- selection ----------------------------------------------------------------
    def _combine(doc, new, mode):
        mode = (mode or "replace").lower()
        cur = doc.selection()
        if mode == "replace" or cur is None:
            doc.setSelection(new)
        elif mode in ("add", "subtract", "intersect"):
            getattr(cur, mode)(new)
            doc.setSelection(cur)
        else:
            raise CommandError("mode must be replace|add|subtract|intersect")
        return selection_get({"doc": _doc_id(doc)})

    def selection_get(a):
        doc = _doc(a)
        sel = doc.selection()
        if sel is None:
            return {"selection": None}
        return {"selection": {"x": sel.x(), "y": sel.y(), "w": sel.width(), "h": sel.height()}}

    def selection_rect(a):
        doc = _doc(a)
        x, y, w, h = _region(doc, a)
        sel = Selection()
        sel.select(x, y, w, h, int(a.get("value", 255)))
        if a.get("feather"):
            sel.feather(int(a["feather"]))
        return _combine(doc, sel, a.get("mode"))

    def selection_ellipse(a):
        doc = _doc(a)
        x, y, w, h = _region(doc, a)
        img = QImage(w, h, GRAY8)
        img.fill(QColor(0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255)))
        p.drawEllipse(QRectF(0, 0, w, h))
        p.end()
        sel = Selection()
        sel.setPixelData(gray8_bytes(img), x, y, w, h)
        if a.get("feather"):
            sel.feather(int(a["feather"]))
        return _combine(doc, sel, a.get("mode"))

    def selection_all(a):
        doc = _doc(a)
        sel = Selection()
        sel.select(0, 0, doc.width(), doc.height(), 255)
        doc.setSelection(sel)
        return selection_get(a)

    def selection_none(a):
        doc = _doc(a)
        _view_for(doc)
        act = _k().action("deselect")
        if act is None:
            raise CommandError("deselect action not found")
        act.trigger()
        _settle(doc)
        return selection_get(a)

    def selection_modify(a):
        doc = _doc(a)
        sel = doc.selection()
        if sel is None:
            raise CommandError("no selection to modify")
        op = a["op"]
        r = int(a.get("radius", 1))
        if op == "invert":
            sel.invert()
        elif op == "grow":
            sel.grow(r, r)
        elif op == "shrink":
            sel.shrink(r, r, bool(a.get("edge_lock", False)))
        elif op == "feather":
            sel.feather(r)
        elif op == "border":
            sel.border(r, r)
        elif op == "smooth":
            sel.smooth()
        else:
            raise CommandError("unknown selection op %r" % op)
        doc.setSelection(sel)
        return selection_get(a)

    def selection_from_mask(a):
        doc = _doc(a)
        path = os.path.abspath(os.path.expanduser(a["path"]))
        img = QImage(path)
        if img.isNull():
            raise CommandError("cannot read mask: %s" % path)
        img = img.convertToFormat(GRAY8)
        x, y = int(a.get("x", 0)), int(a.get("y", 0))
        sel = Selection()
        sel.setPixelData(gray8_bytes(img), x, y, img.width(), img.height())
        return _combine(doc, sel, a.get("mode"))

    def selection_to_mask(a):
        doc = _doc(a)
        sel = doc.selection()
        path = os.path.abspath(os.path.expanduser(a["path"]))
        whole = bool(a.get("whole_canvas", True))
        if whole:
            x, y, w, h = 0, 0, doc.width(), doc.height()
        elif sel is None:
            raise CommandError("no selection")
        else:
            x, y, w, h = sel.x(), sel.y(), sel.width(), sel.height()
        if sel is None:
            img = QImage(w, h, GRAY8)
            img.fill(QColor(255, 255, 255))     # no selection == everything selected
        else:
            img = image_from_gray(sel.pixelData(x, y, w, h), w, h)
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d)
        if not img.save(path):
            raise CommandError("could not write %s" % path)
        return {"path": path, "x": x, "y": y, "w": w, "h": h, "empty": sel is None}

    # ---- transforms ---------------------------------------------------------------
    def transform(a):
        doc = _doc(a)
        op = a["op"]
        strategy = a.get("strategy", "Bicubic")
        if strategy not in SCALE_STRATEGIES:
            raise CommandError("strategy must be one of %s" % (SCALE_STRATEGIES,))
        if op == "resize_canvas":
            doc.resizeImage(int(a.get("x", 0)), int(a.get("y", 0)), int(a["w"]), int(a["h"]))
        elif op == "scale_image":
            doc.scaleImage(int(a["w"]), int(a["h"]), doc.xRes(), doc.yRes(), strategy)
        elif op == "rotate_image":
            doc.rotateImage(math.radians(float(a["degrees"])))
        elif op == "shear_image":
            doc.shearImage(float(a.get("x", 0)), float(a.get("y", 0)))
        elif op == "crop_image":
            x, y, w, h = _region(doc, a)
            doc.crop(x, y, w, h)
        elif op == "flatten":
            doc.flatten()
        elif op in ("scale_layer", "rotate_layer", "crop_layer", "shear_layer"):
            node = _node(doc, a.get("layer"))
            if op == "scale_layer":
                b = node.bounds()
                o = a.get("origin") or [b.x(), b.y()]
                node.scaleNode(QPointF(float(o[0]), float(o[1])), int(a["w"]), int(a["h"]), strategy)
            elif op == "rotate_layer":
                node.rotateNode(math.radians(float(a["degrees"])))
            elif op == "crop_layer":
                x, y, w, h = _region(doc, a)
                node.cropNode(x, y, w, h)
            else:
                node.shearNode(float(a.get("x", 0)), float(a.get("y", 0)))
        else:
            raise CommandError("unknown transform op %r" % op)
        _settle(doc)
        return _doc_info(doc)

    # ---- filters ------------------------------------------------------------------
    def filter_list(a):
        return {"filters": list(_k().filters())}

    def filter_params(a):
        f = _k().filter(a["name"])
        if f is None:
            raise CommandError("unknown filter %r" % a["name"])
        props = f.configuration().properties()
        return {"name": a["name"], "params": {str(k): _jsonable(v) for k, v in props.items()}}

    def filter_apply(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        f = _k().filter(a["name"])
        if f is None:
            raise CommandError("unknown filter %r (see filter.list)" % a["name"])
        if a.get("params"):
            cfg = f.configuration()
            for k, v in a["params"].items():
                if isinstance(v, str) and v.startswith("#") and len(v) in (7, 9):
                    v = qcolor(v)
                cfg.setProperty(str(k), v)
            f.setConfiguration(cfg)
        if node.locked():
            raise CommandError("layer %r is locked" % node.name())
        if node.type() not in ("paintlayer", "filterlayer", "filllayer", "transparencymask", "filtermask", "selectionmask", "colorizemask"):
            raise CommandError("filters apply to paint layers and masks, not %s" % node.type())
        x, y, w, h = _region(doc, a)
        # Filter.apply's bool is unreliable on 5.3 (the effect lands, the return says false), so the
        # preconditions are checked above and the return value is only reported.
        ok = f.apply(node, x, y, w, h)
        _settle(doc)
        info = _node_info(node)
        info["filter"] = a["name"]
        info["region"] = {"x": x, "y": y, "w": w, "h": h}
        info["apply_returned"] = bool(ok)
        return info

    # ---- draw (QPainter -> setPixelData; flat, anti-aliased, bypasses undo/brush engine) ----
    def draw(a):
        doc = _doc(a)
        _require_rgba8(doc, "draw")
        node = _node(doc, a.get("layer"))
        if node.type() != "paintlayer":
            raise CommandError("draw needs a paint layer (got %s)" % node.type())
        rx, ry, rw, rh = _region(doc, a.get("region") or {"x": 0, "y": 0, "w": doc.width(), "h": doc.height()})
        img = image_from_bgra(node.pixelData(rx, ry, rw, rh), rw, rh)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, bool(a.get("antialias", True)))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.translate(-rx, -ry)
        done = []
        try:
            for op in a.get("ops") or []:
                kind = op.get("op")
                if kind in ("fill", "clear"):
                    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
                    p.fillRect(QRectF(rx, ry, rw, rh), QColor(0, 0, 0, 0) if kind == "clear" else qcolor(op.get("color")))
                    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                    done.append(kind)
                    continue
                pen = QPen(qcolor(op.get("stroke") or op.get("color"), "#000000"), float(op.get("width", 2)),
                           Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                if not (op.get("stroke") or op.get("color")) and kind in ("rect", "ellipse", "polygon"):
                    pen = QPen(Qt.PenStyle.NoPen)
                p.setPen(pen)
                p.setBrush(QBrush(qcolor(op["fill"])) if op.get("fill") else QBrush(Qt.BrushStyle.NoBrush))
                if kind == "rect":
                    r = QRectF(float(op["x"]), float(op["y"]), float(op["w"]), float(op["h"]))
                    if op.get("radius"):
                        p.drawRoundedRect(r, float(op["radius"]), float(op["radius"]))
                    else:
                        p.drawRect(r)
                elif kind == "ellipse":
                    if "cx" in op:
                        p.drawEllipse(QPointF(float(op["cx"]), float(op["cy"])), float(op["rx"]), float(op.get("ry", op["rx"])))
                    else:
                        p.drawEllipse(QRectF(float(op["x"]), float(op["y"]), float(op["w"]), float(op["h"])))
                elif kind == "line":
                    p.drawLine(QPointF(float(op["x1"]), float(op["y1"])), QPointF(float(op["x2"]), float(op["y2"])))
                elif kind in ("polyline", "polygon"):
                    poly = QPolygonF([QPointF(x, y) for x, y, _ in _points(op["points"])])
                    if kind == "polygon":
                        p.drawPolygon(poly)
                    else:
                        p.drawPolyline(poly)
                elif kind == "path":
                    path = QPainterPath()
                    pts = _points(op["points"])
                    path.moveTo(pts[0][0], pts[0][1])
                    for x, y, _ in pts[1:]:
                        path.lineTo(x, y)
                    if op.get("close"):
                        path.closeSubpath()
                    p.drawPath(path)
                elif kind == "text":
                    f = QFont(op.get("font", "Sans Serif"), int(op.get("size", 24)))
                    f.setBold(bool(op.get("bold")))
                    f.setItalic(bool(op.get("italic")))
                    p.setFont(f)
                    p.setPen(QPen(qcolor(op.get("color"), "#000000")))
                    p.drawText(QPointF(float(op["x"]), float(op["y"])), str(op["text"]))
                elif kind == "image":
                    src = QImage(os.path.abspath(os.path.expanduser(op["path"])))
                    if src.isNull():
                        raise CommandError("cannot read %s" % op["path"])
                    p.setOpacity(float(op.get("opacity", 1.0)))
                    if op.get("w") or op.get("h"):
                        p.drawImage(QRectF(float(op["x"]), float(op["y"]), float(op.get("w") or src.width()), float(op.get("h") or src.height())), src)
                    else:
                        p.drawImage(QPointF(float(op["x"]), float(op["y"])), src)
                    p.setOpacity(1.0)
                else:
                    raise CommandError("unknown draw op %r" % kind)
                done.append(kind)
        finally:
            p.end()
        node.setPixelData(argb32_bytes(img), rx, ry, rw, rh)
        _settle(doc)
        info = _node_info(node)
        info["ops"] = done
        info["region"] = {"x": rx, "y": ry, "w": rw, "h": rh}
        return info

    # ---- paint (native brush engine, Krita >= 5.3) --------------------------------
    def _brush_apply(view, b):
        if b.get("preset"):
            presets = _k().resources("preset")
            res = presets.get(b["preset"])
            if res is None:
                hits = [n for n in presets if b["preset"].lower() in n.lower()]
                if len(hits) == 1:
                    res = presets[hits[0]]
                else:
                    raise CommandError("preset %r not found (%d partial matches: %s)" % (b["preset"], len(hits), hits[:8]))
            view.setCurrentBrushPreset(res)
        if b.get("size") is not None:
            view.setBrushSize(float(b["size"]))
        if b.get("opacity") is not None:
            view.setPaintingOpacity(float(b["opacity"]))
        if b.get("flow") is not None:
            view.setPaintingFlow(float(b["flow"]))
        if b.get("blending_mode"):
            view.setCurrentBlendingMode(str(b["blending_mode"]))
        if b.get("eraser") is not None:
            view.setEraserMode(bool(b["eraser"]))
        if b.get("color"):
            view.setForeGroundColor(ManagedColor.fromQColor(qcolor(b["color"]), view.canvas()))
        if b.get("background"):
            view.setBackGroundColor(ManagedColor.fromQColor(qcolor(b["background"]), view.canvas()))

    def brush_get(a):
        doc = _doc(a)
        view = _view_for(doc, show=False)
        preset = view.currentBrushPreset()
        canvas = view.canvas()
        return {"preset": preset.name() if preset else None, "size": view.brushSize(),
                "opacity": view.paintingOpacity(), "flow": view.paintingFlow(),
                "blending_mode": view.currentBlendingMode(), "eraser": view.eraserMode(),
                "color": view.foregroundColor().colorForCanvas(canvas).name(),
                "background": view.backgroundColor().colorForCanvas(canvas).name()}

    def brush_set(a):
        doc = _doc(a)
        view = _view_for(doc)
        _brush_apply(view, a)
        return brush_get(a)

    def presets_list(a):
        q = (a.get("filter") or "").lower()
        names = sorted(n for n in _k().resources("preset") if q in n.lower())
        return {"count": len(names), "presets": names[: int(a.get("limit", 200))]}

    def _float_or_int(fn, *args):
        """The 5.3 release binds paint*() with QPoint/QRect while master binds QPointF/QRectF;
        try the float form first and fall back to the integer form on a signature TypeError."""
        try:
            return fn(*args)
        except TypeError:
            conv = []
            for v in args:
                if isinstance(v, QPointF):
                    conv.append(v.toPoint())
                elif isinstance(v, QRectF):
                    conv.append(v.toRect())
                elif isinstance(v, list) and v and isinstance(v[0], QPointF):
                    conv.append([p.toPoint() for p in v])
                else:
                    conv.append(v)
            return fn(*conv)

    def paint(a):
        doc = _doc(a)
        node = _node(doc, a.get("layer"))
        if not hasattr(node, "paintLine"):
            raise CommandError("native brush strokes need Krita >= 5.3 (this is %s); use draw instead" % _k().version())
        ability = node.paintAbility()
        if ability != "PAINT":
            raise CommandError("layer %r cannot take brush strokes (paintAbility=%s; the current preset may be a "
                               "MyPaint brush or the layer is not a paint layer)" % (node.name(), ability))
        view = _view_for(doc)
        doc.setActiveNode(node)
        _brush_apply(view, a.get("brush") or {})
        style = a.get("stroke_style", "ForegroundColor")
        fill = a.get("fill_style", "None")
        n_ops = 0
        for s in a.get("strokes") or []:
            kind = s.get("op", "stroke")
            if kind == "stroke":
                pts = _points(s["points"])
                base = float(s.get("pressure", 1.0))
                if len(pts) == 1:
                    x, y, pr = pts[0]
                    _float_or_int(node.paintLine, QPointF(x, y), QPointF(x, y), pr * base, pr * base, style)
                for (x1, y1, p1), (x2, y2, p2) in zip(pts, pts[1:]):
                    _float_or_int(node.paintLine, QPointF(x1, y1), QPointF(x2, y2), p1 * base, p2 * base, style)
            elif kind == "rect":
                _float_or_int(node.paintRectangle, QRectF(float(s["x"]), float(s["y"]), float(s["w"]), float(s["h"])), style, s.get("fill_style", fill))
            elif kind == "ellipse":
                _float_or_int(node.paintEllipse, QRectF(float(s["x"]), float(s["y"]), float(s["w"]), float(s["h"])), style, s.get("fill_style", fill))
            elif kind == "polygon":
                _float_or_int(node.paintPolygon, [QPointF(x, y) for x, y, _ in _points(s["points"])], style, s.get("fill_style", fill))
            else:
                raise CommandError("unknown paint op %r" % kind)
            n_ops += 1
        _settle(doc)
        info = _node_info(node)
        info["strokes"] = n_ops
        return info

    # ---- colour ---------------------------------------------------------------------
    def color_get(a):
        return brush_get(a)

    def color_set(a):
        doc = _doc(a)
        view = _view_for(doc)
        _brush_apply(view, {"color": a.get("foreground") or a.get("color"), "background": a.get("background")})
        return brush_get(a)

    # ---- actions --------------------------------------------------------------------
    def action_list(a):
        q = (a.get("filter") or "").lower()
        out = []
        for act in _k().actions():
            oid = act.objectName()
            if q and q not in oid.lower() and q not in act.text().lower():
                continue
            out.append({"id": oid, "text": act.text().replace("&", ""), "allowed": oid in ACTION_ALLOWLIST or bool(flags.get("any_action"))})
        return {"count": len(out), "actions": out[: int(a.get("limit", 300))]}

    def action_trigger(a):
        aid = a["id"]
        if aid not in ACTION_ALLOWLIST and not flags.get("any_action"):
            raise CommandError("action %r is not on the allow-list (create ~/.krita-mcp/any_action to lift it)" % aid)
        act = _k().action(aid)
        if act is None:
            raise CommandError("no action with id %r" % aid)
        if a.get("doc"):
            _view_for(_doc(a))
        act.trigger()
        d = _k().activeDocument()
        if d is not None:
            _settle(d)
        return {"triggered": aid, "text": act.text().replace("&", "")}

    # ---- escape hatch (off by default) ---------------------------------------------
    def run_python(a):
        if not flags.get("allow_exec"):
            raise CommandError("run_python is disabled (create ~/.krita-mcp/allow_exec and restart Krita to enable)")
        import krita as krita_mod
        ns = {"Krita": Krita, "krita": krita_mod, "doc": _k().activeDocument(), "__name__": "__krita_mcp__"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(compile(a["code"], "<krita-mcp>", "exec"), ns)  # noqa: S102 — gated
        return {"stdout": buf.getvalue(), "result": _jsonable(ns.get("result"))}

    return {
        "status": status,
        "document.new": document_new, "document.open": document_open, "document.save": document_save,
        "document.close": document_close, "document.info": document_info, "document.list": document_list,
        "document.activate": document_activate, "document.convert": document_convert,
        "preview": preview, "export": export,
        "layers": layers, "layer.info": layer_info, "layer.create": layer_create, "layer.set": layer_set,
        "layer.delete": layer_delete, "layer.move": layer_move, "layer.reorder": layer_reorder,
        "layer.duplicate": layer_duplicate, "layer.merge_down": layer_merge_down, "layer.activate": layer_activate,
        "import_image": import_image,
        "selection.get": selection_get, "selection.rect": selection_rect, "selection.ellipse": selection_ellipse,
        "selection.all": selection_all, "selection.none": selection_none, "selection.modify": selection_modify,
        "selection.from_mask": selection_from_mask, "selection.to_mask": selection_to_mask,
        "transform": transform,
        "filter.list": filter_list, "filter.params": filter_params, "filter.apply": filter_apply,
        "draw": draw, "paint": paint,
        "brush.get": brush_get, "brush.set": brush_set, "presets.list": presets_list,
        "color.get": color_get, "color.set": color_set,
        "action.list": action_list, "action.trigger": action_trigger,
        "run_python": run_python,
    }
