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
    notes           TEXT    DEFAULT '',           -- sysop notes
    term_type       TEXT    NOT NULL DEFAULT 'IBM', -- terminal type: IBM AMIGA CBM SKY NONE
    phone           TEXT    DEFAULT '',           -- voice phone number
    dob             TEXT    DEFAULT '',           -- date of birth (YYYY-MM-DD)
    gender          TEXT    DEFAULT '',           -- M / F
    ansi_level      TEXT    NOT NULL DEFAULT 'Simple', -- None / Simple / Full
    needs_lf        INTEGER NOT NULL DEFAULT 0,   -- terminal needs LF after CR
    screen_width    INTEGER NOT NULL DEFAULT 80,
    screen_height   INTEGER NOT NULL DEFAULT 24,
    ansi_color      INTEGER NOT NULL DEFAULT 1,   -- 1=yes, 0=no
    ansi_tabs       INTEGER NOT NULL DEFAULT 0,   -- 1=yes, 0=no
    -- VDE EA fields
    user_banner     TEXT    NOT NULL DEFAULT '',   -- WHO listing banner line
    data_phone      TEXT    NOT NULL DEFAULT '',   -- modem/data phone number
    priv_flags      INTEGER NOT NULL DEFAULT 0,    -- privilege bitmask (43 bits)
    -- VDE EA: Credits/Balances
    uploads_today       INTEGER NOT NULL DEFAULT 0,
    downloads_today     INTEGER NOT NULL DEFAULT 0,
    total_uploads       INTEGER NOT NULL DEFAULT 0,
    total_downloads     INTEGER NOT NULL DEFAULT 0,
    file_credits        INTEGER NOT NULL DEFAULT 0,
    bytes_up_today      INTEGER NOT NULL DEFAULT 0,
    bytes_dn_today      INTEGER NOT NULL DEFAULT 0,
    total_bytes_up      INTEGER NOT NULL DEFAULT 0,
    total_bytes_dn      INTEGER NOT NULL DEFAULT 0,
    byte_credits        INTEGER NOT NULL DEFAULT 0,
    calls_today         INTEGER NOT NULL DEFAULT 0,
    time_credits_tenths INTEGER NOT NULL DEFAULT 0,
    balance_cents       INTEGER NOT NULL DEFAULT 0,
    pfile_points        INTEGER NOT NULL DEFAULT 0,
    network_credits     INTEGER NOT NULL DEFAULT 0,
    public_msg_count    INTEGER NOT NULL DEFAULT 0,
    private_msg_count   INTEGER NOT NULL DEFAULT 0,
    -- VDE EA: Preferences/Terminal
    logon_macro         TEXT    NOT NULL DEFAULT '',
    ctrl_e_macro        TEXT    NOT NULL DEFAULT '',
    ctrl_f_macro        TEXT    NOT NULL DEFAULT ''
);

-- Finger file answers.  One row per question per user.
-- question_num maps to nq0..nq4 (0=occupation, 1=equipment, 2=interests,
-- 3=how-found-BBS, 4=run-a-BBS).
CREATE TABLE IF NOT EXISTS finger_answers (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    question_num INTEGER NOT NULL,   -- 0..4
    answer       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, question_num)
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
    access_group INTEGER DEFAULT 0,
    is_hidden   INTEGER DEFAULT 0                -- 1 = hidden from WHO listing
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

