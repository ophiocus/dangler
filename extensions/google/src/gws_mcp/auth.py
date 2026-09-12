"""OAuth for gws-mcp.

Design rules
- Uses YOUR OAuth Desktop client (client_secret.json) — the app is you.
- Scopes are the minimum that gives full Docs/Sheets CRUD without any Drive
  write scope: documents, spreadsheets, drive.readonly (search + export).
- `gws-mcp auth` is an explicit, blocking, standalone flow (loopback server on
  an ephemeral port) — there is no "auth on first tool call", so the
  dead-callback problem cannot happen. Tools fail fast with a clear message if
  no token exists.
- Secrets never printed. Token file is per-machine, chmod 600 where supported.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

DEFAULT_DIR = Path(os.environ.get("GWS_MCP_DIR", str(Path.home() / ".gcp")))
CLIENT_SECRET = Path(os.environ.get("GWS_MCP_CLIENT_SECRET", str(DEFAULT_DIR / "client_secret.json")))
TOKEN_FILE = Path(os.environ.get("GWS_MCP_TOKEN", str(DEFAULT_DIR / "gws-mcp-token.json")))


def _chmod600(p: Path) -> None:
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # Windows: rely on the user profile ACL


def load_credentials() -> Credentials:
    """Return valid credentials or raise a clear RuntimeError telling the agent to run `gws-mcp auth`."""
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"No Google token at {TOKEN_FILE}. Run `gws-mcp auth` once on this machine "
            f"(uses {CLIENT_SECRET}), approve in the browser, then retry."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        _chmod600(TOKEN_FILE)
        return creds
    raise RuntimeError(
        "Google token is invalid and cannot be refreshed (revoked, or the OAuth app is in "
        "'Testing' status and the 7-day refresh-token limit hit). Run `gws-mcp auth` again."
    )


def run_auth(open_browser: bool = True, port: int = 0) -> str:
    """Interactive consent. Blocks until the browser round-trip completes. Returns the authorized email."""
    if not CLIENT_SECRET.exists():
        raise SystemExit(
            f"client_secret.json not found at {CLIENT_SECRET}. Download the Desktop OAuth client JSON "
            "from https://console.cloud.google.com/auth/clients and place it there (or set GWS_MCP_CLIENT_SECRET)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(
        port=port,
        open_browser=open_browser,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message="Open this URL to authorize gws-mcp:\n{url}\n",
        success_message="gws-mcp authorized. You can close this tab.",
    )
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    _chmod600(TOKEN_FILE)
    return whoami(creds)


def whoami(creds: Credentials | None = None) -> str:
    """Email of the authorized account (via Drive about.get, covered by drive.readonly)."""
    from googleapiclient.discovery import build

    creds = creds or load_credentials()
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    about = svc.about().get(fields="user(emailAddress)").execute()
    return about.get("user", {}).get("emailAddress", "?")


def revoke() -> None:
    """Revoke the token at Google and delete the local file."""
    import urllib.parse
    import urllib.request

    if not TOKEN_FILE.exists():
        print("no token file; nothing to revoke")
        return
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    tok = data.get("refresh_token") or data.get("token")
    if tok:
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=urllib.parse.urlencode({"token": tok}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"revoked at Google (HTTP {r.status})")
        except Exception as e:  # already invalid is fine
            print(f"revoke request failed ({e}); deleting local token anyway", file=sys.stderr)
    TOKEN_FILE.unlink(missing_ok=True)
    print(f"deleted {TOKEN_FILE}")
