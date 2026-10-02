"""krita_mcp_bridge — loopback bridge that lets the krita-mcp server drive this Krita.

Installed by `uv run --directory I:/dangler/extensions/krita krita-mcp install-plugin` into
%APPDATA%\\krita\\pykrita\\ and enabled in kritarc. Source of truth: the dangler repo.
"""
from krita import Krita

from .extension import KritaMcpBridgeExtension

Krita.instance().addExtension(KritaMcpBridgeExtension(Krita.instance()))