CREATE TABLE IF NOT EXISTS access_groups (
    id              INTEGER PRIMARY KEY,
    title           TEXT    NOT NULL DEFAULT '',
    days_until_exp  INTEGER NOT NULL DEFAULT 0,
    exp_to_access   INTEGER NOT NULL DEFAULT 0,
    ag_privs        TEXT    NOT NULL DEFAULT '{}',  -- JSON dict of privilege values
    ag_limits       TEXT    NOT NULL DEFAULT '{}'   -- JSON dict of limit values
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
        # Migrations — safe to run on every startup, ALTER TABLE is no-op if column exists
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if 'term_type' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN term_type TEXT NOT NULL DEFAULT 'IBM'")
        if 'phone' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''")
        if 'dob' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN dob TEXT DEFAULT ''")
        if 'gender' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN gender TEXT DEFAULT ''")
        if 'ansi_level' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN ansi_level TEXT NOT NULL DEFAULT 'Simple'")
        if 'needs_lf' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN needs_lf INTEGER NOT NULL DEFAULT 0")
        if 'screen_width' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN screen_width INTEGER NOT NULL DEFAULT 80")
        if 'screen_height' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN screen_height INTEGER NOT NULL DEFAULT 24")
        if 'ansi_color' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN ansi_color INTEGER NOT NULL DEFAULT 1")
        if 'ansi_tabs' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN ansi_tabs INTEGER NOT NULL DEFAULT 0")
        if 'user_banner' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN user_banner TEXT NOT NULL DEFAULT ''")
        if 'data_phone' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN data_phone TEXT NOT NULL DEFAULT ''")
        if 'priv_flags' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN priv_flags INTEGER NOT NULL DEFAULT 0")
        # VDE Credits/Balances columns
        _credit_cols = [
            ('uploads_today',       'INTEGER NOT NULL DEFAULT 0'),
            ('downloads_today',     'INTEGER NOT NULL DEFAULT 0'),
            ('total_uploads',       'INTEGER NOT NULL DEFAULT 0'),
            ('total_downloads',     'INTEGER NOT NULL DEFAULT 0'),
            ('file_credits',        'INTEGER NOT NULL DEFAULT 0'),
            ('bytes_up_today',      'INTEGER NOT NULL DEFAULT 0'),
            ('bytes_dn_today',      'INTEGER NOT NULL DEFAULT 0'),
            ('total_bytes_up',      'INTEGER NOT NULL DEFAULT 0'),
            ('total_bytes_dn',      'INTEGER NOT NULL DEFAULT 0'),
            ('byte_credits',        'INTEGER NOT NULL DEFAULT 0'),
            ('calls_today',         'INTEGER NOT NULL DEFAULT 0'),
            ('time_credits_tenths', 'INTEGER NOT NULL DEFAULT 0'),
            ('balance_cents',       'INTEGER NOT NULL DEFAULT 0'),
            ('pfile_points',        'INTEGER NOT NULL DEFAULT 0'),
            ('network_credits',     'INTEGER NOT NULL DEFAULT 0'),
            ('public_msg_count',    'INTEGER NOT NULL DEFAULT 0'),
            ('private_msg_count',   'INTEGER NOT NULL DEFAULT 0'),
            # VDE Preferences/Terminal columns
            ('logon_macro',         "TEXT NOT NULL DEFAULT ''"),
            ('ctrl_e_macro',        "TEXT NOT NULL DEFAULT ''"),
            ('ctrl_f_macro',        "TEXT NOT NULL DEFAULT ''"),
            # EA Profile extra columns
            ('organization',        "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_def in _credit_cols:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
        # News tracking
        if 'last_news_read' not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_news_read TEXT DEFAULT NULL")
        # sessions migrations
        scols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if 'is_hidden' not in scols:
            conn.execute("ALTER TABLE sessions ADD COLUMN is_hidden INTEGER DEFAULT 0")
        # news_items migrations — add columns if table already exists
        ncols = [r[1] for r in conn.execute("PRAGMA table_info(news_items)").fetchall()]
        if ncols:
            if 'sort_order' not in ncols:
                conn.execute("ALTER TABLE news_items ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            if 'filename' not in ncols:
                conn.execute("ALTER TABLE news_items ADD COLUMN filename TEXT NOT NULL DEFAULT ''")
            if 'access_groups' not in ncols:
                conn.execute("ALTER TABLE news_items ADD COLUMN access_groups TEXT NOT NULL DEFAULT '0-31'")
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


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    """Look up a user by primary key. Returns Row or None."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id = ? AND is_deleted = 0",
            (user_id,)
        ).fetchone()


def change_password(user_id: int, new_password: str) -> None:
    """Hash and store a new password for the given user."""
    phash, psalt = _hash_password(new_password)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (phash, psalt, user_id),
        )
        conn.commit()


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


def update_term_type(user_id: int, term_type: str) -> None:
    """Save the terminal type the user selected at logon (IBM, AMIGA, CBM, SKY, NONE)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET term_type = ? WHERE id = ?",
            (term_type.upper(), user_id),
        )
        conn.commit()


def update_term_prefs(
    user_id:      int,
    ansi_level:   str = 'Simple',
    needs_lf:     bool = False,
    screen_width:  int = 80,
    screen_height: int = 24,
    ansi_color:   bool = True,
    ansi_tabs:    bool = False,
) -> None:
    """Persist all ET terminal preferences for a user."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE users SET
                ansi_level = ?, needs_lf = ?, screen_width = ?,
                screen_height = ?, ansi_color = ?, ansi_tabs = ?
               WHERE id = ?""",
            (ansi_level, int(needs_lf), screen_width,
             screen_height, int(ansi_color), int(ansi_tabs), user_id),
        )
        conn.commit()


def toggle_node_hidden(node_id: int) -> bool:
    """
    Toggle the is_hidden flag on a session row.
    Returns the NEW state: True = now hidden, False = now visible.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT is_hidden FROM sessions WHERE node_id = ?", (node_id,)
        ).fetchone()
        if cur is None:
            return False
        new_val = 0 if cur['is_hidden'] else 1
        conn.execute(
            "UPDATE sessions SET is_hidden = ? WHERE node_id = ?",
            (new_val, node_id),
        )
        conn.commit()
    return bool(new_val)


def update_user_profile(
    user_id: int,
    phone:     str = '',
    dob:       str = '',
    gender:    str = '',
    real_name: str | None = None,
) -> None:
    """
    Save personal profile fields (new-user registration or EP command).
    Pass real_name=None to leave it unchanged.
    """
    with get_connection() as conn:
        if real_name is not None:
            conn.execute(
                "UPDATE users SET phone = ?, dob = ?, gender = ?, real_name = ? WHERE id = ?",
                (phone, dob, gender.upper()[:1], real_name, user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET phone = ?, dob = ?, gender = ? WHERE id = ?",
                (phone, dob, gender.upper()[:1], user_id),
            )
        conn.commit()


def save_finger_answers(user_id: int, answers: dict[int, str]) -> None:
    """
    Save finger-file answers for a user.

    `answers` is a dict mapping question_num (0-4) to answer text.
    Existing answers are replaced (INSERT OR REPLACE).

    question_num mapping:
      0 — occupation
      1 — computer equipment
      2 — interests / hobbies
      3 — how did you find this BBS?
      4 — do you run a BBS?
    """
    with get_connection() as conn:
        for qnum, answer in answers.items():
            conn.execute(
                """INSERT OR REPLACE INTO finger_answers (user_id, question_num, answer)
                   VALUES (?, ?, ?)""",
                (user_id, int(qnum), (answer or '').strip()),
            )
        conn.commit()


def get_finger_answers(user_id: int) -> dict[int, str]:
    """Return finger-file answers as {question_num: answer_text}."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT question_num, answer FROM finger_answers WHERE user_id = ? ORDER BY question_num",
            (user_id,),
        ).fetchall()
    return {r['question_num']: r['answer'] for r in rows}


# --------------------------------------------------------------------------
# Sysop user-management helpers
# --------------------------------------------------------------------------

def get_all_users(pattern: str = '') -> list:
    """
    Return all non-deleted users ordered by handle.
    If `pattern` is given, restrict to handles that contain it (case-insensitive).
    """
    with get_connection() as conn:
        if pattern:
            return conn.execute(
                """SELECT * FROM users WHERE is_deleted = 0
                   AND handle LIKE ? COLLATE NOCASE
                   ORDER BY handle""",
                (f'%{pattern}%',),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM users WHERE is_deleted = 0 ORDER BY handle"
        ).fetchall()


def update_user_admin(
    user_id:   int,
    ag:        int | None = None,
    location:  str | None = None,
    email:     str | None = None,
    notes:     str | None = None,
    validated: int | None = None,
) -> None:
    """
    Sysop-only update of privileged user fields.
    Pass None for any field to leave it unchanged.
    """
    fields, vals = [], []
    if ag        is not None: fields.append('access_group = ?');  vals.append(max(0, min(31, ag)))
    if location  is not None: fields.append('location = ?');      vals.append(location)
    if email     is not None: fields.append('email = ?');         vals.append(email)
    if notes     is not None: fields.append('notes = ?');         vals.append(notes)
    if validated is not None: fields.append('is_validated = ?');  vals.append(int(bool(validated)))
    if not fields:
        return
    vals.append(user_id)
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", vals)
        conn.commit()


def update_user_vde(user_id: int, changes: dict) -> None:
    """
    Save a dict of changed field values from the VDE EA form.

    Handles all profile + privilege fields.  Special keys:
      __kill__   — soft-deletes the account (set to True)
      __pwd__    — new plaintext password string
      __priv_N__ — individual privilege bit fields (packed into priv_flags)
    """
    if not changes:
        return

    # Handle kill first
    if changes.get('__kill__'):
        soft_delete_user(user_id)
        return

    # Handle password change
    if '__pwd__' in changes and changes['__pwd__']:
        change_password(user_id, str(changes['__pwd__']))

    # Collect privilege bit changes and pack into priv_flags
    priv_updates = {k: v for k, v in changes.items() if k.startswith('__priv_')}
    if priv_updates:
        # Load current priv_flags
        with get_connection() as conn:
            row = conn.execute(
                "SELECT priv_flags FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        flags = (row['priv_flags'] or 0) if row else 0
        for key, val in priv_updates.items():
            try:
                bit = int(key.split('_')[2])  # __priv_N__
                if int(val or 0):
                    flags |= (1 << bit)
                else:
                    flags &= ~(1 << bit)
            except (IndexError, ValueError):
                pass
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET priv_flags = ? WHERE id = ?", (flags, user_id)
            )
            conn.commit()

    # Map direct field names to DB columns
    FIELD_MAP = {
        # Profile
        'handle':               'handle',
        'real_name':            'real_name',
        'organization':         'organization',
        'notes':                'notes',
        'user_banner':          'user_banner',
        'location':             'location',
        'email':                'email',
        'dob':                  'dob',
        'gender':               'gender',
        'data_phone':           'data_phone',
        'phone':                'phone',
        'access_group':         'access_group',
        # Credits/Balances
        'uploads_today':        'uploads_today',
        'downloads_today':      'downloads_today',
        'total_uploads':        'total_uploads',
        'total_downloads':      'total_downloads',
        'file_credits':         'file_credits',
        'bytes_up_today':       'bytes_up_today',
        'bytes_dn_today':       'bytes_dn_today',
        'total_bytes_up':       'total_bytes_up',
        'total_bytes_dn':       'total_bytes_dn',
        'byte_credits':         'byte_credits',
        'calls_today':          'calls_today',
        'call_count':           'call_count',
        'time_today':           'time_today',
        'time_credits_tenths':  'time_credits_tenths',
        'balance_cents':        'balance_cents',
        'pfile_points':         'pfile_points',
        'network_credits':      'network_credits',
        'public_msg_count':     'public_msg_count',
        'private_msg_count':    'private_msg_count',
        # Preferences/Terminal
        'logon_macro':          'logon_macro',
        'ctrl_e_macro':         'ctrl_e_macro',
        'ctrl_f_macro':         'ctrl_f_macro',
        'screen_width':         'screen_width',
        'screen_height':        'screen_height',
        'ansi_color':           'ansi_color',
        'ansi_tabs':            'ansi_tabs',
        'needs_lf':             'needs_lf',
        'ansi_level':           'ansi_level',
    }

    # Integer-bounded columns and their ranges
    _INT_BOUNDS = {
        'access_group':   (0, 31),
        'screen_width':   (22, 255),
        'screen_height':  (5, 50),
        'calls_today':    (0, 32767),
        'call_count':     (0, 999999999),
        'time_today':     (0, 14400),
        'ansi_color':     (0, 1),
        'ansi_tabs':      (0, 1),
        'needs_lf':       (0, 1),
    }

    fields_sql, vals = [], []
    for key, col in FIELD_MAP.items():
        if key in changes:
            val = changes[key]
            if col in _INT_BOUNDS:
                lo, hi = _INT_BOUNDS[col]
                try:
                    val = max(lo, min(hi, int(val)))
                except (ValueError, TypeError):
                    continue
            fields_sql.append(f'{col} = ?')
            vals.append(val)

    if fields_sql:
        vals.append(user_id)
        with get_connection() as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(fields_sql)} WHERE id = ?", vals
            )
            conn.commit()


