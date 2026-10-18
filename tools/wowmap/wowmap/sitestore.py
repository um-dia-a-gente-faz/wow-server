"""The site's own SQLite store (ADR 0012, #841): admin users, sessions and the reserved site row.

Stdlib only. It never touches the game database. Passwords are scrypt hashes; a session token
is shown once and only its sha256 is stored. `python -m sitestore create-admin` is the only way to
make an admin: there is no default user and no default password.
"""
import argparse
import base64
import datetime as dt
import functools
import getpass
import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading

DEFAULT_DB = "/app/data/site.sqlite3"
MIN_PASSWORD = 12
SESSION_TTL_S = 7 * 24 * 3600
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN = 2 ** 14, 8, 1, 64

# ADR 0018 (SQLite, authoritative): the version is PRAGMA user_version. Forward-only: append a step,
# never edit one that has shipped. Step N brings a file from user_version N-1 to N.
MIGRATIONS = ((
    """CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)""",
    # Reserved for the presence mode (ADR 0012): one row, not read by any API yet.
    "CREATE TABLE site (id INTEGER PRIMARY KEY CHECK (id = 1), presence_mode TEXT)",
    "INSERT INTO site (id, presence_mode) VALUES (1, NULL)",
),)
SCHEMA_VERSION = len(MIGRATIONS)


class SiteStoreError(Exception):
    """The store file cannot be used (newer version, unreadable or corrupt). Startup stops; the file is left alone."""


def default_db_path():
    return os.environ.get("SITE_DB", DEFAULT_DB)


def _now():
    return dt.datetime.now(dt.UTC)


def _iso(when):
    # Fixed-width UTC, so string comparison is time comparison.
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=SCRYPT_DKLEN)
    return (f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
            f"{base64.b64encode(salt).decode('ascii')}${base64.b64encode(digest).decode('ascii')}")


def check_password(password, stored):
    """True when `password` matches the stored scrypt hash. Uses the parameters stored with the hash."""
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@functools.lru_cache(maxsize=1)
def _dummy_hash():
    # Verifying an unknown user costs the same scrypt as a known one (no timing hint on usernames).
    return hash_password(secrets.token_urlsafe(16))


class SiteStore:
    def __init__(self, path):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            # Before any write (the WAL switch rewrites the header): a file with tables but no version is
            # not ours to migrate, whatever its tables are called.
            if version == 0 and self._conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
                raise SiteStoreError(f"{path}: has tables but no user_version; refusing to start. "
                                     "The file is left as it is; restore a backup or point SITE_DB elsewhere.")
            if version > SCHEMA_VERSION:
                raise SiteStoreError(
                    f"{path}: schema version {version} is newer than this code ({SCHEMA_VERSION}); "
                    "refusing to start. Run the newer code or restore an older backup.")
            if 0 < version < SCHEMA_VERSION:  # ADR 0018: keep the old file before the first step runs
                backup = sqlite3.connect(f"{path}.v{version}.bak")
                try:
                    self._conn.backup(backup)
                finally:
                    backup.close()
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            _migrate(self._conn, version)
        except sqlite3.DatabaseError as e:
            self._conn.close()
            raise SiteStoreError(f"{path}: cannot be read or migrated ({e}); refusing to start. "
                                 "The file is left as it is; restore a backup.") from e
        except BaseException:
            self._conn.close()
            raise

    def close(self):
        with self._lock:
            self._conn.close()

    def create_admin(self, username, password):
        """Add an admin user; returns its id. ValueError on a short password or a taken username."""
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        pw_hash = hash_password(password)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)",
                    (username, pw_hash, _iso(_now())))
            except sqlite3.IntegrityError:
                raise ValueError(f"user {username!r} already exists") from None
        return cur.lastrowid

    def verify(self, username, password):
        """The user id when the password matches, else None. Same work for unknown users."""
        with self._lock:
            row = self._conn.execute("SELECT id, pw_hash FROM users WHERE username = ?",
                                     (username,)).fetchone()
        if row is None:
            check_password(password, _dummy_hash())
            return None
        return row[0] if check_password(password, row[1]) else None

    def create_session(self, user_id, ttl_s=SESSION_TTL_S):
        """A new session; returns the token (shown once). Only its sha256 is stored."""
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_token_hash(token), user_id, _iso(now), _iso(now + dt.timedelta(seconds=ttl_s))))
        return token

    def session_user(self, token):
        """The user id for a live session, else None. An expired session is deleted."""
        digest = _token_hash(token)
        with self._lock:
            row = self._conn.execute("SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
                                     (digest,)).fetchone()
            if row is None:
                return None
            user_id, expires_at = row
            if expires_at <= _iso(_now()):
                self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
                return None
        return user_id

    def delete_session(self, token):
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _migrate(conn, version):
    """Run the steps after `version`, each in its own transaction that also sets user_version."""
    for n in range(version, SCHEMA_VERSION):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[n]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {n + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sitestore")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create an admin user (password is prompted)")
    admin.add_argument("--db", default=default_db_path(), help="store file (default: $SITE_DB)")
    admin.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    # The password is read here only: never an argument, never an environment variable, never echoed.
    password = getpass.getpass("Admin password: ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords do not match; no user created", file=sys.stderr)
        return 1
    try:
        store = SiteStore(args.db)
    except SiteStoreError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        store.create_admin(args.username, password)
    except ValueError as e:
        print(f"not created: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"admin {args.username} created in {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
