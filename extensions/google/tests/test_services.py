"""Offline: API clients follow the token file, so a re-minted token needs no restart.

  uv run python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gws_mcp import server


class ServicesFollowTheToken(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.token = Path(self.dir.name, "token.json")
        self.built = []
        patches = [
            mock.patch.object(server.auth, "TOKEN_FILE", self.token),
            mock.patch.object(server.auth, "load_credentials", lambda: self.token.read_text()),
            mock.patch.object(server, "build", lambda name, ver, credentials, cache_discovery: self.built.append(credentials) or credentials),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        server._services.clear()
        server._services_token = None
        self.addCleanup(self.dir.cleanup)

    def test_reused_while_the_token_is_unchanged(self):
        self.token.write_text("one")
        self.assertEqual(server._svc("drive", "v3"), "one")
        self.assertEqual(server._svc("drive", "v3"), "one")
        self.assertEqual(self.built, ["one"])

    def test_rebuilt_after_a_re_mint(self):
        self.token.write_text("one")
        server._svc("gmail", "v1")
        self.token.write_text("second")
        st = self.token.stat()
        os.utime(self.token, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertEqual(server._svc("gmail", "v1"), "second")
        self.assertEqual(self.built, ["one", "second"])

    def test_token_minted_after_a_failed_call(self):
        with mock.patch.object(server.auth, "load_credentials", side_effect=RuntimeError("no token")):
            with self.assertRaises(RuntimeError):
                server._svc("gmail", "v1")
        self.token.write_text("fresh")
        self.assertEqual(server._svc("gmail", "v1"), "fresh")


if __name__ == "__main__":
    unittest.main()
