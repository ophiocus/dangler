"""Qt binding shim: Krita 5.x embeds PyQt5, Krita 6.x embeds PyQt6.

Krita 6 raises ModuleNotFoundError (a subclass of ImportError) when PyQt5 is imported, so
trying PyQt5 first and falling back is the documented-safe order inside Krita, where only one
binding ever exists. Every enum is used *scoped* (``QImage.Format.Format_ARGB32``) because that
is the one spelling both bindings accept.
"""
try:
    from PyQt5.QtCore import (QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, Qt, QTimer,  # noqa: F401
                              QUuid)
    from PyQt5.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen,  # noqa: F401
                             QPolygonF)
    QT_MAJOR = 5
except ImportError:  # Krita 6
    from PyQt6.QtCore import (QBuffer, QByteArray, QIODevice, QPointF, QRect, QRectF, Qt, QTimer,  # noqa: F401
                              QUuid)
    from PyQt6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen,  # noqa: F401
                             QPolygonF)
    QT_MAJOR = 6

ARGB32 = QImage.Format.Format_ARGB32
GRAY8 = QImage.Format.Format_Grayscale8


def image_bytes(img):
    """Raw bytes of a QImage, honouring bytesPerLine padding. Works on both bindings."""
    n = img.sizeInBytes() if hasattr(img, "sizeInBytes") else img.byteCount()
    ptr = img.bits()
    if hasattr(ptr, "setsize"):
        ptr.setsize(n)
    return ptr.asstring(n)


def argb32_bytes(img):
    """Tightly packed B,G,R,A rows (== Krita RGBA/U8 layout) for an ARGB32 image."""
    if img.format() != ARGB32:
        img = img.convertToFormat(ARGB32)
    w, h = img.width(), img.height()
    raw = image_bytes(img)
    bpl = img.bytesPerLine()
    if bpl == w * 4:
        return raw
    return b"".join(raw[y * bpl:y * bpl + w * 4] for y in range(h))


def gray8_bytes(img):
    """Tightly packed 1-byte-per-pixel rows for a Grayscale8 image (selection mask layout)."""
    if img.format() != GRAY8:
        img = img.convertToFormat(GRAY8)
    w, h = img.width(), img.height()
    raw = image_bytes(img)
    bpl = img.bytesPerLine()
    if bpl == w:
        return raw
    return b"".join(raw[y * bpl:y * bpl + w] for y in range(h))


def image_from_bgra(data, w, h):
    """QImage (owning its memory) from Krita RGBA/U8 bytes."""
    if isinstance(data, QByteArray):
        data = bytes(data)
    if len(data) < w * h * 4:
        data = data + b"\x00" * (w * h * 4 - len(data))
    return QImage(data, w, h, w * 4, ARGB32).copy()


def image_from_gray(data, w, h):
    if isinstance(data, QByteArray):
        data = bytes(data)
    if len(data) < w * h:
        data = data + b"\x00" * (w * h - len(data))
    return QImage(data, w, h, w, GRAY8).copy()


def png_bytes(img, fmt="PNG"):
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, fmt)
    return bytes(buf.data())


def qcolor(spec, default="#000000"):
    """'#rrggbb', '#rrggbbaa', a colour name, or [r,g,b(,a)] 0-255."""
    if spec is None:
        spec = default
    if isinstance(spec, (list, tuple)):
        vals = [int(v) for v in spec] + [255]
        return QColor(vals[0], vals[1], vals[2], vals[3])
    s = str(spec)
    if s.startswith("#") and len(s) == 9:               # #rrggbbaa -> QColor wants #aarrggbb
        s = "#" + s[7:9] + s[1:7]
    c = QColor(s)
    if not c.isValid():
        raise ValueError("bad colour: %r" % (spec,))
    return c
