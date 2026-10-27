"""Gmail drafts for gws-mcp — MIME building and reading, no network.

Why this exists: the claude.ai Gmail connector stores every link in a draft as a
`https://www.google.com/url?q=…` redirect. Here the message is built locally with
Python's `email` package and handed to `users.drafts.create` as raw RFC 822, so
what is stored is exactly what was given.

Sending is a separate tool that sends an existing draft only (server.gmail_draft_send).
"""
from __future__ import annotations

import base64
import mimetypes
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

Addresses = str | list[str] | None

# Headers asked for when looking up the message a draft replies to.
REPLY_HEADERS = ["Message-ID", "References", "Subject", "From", "Reply-To"]


def _addr(v: Addresses) -> str:
    if not v:
        return ""
    if isinstance(v, str):
        return v.strip()
    return ", ".join(a.strip() for a in v if a and a.strip())


def reply_subject(original: str) -> str:
    """'Re: <original>' unless it already starts with Re:."""
    original = (original or "").strip()
    return original if re.match(r"(?i)^re\s*:", original) else f"Re: {original}"


def reply_context(headers: list[dict]) -> dict:
    """Turn Gmail `payload.headers` of the original into the fields a reply needs."""
    h = {x.get("name", "").lower(): x.get("value", "") for x in headers}
    mid = h.get("message-id", "").strip()
    refs = h.get("references", "").strip()
    return {
        "in_reply_to": mid,
        "references": f"{refs} {mid}".strip() if mid and mid not in refs else refs,
        "subject": reply_subject(h.get("subject", "")),
        "reply_address": h.get("reply-to") or h.get("from", ""),
    }


def build_message(
    *,
    to: Addresses = None,
    cc: Addresses = None,
    bcc: Addresses = None,
    subject: str = "",
    text: str | None = None,
    html: str | None = None,
    attachments: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> EmailMessage:
    """Build the draft as an EmailMessage.

    text only -> text/plain; html only -> text/html; both -> multipart/alternative.
    Attachments wrap the body in multipart/mixed. Everything is UTF-8; bodies are
    never rewritten, so links stay exactly as given.
    """
    if not text and not html:
        raise ValueError("give a body: `text`, `html`, or both")
    msg = EmailMessage(policy=policy.SMTP)
    for name, val in (("To", _addr(to)), ("Cc", _addr(cc)), ("Bcc", _addr(bcc))):
        if val:
            msg[name] = val
    msg["Subject"] = subject or ""
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    if text:
        msg.set_content(text, subtype="plain", charset="utf-8")
        if html:
            msg.add_alternative(html, subtype="html", charset="utf-8")
    else:
        msg.set_content(html, subtype="html", charset="utf-8")

    for path in attachments or []:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"attachment not found: {p}")
        ctype, encoding = mimetypes.guess_type(p.name)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def to_raw(msg: EmailMessage) -> str:
    """base64url of the RFC 822 bytes, as Gmail's `message.raw` wants."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


_URL = re.compile(r"""https?://[^\s"'<>)\]]+""")


def read_raw(raw: str) -> dict:
    """Parse a stored `message.raw` back into headers, bodies, attachment names and links."""
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    msg = BytesParser(policy=policy.default).parsebytes(data)
    text: list[str] = []
    html: list[str] = []
    files: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn or part.get_content_disposition() == "attachment":
            payload = part.get_payload(decode=True) or b""
            files.append({"filename": fn, "mime_type": part.get_content_type(), "size": len(payload)})
        elif part.get_content_type() == "text/plain":
            text.append(part.get_content())
        elif part.get_content_type() == "text/html":
            html.append(part.get_content())
    body_text, body_html = "".join(text), "".join(html)
    links = list(dict.fromkeys(_URL.findall(body_text) + _URL.findall(body_html)))
    return {
        "headers": {k: str(msg[k]) for k in ("To", "Cc", "Bcc", "Subject", "In-Reply-To", "References") if msg[k]},
        "text": body_text,
        "html": body_html,
        "attachments": files,
        "links": links,
        "redirect_wrapped_links": [u for u in links if re.match(r"https?://(www\.)?google\.[a-z.]+/url\?", u)],
    }
