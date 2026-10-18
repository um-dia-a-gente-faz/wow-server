"""#841 (ADR 0012): the site's own SQLite store. Admin users, hashed passwords, sessions and the
reserved site row. Every test uses a temporary database and throwaway passwords.
"""
import contextlib
import hashlib
import io
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

# #670: point CALIBRATION_FILE at a temp file before any wowmap import
import _calibration_env  # noqa: F401
import pagesrc  # noqa: E402,F401  (puts wowmap/ on sys.path; no new sys.path edit, see ADR 0019)
import sitestore  # noqa: E402

PASSWORD = "throwaway-pass-1"  # 16 characters, well above the minimum
API_SOURCE = pathlib.Path(__file__).resolve().parents[1] / "wowmap" / "api.py"


class SiteStoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="sitestore-test-")
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "site.sqlite3")
        self.store = sitestore.SiteStore(self.path)
        self.addCleanup(self._close)

    def _close(self):
        self.store.close()

    def rows(self, sql, *args):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


class SchemaTest(SiteStoreCase):
    def test_schema_created_and_reopen_is_idempotent(self):
        names = {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"users", "sessions", "site", "schema_version"} <= names, names)
        self._close()
        self.store = sitestore.SiteStore(self.path)  # re-open runs the migration again
        self.assertEqual(self.rows("SELECT version FROM schema_version"), [(sitestore.SCHEMA_VERSION,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM site"), [(1,)])

    def test_wal_and_foreign_keys_are_on(self):
        self.assertEqual(self.store._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_presence_mode_column_exists_and_api_does_not_read_it(self):
        cols = {r[1] for r in self.rows("PRAGMA table_info(site)")}
        self.assertIn("presence_mode", cols)
        self.assertNotIn("presence_mode", API_SOURCE.read_text(encoding="utf-8"))


class AdminTest(SiteStoreCase):
    def test_create_and_verify_admin(self):
        uid = self.store.create_admin("owner", PASSWORD)
        self.assertEqual(self.store.verify("owner", PASSWORD), uid)

    def test_wrong_password_and_unknown_user_return_none(self):
        self.store.create_admin("owner", PASSWORD)
        self.assertIsNone(self.store.verify("owner", PASSWORD + "x"))
        self.assertIsNone(self.store.verify("nobody", PASSWORD))

    def test_duplicate_username_refused(self):
        self.store.create_admin("owner", PASSWORD)
        with self.assertRaises(ValueError):
            self.store.create_admin("owner", PASSWORD + "-other")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM users"), [(1,)])

    def test_short_password_refused(self):
        with self.assertRaises(ValueError):
            self.store.create_admin("owner", "x" * 11)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM users"), [(0,)])
        self.assertGreater(self.store.create_admin("owner", "x" * 12), 0)

    def test_password_stored_as_scrypt_not_in_clear(self):
        self.store.create_admin("owner", PASSWORD)
        (stored,) = self.rows("SELECT pw_hash FROM users WHERE username = 'owner'")[0]
        self.assertTrue(stored.startswith("scrypt$16384$8$1$"), stored)
        self.assertNotIn(PASSWORD, stored)


class SessionTest(SiteStoreCase):
    def setUp(self):
        super().setUp()
        self.uid = self.store.create_admin("owner", PASSWORD)

    def test_session_round_trip(self):
        token = self.store.create_session(self.uid)
        self.assertEqual(self.store.session_user(token), self.uid)
        self.store.delete_session(token)
        self.assertIsNone(self.store.session_user(token))

    def test_expired_session_is_none_and_row_gone(self):
        token = self.store.create_session(self.uid, ttl_s=-1)
        self.assertIsNone(self.store.session_user(token))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM sessions"), [(0,)])

    def test_unknown_token_is_none(self):
        self.assertIsNone(self.store.session_user("not-a-token"))

    def test_token_not_stored_in_clear(self):
        token = self.store.create_session(self.uid)
        self._close()  # checkpoints the WAL into the main file
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(self.rows("SELECT token_hash FROM sessions"), [(token_hash,)])
        for name in os.listdir(self._dir.name):
            with open(os.path.join(self._dir.name, name), "rb") as f:
                self.assertNotIn(token.encode(), f.read(), name)
        self.store = sitestore.SiteStore(self.path)  # keep the addCleanup close valid


class CliTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="sitestore-cli-")
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "site.sqlite3")

    def run_cli(self, passwords):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("getpass.getpass", side_effect=passwords) as gp, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sitestore.main(["create-admin", "--db", self.path, "--username", "owner"])
        self.assertEqual(gp.call_count, 2)  # asked twice: password and confirmation
        return code, out.getvalue() + err.getvalue()

    def users(self):
        store = sitestore.SiteStore(self.path)
        try:
            return store._conn.execute("SELECT username FROM users").fetchall()
        finally:
            store.close()

    def test_create_admin_with_matching_confirmation(self):
        code, _ = self.run_cli([PASSWORD, PASSWORD])
        self.assertEqual(code, 0)
        store = sitestore.SiteStore(self.path)
        try:
            self.assertIsNotNone(store.verify("owner", PASSWORD))
        finally:
            store.close()

    def test_mismatched_confirmation_fails_without_user(self):
        code, output = self.run_cli([PASSWORD, PASSWORD + "-typo"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.users(), [])
        self.assertNotIn(PASSWORD, output)

    def test_default_db_path_comes_from_env(self):
        with mock.patch.dict(os.environ, {"SITE_DB": "/somewhere/else.sqlite3"}):
            self.assertEqual(sitestore.default_db_path(), "/somewhere/else.sqlite3")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sitestore.default_db_path(), "/app/data/site.sqlite3")


if __name__ == "__main__":
    unittest.main()