def update_subboard_vde(board_id: int, changes: dict) -> None:
    """Save VDE EB changes for a subboard."""
    if not changes:
        return

    FIELD_MAP = {
        'name':        'name',
        'description': 'description',
        'read_ag':     'read_ag',
        'write_ag':    'write_ag',
    }

    fields_sql, vals = [], []
    for key, col in FIELD_MAP.items():
        if key in changes:
            val = changes[key]
            if col in ('read_ag', 'write_ag'):
                try:
                    val = max(0, min(31, int(val)))
                except (ValueError, TypeError):
                    continue
            fields_sql.append(f'{col} = ?')
            vals.append(val)

    if fields_sql:
        vals.append(board_id)
        from server import msgbase
        msgbase.update_subboard_raw(board_id, dict(zip(
            [f.split(' =')[0] for f in fields_sql], vals
        )))


def soft_delete_user(user_id: int) -> bool:
    """Deactivate a user account (soft delete). Returns True if found."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET is_deleted = 1 WHERE id = ? AND is_deleted = 0",
            (user_id,),
        )
        conn.commit()
    return cur.rowcount > 0


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


def get_active_sessions(include_hidden: bool = False) -> list:
    """Return all current session rows, ordered by node_id.
    Hidden sessions are excluded unless include_hidden=True (sysop WHO)."""
    with get_connection() as conn:
        if include_hidden:
            return conn.execute(
                "SELECT * FROM sessions ORDER BY node_id"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM sessions WHERE is_hidden = 0 ORDER BY node_id"
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


# --------------------------------------------------------------------------
# Access Groups
# --------------------------------------------------------------------------

import json as _json


def get_access_group(ag_id: int) -> dict | None:
    """Return a single access group by id, with ag_privs/ag_limits decoded from JSON."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM access_groups WHERE id = ?", (ag_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['ag_privs'] = _json.loads(d.get('ag_privs') or '{}')
    except Exception:
        d['ag_privs'] = {}
    try:
        d['ag_limits'] = _json.loads(d.get('ag_limits') or '{}')
    except Exception:
        d['ag_limits'] = {}
    return d


def get_or_create_access_group(ag_id: int) -> dict:
    """Get an AG, creating a default row if it doesn't exist yet."""
    ag = get_access_group(ag_id)
    if ag:
        return ag
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO access_groups (id, title) VALUES (?, ?)",
            (ag_id, f'Access Group {ag_id}')
        )
        conn.commit()
    return get_access_group(ag_id) or {'id': ag_id, 'title': '', 'ag_privs': {}, 'ag_limits': {}}


def update_access_group(ag_id: int, **kwargs) -> bool:
    """
    Update access group fields.  Accepts:
      title, days_until_exp, exp_to_access  — direct columns
      ag_privs  — dict, serialised to JSON
      ag_limits — dict, serialised to JSON
    """
    allowed = {'title', 'days_until_exp', 'exp_to_access', 'ag_privs', 'ag_limits'}
    sets, vals = [], []
    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if k in ('ag_privs', 'ag_limits'):
            v = _json.dumps(v) if isinstance(v, dict) else str(v)
        sets.append(f'{k} = ?')
        vals.append(v)
    if not sets:
        return False
    vals.append(ag_id)
    try:
        with get_connection() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO access_groups (id) VALUES (?)", (ag_id,)
            )
            conn.execute(
                f"UPDATE access_groups SET {', '.join(sets)} WHERE id = ?", vals
            )
            conn.commit()
        return True
    except Exception as exc:
        log.warning("update_access_group failed: %s", exc)
        return False

