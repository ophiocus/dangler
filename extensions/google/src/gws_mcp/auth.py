"""OAuth for gws-mcp.

Design rules
- Uses YOUR OAuth Desktop client (client_secret.json) — the app is you.
- The default identity's scopes are the minimum that gives full Docs/Sheets CRUD without
  any Drive write scope: documents, spreadsheets, drive.readonly (search + export).
  Gmail adds gmail.compose (create/update/read/delete drafts — and, by Google's
  definition, send: gmail_draft_send sends an existing draft only) and gmail.readonly, which
  only the reply lookup uses: gmail.compose cannot read the message a draft
  answers, and its Message-ID/threadId are what put the draft in the thread.
  Drive write (`drive` or `drive.file`) and Slides (`presentations`) are granted only to an
  identity that names them.
- Several identities, one process: the env identity is the default, and an accounts file
  (GWS_MCP_ACCOUNTS, default ~/.gcp/gws-accounts.toml) names more. Each has its own token,
  scope set, default From and login hint; a tool call picks one with `account`. The file is
  re-read when it changes, and holds paths, never a token.
- Credentials load with the scopes the token was granted, so a token minted
  before a scope was added keeps working for the tools it covers; tools that
  need a newer scope call `require()` and ask for `gws-mcp auth` again.
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
import tomllib
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

_SCOPE_ROOT = "https://www.googleapis.com/auth/"
ALL_SCOPES = [
    _SCOPE_ROOT + "documents",
    _SCOPE_ROOT + "spreadsheets",
    _SCOPE_ROOT + "drive.readonly",
    _SCOPE_ROOT + "gmail.compose",
    _SCOPE_ROOT + "gmail.readonly",
]

GMAIL_COMPOSE = _SCOPE_ROOT + "gmail.compose"
GMAIL_READONLY = _SCOPE_ROOT + "gmail.readonly"
DRIVE = _SCOPE_ROOT + "drive"
DRIVE_FILE = _SCOPE_ROOT + "drive.file"
DRIVE_READONLY = _SCOPE_ROOT + "drive.readonly"
PRESENTATIONS = _SCOPE_ROOT + "presentations"
# Any of these lets drive.about.get name the account; the first two also write.
DRIVE_ANY = (DRIVE, DRIVE_FILE, DRIVE_READONLY)
DRIVE_WRITE = (DRIVE, DRIVE_FILE)

DEFAULT_ACCOUNT = "default"
DEFAULT_DIR = Path(os.environ.get("GWS_MCP_DIR", str(Path.home() / ".gcp")))
ACCOUNTS_FILE = Path(os.environ.get("GWS_MCP_ACCOUNTS", str(DEFAULT_DIR / "gws-accounts.toml"))).expanduser()


def scope_list(value: str | list[str] | None) -> list[str]:
    """Short names ("gmail.compose") or full URLs, comma-separated or a list, to full scope URLs."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    return [s if s.startswith("https://") else _SCOPE_ROOT + s for s in (i.strip() for i in items) if s]


@dataclass(frozen=True)
class Identity:
    name: str
    client_secret: Path
    token_file: Path
    scopes: tuple[str, ...]
    default_from: str | None = None
    login_hint: str | None = None


def _path(p: str) -> Path:
    return Path(p).expanduser()


# The env identity: what a fleet entry's own env sets (client secret, token, scope set,
# default From, the account to suggest at consent). Calls without `account` use it.
DEFAULT = Identity(
    name=DEFAULT_ACCOUNT,
    client_secret=_path(os.environ.get("GWS_MCP_CLIENT_SECRET", str(DEFAULT_DIR / "client_secret.json"))),
    token_file=_path(os.environ.get("GWS_MCP_TOKEN", str(DEFAULT_DIR / "gws-mcp-token.json"))),
    scopes=tuple(scope_list(os.environ.get("GWS_MCP_SCOPES")) or ALL_SCOPES),
    default_from=os.environ.get("GWS_MCP_DEFAULT_FROM", "").strip() or None,
    login_hint=os.environ.get("GWS_MCP_LOGIN_HINT", "").strip() or None,
)

_accounts: dict[str, Identity] = {}
_accounts_state: tuple[int, int] | None = None


def parse_accounts(text: str, source: str = "accounts file") -> dict[str, Identity]:
    """`[accounts.<name>]` tables: token (required), scopes, default_from, login_hint, client_secret."""
    out: dict[str, Identity] = {}
    for name, t in (tomllib.loads(text).get("accounts") or {}).items():
        if name == DEFAULT_ACCOUNT:
            raise ValueError(f"{source}: '{DEFAULT_ACCOUNT}' is the env identity; pick another name")
        if not t.get("token"):
            raise ValueError(f"{source}: account '{name}' has no `token` path")
        out[name] = Identity(
            name=name,
            client_secret=_path(t["client_secret"]) if t.get("client_secret") else DEFAULT.client_secret,
            token_file=_path(t["token"]),
            scopes=tuple(scope_list(t.get("scopes")) or ALL_SCOPES),
            default_from=(t.get("default_from") or "").strip() or None,
            login_hint=(t.get("login_hint") or "").strip() or None,
        )
    return out


def accounts() -> dict[str, Identity]:
    """Every identity this process serves, the accounts file re-read whenever it changes."""
    global _accounts, _accounts_state
    try:
        st = ACCOUNTS_FILE.stat()
        state = (st.st_mtime_ns, st.st_size)
    except OSError:
        state = None
    if state != _accounts_state:
        _accounts = parse_accounts(ACCOUNTS_FILE.read_text(encoding="utf-8"), str(ACCOUNTS_FILE)) if state else {}
        _accounts_state = state
    return {DEFAULT_ACCOUNT: DEFAULT, **_accounts}


