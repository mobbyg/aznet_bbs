"""
server/database.py — ANet BBS SQLite Database Layer

Handles all database operations: creating the DB on first run, user management,
and session tracking.

We use Python's built-in sqlite3 module — no extra packages required.
Passwords are hashed with PBKDF2-HMAC-SHA256 (Python stdlib, secure).

On first run (no DB file yet), init_db() creates all tables and the sysop
account with a password you set in config.
"""

import sqlite3
import hashlib
import hmac
import os
import logging
from pathlib import Path
from datetime import datetime

from config import Config

log = logging.getLogger('anet.database')


# --------------------------------------------------------------------------
# Password hashing
# Uses PBKDF2-HMAC-SHA256: a proper key-derivation function that is
# resistant to brute-force and rainbow-table attacks.
# --------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """
    Hash a password.  Returns (hash_bytes, salt_bytes).
    If salt is None, a new random 16-byte salt is generated.
    Store both in the database; you need both to verify later.
    """
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(
        hash_name   = 'sha256',
        password    = password.encode('utf-8'),
        salt        = salt,
        iterations  = 260_000,   # NIST recommended minimum as of 2024
    )
    return dk, salt


def verify_password(password: str, stored_hash: bytes, stored_salt: bytes) -> bool:
    """Return True if password matches the stored hash+salt."""
    candidate, _ = _hash_password(password, salt=stored_salt)
    # hmac.compare_digest is a constant-time comparison that prevents timing attacks.
    return hmac.compare_digest(candidate, stored_hash)


# --------------------------------------------------------------------------
# Database connection
# --------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    """
    Return a sqlite3 connection to the ANet database.
    row_factory = sqlite3.Row means rows behave like dicts: row['column_name'].
    """
    conn = sqlite3.connect(Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # WAL = better concurrent access
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------
# Schema creation
# --------------------------------------------------------------------------

_SCHEMA = """
-- Users table.  Mirrors CNet's user record structure.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    handle          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   BLOB    NOT NULL,
    password_salt   BLOB    NOT NULL,
    real_name       TEXT    DEFAULT '',
    location        TEXT    DEFAULT '',
    email           TEXT    DEFAULT '',
    access_group    INTEGER NOT NULL DEFAULT 5,   -- 0=lowest, 31=sysop
    created_at      TEXT    NOT NULL,
    last_call       TEXT,
    call_count      INTEGER NOT NULL DEFAULT 0,
    time_today      INTEGER NOT NULL DEFAULT 0,   -- minutes used today
    bytes_today     INTEGER NOT NULL DEFAULT 0,   -- bytes downloaded today
    is_deleted      INTEGER NOT NULL DEFAULT 0,   -- soft delete flag
    is_validated    INTEGER NOT NULL DEFAULT 1,   -- sysop validation flag
    notes           TEXT    DEFAULT ''            -- sysop notes
);

-- Node/session tracking.  One row per active or recently-ended session.
-- The sysop control panel reads this table to populate the node list.
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     INTEGER NOT NULL,
    user_id     INTEGER REFERENCES users(id),
    handle      TEXT,                            -- cached so we don't JOIN every update
    connected_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting', -- waiting / logging_in / online / disconnected
    location    TEXT DEFAULT '',                 -- caller's reported location / IP
    speed       INTEGER DEFAULT 0,               -- connection speed in cps/baud
    access_group INTEGER DEFAULT 0
);

-- System configuration (key/value store for sysop-editable settings)
CREATE TABLE IF NOT EXISTS system_config (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Activity log: the server writes significant events here; the sysop
-- control panel polls this table to populate its live activity feed.
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    node_id     INTEGER,
    message     TEXT    NOT NULL
);
"""


def init_db() -> None:
    """
    Create the database and all tables if they don't exist.
    On a brand-new install, also creates the sysop account.
    Safe to call on every startup — CREATE TABLE IF NOT EXISTS is idempotent.
    """
    # Make sure the data directory exists
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()

    _ensure_sysop_account()
    log.info("Database initialised at %s", Config.DB_PATH)


def _ensure_sysop_account() -> None:
    """
    Create the sysop account if it doesn't already exist.
    Password is prompted on first run so it's never stored in source code.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE handle = ?", (Config.SYSOP_HANDLE,)
        ).fetchone()

        if row is None:
            print("\n" + "="*60)
            print("  FIRST RUN — Creating SysOp account")
            print("  Handle:", Config.SYSOP_HANDLE)
            print("="*60)

            while True:
                import getpass
                pw1 = getpass.getpass("  Set SysOp password: ")
                pw2 = getpass.getpass("  Confirm password  : ")
                if pw1 == pw2 and len(pw1) >= 4:
                    break
                print("  Passwords don't match or too short (min 4 chars). Try again.")

            phash, psalt = _hash_password(pw1)
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO users
                    (handle, password_hash, password_salt, access_group,
                     created_at, is_validated, real_name)
                VALUES (?, ?, ?, ?, ?, 1, 'SysOp')
                """,
                (Config.SYSOP_HANDLE, phash, psalt, Config.SYSOP_AG, now),
            )
            conn.commit()
            print("  SysOp account created.\n")


# --------------------------------------------------------------------------
# User operations
# --------------------------------------------------------------------------

def get_user_by_handle(handle: str) -> sqlite3.Row | None:
    """Look up a user by handle (case-insensitive). Returns Row or None."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE handle = ? AND is_deleted = 0",
            (handle,)
        ).fetchone()


