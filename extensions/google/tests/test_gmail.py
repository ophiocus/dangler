"""Offline tests for the Gmail draft MIME builder (no network, no token).

  uv run python -m unittest discover -s tests -v
"""
import base64
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path

from gws_mcp import gmail

LINK = "https://oidoenvivo.com/ciudad/bogota"
# A long link with a query string: quoted-printable must not break it.
LONG = "https://oidoenvivo.com/ciudad/bogota?utm_source=outreach&utm_medium=email&utm_campaign=talleres-2026-octubre&ref=ñ"
TEXT = f"Hola,\n\nLa página de Bogotá: {LINK}\nY esta otra: {LONG}\n\nSaludos — Carlos\n"
HTML = f'<p>Hola,</p><p>La página de <a href="{LINK}">Bogotá</a> y <a href="{LONG}">esta otra</a>.</p><p>{LINK}</p>'


def parse(msg):
    return BytesParser(policy=policy.default).parsebytes(msg.as_bytes())


def bodies(m):
    text = html = None
    for part in m.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            text = part.get_content()
        elif part.get_content_type() == "text/html" and not part.get_filename():
            html = part.get_content()
    return text, html


class BuildMessage(unittest.TestCase):
    def test_links_exact_in_both_parts(self):
        m = parse(gmail.build_message(to="a@example.com", subject="Prueba ñ", text=TEXT, html=HTML))
        self.assertEqual(m.get_content_type(), "multipart/alternative")
        text, html = bodies(m)
        self.assertEqual(text.replace("\r\n", "\n"), TEXT)
        self.assertEqual(html.rstrip("\r\n"), HTML)
        for body in (text, html):
            self.assertIn(LINK, body)
            self.assertIn(LONG, body)
            self.assertNotIn("google.com/url", body)
        self.assertEqual(m["Subject"], "Prueba ñ")

    def test_single_part_bodies(self):
        self.assertEqual(parse(gmail.build_message(to="a@x.co", text="t")).get_content_type(), "text/plain")
        self.assertEqual(parse(gmail.build_message(to="a@x.co", html="<b>h</b>")).get_content_type(), "text/html")
        with self.assertRaises(ValueError):
            gmail.build_message(to="a@x.co")

    def test_addresses(self):
        m = parse(gmail.build_message(to=["a@x.co", "Bé <b@x.co>"], cc="c@x.co", bcc=["d@x.co"], text="t"))
        self.assertEqual([a.addr_spec for a in m["To"].addresses], ["a@x.co", "b@x.co"])
        self.assertEqual(m["To"].addresses[1].display_name, "Bé")
        self.assertEqual(m["Cc"].addresses[0].addr_spec, "c@x.co")
        self.assertEqual(m["Bcc"].addresses[0].addr_spec, "d@x.co")

    def test_attachments(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d, "dossier ñ.pdf")
            pdf.write_bytes(b"%PDF-1.4 fake")
            blob = Path(d, "data.unknownext")
            blob.write_bytes(b"\x00\x01")
            m = parse(gmail.build_message(to="a@x.co", text=TEXT, html=HTML, attachments=[str(pdf), str(blob)]))
        self.assertEqual(m.get_content_type(), "multipart/mixed")
        parts = list(m.iter_parts())
        self.assertEqual(parts[0].get_content_type(), "multipart/alternative")
        att = {p.get_filename(): p for p in m.iter_attachments()}
        self.assertEqual(set(att), {"dossier ñ.pdf", "data.unknownext"})
        self.assertEqual(att["dossier ñ.pdf"].get_content_type(), "application/pdf")
        self.assertEqual(att["dossier ñ.pdf"].get_payload(decode=True), b"%PDF-1.4 fake")
        self.assertEqual(att["data.unknownext"].get_content_type(), "application/octet-stream")
        text, html = bodies(m)
        self.assertIn(LONG, text)
        self.assertIn(LONG, html)

    def test_missing_attachment(self):
        with self.assertRaises(FileNotFoundError):
            gmail.build_message(to="a@x.co", text="t", attachments=["Z:/nope/missing.pdf"])

    def test_reply_headers(self):
        m = parse(gmail.build_message(to="a@x.co", subject="Re: Hola", text="t",
                                      in_reply_to="<orig@mail.example>", references="<r1@x> <orig@mail.example>"))
        self.assertEqual(m["In-Reply-To"], "<orig@mail.example>")
        self.assertEqual(m["References"], "<r1@x> <orig@mail.example>")


class Reply(unittest.TestCase):
    def test_context_appends_message_id(self):
        ctx = gmail.reply_context([
            {"name": "Message-Id", "value": "<m2@x>"},
            {"name": "References", "value": "<m0@x> <m1@x>"},
            {"name": "Subject", "value": "Talleres en Bogotá"},
            {"name": "From", "value": "Taller <t@x.co>"},
        ])
        self.assertEqual(ctx["in_reply_to"], "<m2@x>")
        self.assertEqual(ctx["references"], "<m0@x> <m1@x> <m2@x>")
        self.assertEqual(ctx["subject"], "Re: Talleres en Bogotá")
        self.assertEqual(ctx["reply_address"], "Taller <t@x.co>")

    def test_context_first_message_prefers_reply_to(self):
        ctx = gmail.reply_context([
            {"name": "Message-ID", "value": "<m1@x>"},
            {"name": "Subject", "value": "RE: ya respondido"},
            {"name": "From", "value": "a@x.co"},
            {"name": "Reply-To", "value": "b@x.co"},
        ])
        self.assertEqual(ctx["references"], "<m1@x>")
        self.assertEqual(ctx["subject"], "RE: ya respondido")
        self.assertEqual(ctx["reply_address"], "b@x.co")


class ReadRaw(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "kit.pdf")
            f.write_bytes(b"%PDF")
            msg = gmail.build_message(to="a@x.co", subject="S", text=TEXT, html=HTML, attachments=[str(f)])
        # Gmail returns base64url without padding guarantees; strip it to prove read_raw copes.
        got = gmail.read_raw(gmail.to_raw(msg).rstrip("="))
        self.assertEqual(got["headers"]["Subject"], "S")
        self.assertIn(LINK, got["text"])
        self.assertIn(LONG, got["html"])
        self.assertEqual(got["attachments"], [{"filename": "kit.pdf", "mime_type": "application/pdf", "size": 4}])
        self.assertIn(LINK, got["links"])
        self.assertIn(LONG, got["links"])
        self.assertEqual(got["redirect_wrapped_links"], [])

    def test_flags_google_redirect(self):
        wrapped = "https://www.google.com/url?q=https://oidoenvivo.com/&source=gmail&ust=1&sa=E"
        msg = gmail.build_message(to="a@x.co", html=f'<a href="{wrapped}">x</a>')
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.assertEqual(gmail.read_raw(raw)["redirect_wrapped_links"], [wrapped])


if __name__ == "__main__":
    unittest.main()
