"""Offline: several identities in one server, picked per call with `account`.

  uv run python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gws_mcp import auth, server

ACCOUNTS = """
[accounts.shared]
token = "{dir}/shared-token.json"
scopes = ["drive", "documents", "presentations"]
default_from = "Ejemplo <hola@example.org>"
login_hint = "mailbox@example.com"

[accounts.mail]
token = "{dir}/mail-token.json"
scopes = "gmail.compose,gmail.readonly"
"""


class AccountsFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = Path(self.dir.name, "gws-accounts.toml")
        for p in (mock.patch.object(auth, "ACCOUNTS_FILE", self.file),
                  mock.patch.object(auth, "_accounts", {}), mock.patch.object(auth, "_accounts_state", None)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, text):
        self.file.write_text(text.format(dir=Path(self.dir.name).as_posix()), encoding="utf-8")
        st = self.file.stat()
        os.utime(self.file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def test_no_file_serves_only_the_env_identity(self):
        self.assertEqual(list(auth.accounts()), ["default"])
        self.assertIs(auth.resolve(None), auth.DEFAULT)

    def test_named_identities_carry_their_own_settings(self):
        self.write(ACCOUNTS)
        shared = auth.resolve("shared")
        self.assertEqual(shared.token_file, Path(self.dir.name, "shared-token.json"))
        self.assertEqual(shared.scopes, tuple(auth.scope_list("drive,documents,presentations")))
        self.assertEqual(shared.default_from, "Ejemplo <hola@example.org>")
        self.assertEqual(shared.login_hint, "mailbox@example.com")
        self.assertEqual(shared.client_secret, auth.DEFAULT.client_secret)
        self.assertEqual(auth.resolve("mail").scopes, (auth.GMAIL_COMPOSE, auth.GMAIL_READONLY))

    def test_the_file_is_re_read_when_it_changes(self):
        self.write(ACCOUNTS)
        self.assertIn("shared", auth.accounts())
        self.write("[accounts.other]\ntoken = \"{dir}/o.json\"\n")
        self.assertEqual(sorted(auth.accounts()), ["default", "other"])

    def test_unknown_account_names_the_known_ones(self):
        self.write(ACCOUNTS)
        with self.assertRaisesRegex(RuntimeError, "unknown account 'nope'.*default, mail, shared"):
            auth.resolve("nope")

    def test_bad_entries_are_refused(self):
        with self.assertRaisesRegex(ValueError, "no `token`"):
            auth.parse_accounts("[accounts.x]\nscopes = 'drive'\n")
        with self.assertRaisesRegex(ValueError, "env identity"):
            auth.parse_accounts("[accounts.default]\ntoken = 't'\n")


class ToolsTakeAnAccount(unittest.TestCase):
    def test_every_tool_but_accounts_has_an_optional_account_argument(self):
        tools = asyncio.run(server.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertTrue({"drive_create_folder", "drive_upload", "drive_move", "drive_rename", "drive_shared_drives",
                         "slides_create", "slides_get", "slides_batch_update", "accounts"} <= names)
        for t in tools:
            props = t.inputSchema.get("properties", {})
            if t.name == "accounts":
                self.assertNotIn("account", props)
                continue
            self.assertIn("account", props, t.name)
            self.assertNotIn("account", t.inputSchema.get("required", []), t.name)

    def test_the_call_runs_as_the_named_identity(self):
        seen = []
        other = auth.Identity("o", Path("s.json"), Path("o.json"), ())

        @server.tool
        def probe_identity(x: int) -> str:
            seen.append((x, server._ident().name))
            return "ok"

        with mock.patch.object(auth, "resolve", lambda a: other if a == "o" else auth.DEFAULT):
            self.assertEqual(probe_identity(1, account="o"), "ok")
            self.assertEqual(probe_identity(2), "ok")
        self.assertEqual(seen, [(1, "o"), (2, "default")])
        self.assertIs(server._ident(), auth.DEFAULT)

    def test_an_unknown_account_is_an_error_string_not_a_crash(self):
        out = server.drive_get("id", account="no-such-account-here")
        self.assertIn("unknown account", out)


class ScopeGuards(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.token = Path(self.dir.name, "t.json")

    def ident(self, *short):
        self.token.write_text(json.dumps({"scopes": auth.scope_list(list(short))}))
        return auth.Identity("t", Path("s.json"), self.token, ())

    def test_drive_write_needs_drive_or_drive_file(self):
        with self.assertRaisesRegex(RuntimeError, "not granted drive or drive.file"):
            auth.require_any(self.ident("drive.readonly", "documents"), *auth.DRIVE_WRITE)
        auth.require_any(self.ident("drive.file"), *auth.DRIVE_WRITE)

    def test_write_tools_refuse_a_read_only_identity_before_calling_google(self):
        ident = self.ident("drive.readonly")
        with mock.patch.object(auth, "resolve", lambda a: ident), mock.patch.object(server, "_svc") as svc:
            out = server.drive_create_folder("x", account="t")
        self.assertIn("not granted drive or drive.file", out)
        svc.assert_not_called()

    def test_whoami_uses_drive_for_any_drive_scope(self):
        calls = []

        class Fake:
            def __init__(self, api): self.api = api
            def about(self): return self
            def users(self): return self
            def get(self, **kw): return self
            def getProfile(self, **kw): return self
            def execute(self):
                calls.append(self.api)
                return {"user": {"emailAddress": "d@example.com"}} if self.api == "drive" else {"emailAddress": "g@example.com"}

        creds = mock.Mock(scopes=auth.scope_list("drive,presentations"))
        with mock.patch("googleapiclient.discovery.build", lambda api, ver, **kw: Fake(api)):
            self.assertEqual(auth.whoami(auth.DEFAULT, creds), "d@example.com")
            creds.scopes = auth.scope_list("gmail.compose")
            self.assertEqual(auth.whoami(auth.DEFAULT, creds), "g@example.com")
            creds.scopes = auth.scope_list("documents")
            self.assertIn("no Drive or Gmail scope", auth.whoami(auth.DEFAULT, creds))
        self.assertEqual(calls, ["drive", "gmail"])


class Upload(unittest.TestCase):
    def test_convert_to_sets_the_google_type_and_drops_the_extension(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "Plan 2027.xlsx")
            src.write_bytes(b"PK")
            drive = mock.MagicMock()
            drive.files().create().execute.return_value = {"id": "f1"}
            with mock.patch.object(server, "_drive_write", lambda: drive):
                out = json.loads(server.drive_upload(str(src), parent_id="folder", convert_to="sheet"))
            kwargs = drive.files().create.call_args.kwargs
            self.assertEqual(out, {"id": "f1"})
            self.assertEqual(kwargs["body"], {"name": "Plan 2027", "parents": ["folder"],
                                              "mimeType": "application/vnd.google-apps.spreadsheet"})
            self.assertTrue(kwargs["supportsAllDrives"])

    def test_unknown_conversion_is_refused(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            pass
        try:
            self.assertIn("unsupported convert_to", server.drive_upload(f.name, convert_to="pdf"))
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