def resolve(account: str | None) -> Identity:
    """The identity a call names; None is the env identity."""
    if not account:
        return DEFAULT
    known = accounts()
    if account not in known:
        raise RuntimeError(f"unknown account '{account}'; this server has: {', '.join(sorted(known))} "
                           f"(more are added in {ACCOUNTS_FILE})")
    return known[account]


def _chmod600(p: Path) -> None:
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # Windows: rely on the user profile ACL


def _auth_cmd(ident: Identity) -> str:
    return "`gws-mcp auth`" if ident.name == DEFAULT_ACCOUNT else f"`gws-mcp auth --account {ident.name}`"


def load_credentials(ident: Identity = DEFAULT) -> Credentials:
    """Return valid credentials or raise a clear RuntimeError telling the agent to run `gws-mcp auth`."""
    if not ident.token_file.exists():
        raise RuntimeError(
            f"No Google token at {ident.token_file}. Run {_auth_cmd(ident)} once on this machine "
            f"(uses {ident.client_secret}), approve in the browser, then retry."
        )
    # No scopes argument: use what the token was granted (see module docstring).
    creds = Credentials.from_authorized_user_file(str(ident.token_file))
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        ident.token_file.write_text(creds.to_json(), encoding="utf-8")
        _chmod600(ident.token_file)
        return creds
    raise RuntimeError(
        "Google token is invalid and cannot be refreshed (revoked, or the OAuth app is in "
        f"'Testing' status and the 7-day refresh-token limit hit). Run {_auth_cmd(ident)} again."
    )


def granted_scopes(ident: Identity = DEFAULT) -> list[str]:
    """Scopes recorded in the token file (no secret is read out of it)."""
    if not ident.token_file.exists():
        return []
    return list(json.loads(ident.token_file.read_text(encoding="utf-8")).get("scopes") or [])


def require(ident: Identity, *scopes: str) -> None:
    """Raise a clear RuntimeError if the token lacks any of `scopes`."""
    granted = granted_scopes(ident)
    missing = [s for s in scopes if s not in granted]
    if missing:
        short = ", ".join(s.rsplit("/", 1)[-1] for s in missing)
        raise RuntimeError(
            f"Account '{ident.name}' (token {ident.token_file}) is not granted {short}. Add it to that "
            f"identity's scopes and run {_auth_cmd(ident)} again, then retry."
        )


def require_any(ident: Identity, *scopes: str) -> None:
    """Raise a clear RuntimeError unless the token holds at least one of `scopes`."""
    if not set(scopes) & set(granted_scopes(ident)):
        short = " or ".join(s.rsplit("/", 1)[-1] for s in scopes)
        raise RuntimeError(
            f"Account '{ident.name}' (token {ident.token_file}) is not granted {short}. Use an account "
            f"that is, or add the scope to this identity and run {_auth_cmd(ident)} again."
        )


def run_auth(ident: Identity = DEFAULT, open_browser: bool = True, port: int = 0) -> str:
    """Interactive consent. Blocks until the browser round-trip completes. Returns the authorized email."""
    if not ident.client_secret.exists():
        raise SystemExit(
            f"client_secret.json not found at {ident.client_secret}. Download the Desktop OAuth client JSON "
            "from https://console.cloud.google.com/auth/clients and place it there (or set GWS_MCP_CLIENT_SECRET)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(ident.client_secret), list(ident.scopes))
    extra = {"login_hint": ident.login_hint} if ident.login_hint else {}
    creds = flow.run_local_server(
        **extra,
        port=port,
        open_browser=open_browser,
        prompt="consent",
        access_type="offline",
        authorization_prompt_message="Open this URL to authorize gws-mcp:\n{url}\n",
        success_message="gws-mcp authorized. You can close this tab.",
    )
    ident.token_file.parent.mkdir(parents=True, exist_ok=True)
    ident.token_file.write_text(creds.to_json(), encoding="utf-8")
    _chmod600(ident.token_file)
    try:
        return whoami(ident, creds)
    except Exception as e:  # the token is saved; naming the account is only a courtesy
        return f"? (token saved; could not read the address: {type(e).__name__})"


def whoami(ident: Identity = DEFAULT, creds: Credentials | None = None) -> str:
    """Email of the authorized account: Drive about.get with any Drive scope, else the Gmail profile."""
    from googleapiclient.discovery import build

    creds = creds or load_credentials(ident)
    granted = set(creds.scopes or granted_scopes(ident))
    if granted & set(DRIVE_ANY):
        about = build("drive", "v3", credentials=creds, cache_discovery=False).about().get(fields="user(emailAddress)").execute()
        return about.get("user", {}).get("emailAddress", "?")
    if granted & {GMAIL_COMPOSE, GMAIL_READONLY}:
        prof = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
        return prof.get("emailAddress", "?")
    return "? (the token has no Drive or Gmail scope to read the address with)"


def revoke(ident: Identity = DEFAULT) -> None:
    """Revoke the token at Google and delete the local file."""
    import urllib.parse
    import urllib.request

    if not ident.token_file.exists():
        print("no token file; nothing to revoke")
        return
    data = json.loads(ident.token_file.read_text(encoding="utf-8"))
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
    ident.token_file.unlink(missing_ok=True)
    print(f"deleted {ident.token_file}")