def authenticate_user(handle: str, password: str) -> sqlite3.Row | None:
    """
    Verify handle + password.
    Returns the user Row on success, None on failure.
    """
    user = get_user_by_handle(handle)
    if user is None:
        return None
    if not verify_password(password, bytes(user['password_hash']), bytes(user['password_salt'])):
        return None
    return user


def create_user(
    handle: str,
    password: str,
    real_name: str = '',
    location: str = '',
    email: str = '',
    access_group: int | None = None,
) -> int:
    """
    Create a new user account.  Returns the new user's id.
    Raises ValueError if the handle is already taken.
    """
    if access_group is None:
        access_group = Config.DEFAULT_NEW_USER_AG

    if get_user_by_handle(handle) is not None:
        raise ValueError(f"Handle '{handle}' is already in use.")

    phash, psalt = _hash_password(password)
    now = datetime.utcnow().isoformat()

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO users
                (handle, password_hash, password_salt, real_name, location,
                 email, access_group, created_at, is_validated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (handle, phash, psalt, real_name, location, email, access_group, now),
        )
        conn.commit()
        log.info("New user created: %s (AG %d)", handle, access_group)
        return cur.lastrowid


def update_last_call(user_id: int) -> None:
    """Update last_call timestamp and increment call_count."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_call = ?, call_count = call_count + 1 WHERE id = ?",
            (now, user_id),
        )
        conn.commit()


# --------------------------------------------------------------------------
# Session / node tracking
# --------------------------------------------------------------------------

def register_node_waiting(node_id: int) -> None:
    """Mark a node as waiting for a call (unauthenticated connection)."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        # Delete any stale record for this node first
        conn.execute("DELETE FROM sessions WHERE node_id = ?", (node_id,))
        conn.execute(
            """
            INSERT INTO sessions (node_id, connected_at, last_activity, status)
            VALUES (?, ?, ?, 'waiting')
            """,
            (node_id, now, now),
        )
        conn.commit()


def update_node_online(
    node_id: int,
    user_id: int,
    handle: str,
    location: str = '',
    speed: int = 0,
    access_group: int = 0,
) -> None:
    """Update a node record once a user has successfully logged in."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE sessions
            SET user_id = ?, handle = ?, status = 'online',
                last_activity = ?, location = ?, speed = ?, access_group = ?
            WHERE node_id = ?
            """,
            (user_id, handle, now, location, speed, access_group, node_id),
        )
        conn.commit()


def clear_node(node_id: int) -> None:
    """Remove a node's session record when the connection closes."""
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE node_id = ?", (node_id,))
        conn.commit()


def get_active_sessions() -> list[sqlite3.Row]:
    """Return all current session rows, ordered by node_id."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM sessions ORDER BY node_id"
        ).fetchall()


# --------------------------------------------------------------------------
# Activity log
# --------------------------------------------------------------------------

def write_activity(message: str, node_id: int | None = None) -> None:
    """
    Append an event to the activity_log table.
    The sysop control panel polls this table to show the live feed.

    Args:
        message  : human-readable description of the event
        node_id  : the node this event relates to, or None for system events
    """
    now = datetime.utcnow().isoformat(timespec='seconds')
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO activity_log (timestamp, node_id, message) VALUES (?, ?, ?)",
                (now, node_id, message),
            )
            conn.commit()
    except Exception as exc:
        # Never crash the BBS because of a logging failure
        log.warning("activity_log write failed: %s", exc)
