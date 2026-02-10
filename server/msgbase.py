"""
server/msgbase.py — Message Base Database Layer

Handles all SQLite operations for subboards and messages.

DATA MODEL (CNet "post and response" format):
─────────────────────────────────────────────
Only root posts appear in the thread list.  Responses are attached to
a root post and read together with it.  This matches CNet's "partially
threaded" design described in Chapter 9: not fully threaded (responses
to responses), not unthreaded (no links at all) — just posts with
ordered responses.

  subboards   — one row per message board
  messages    — posts and responses; thread_id = root post's id
  board_visits — tracks when each user last visited each board (for NEW)

THREADING:
  Root post:   thread_id = its own id (set after insert), parent_id = NULL
  Response:    thread_id = root post id, parent_id = message being replied to
"""

import sqlite3
import logging
from datetime import datetime, timezone

from server.database import get_connection

log = logging.getLogger("anet.msgbase")


# ─────────────────────────────────────────────────────────────────────────────
# Schema initialisation (called from database.init_db)
# ─────────────────────────────────────────────────────────────────────────────

MSGBASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS subboards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    read_ag      INTEGER NOT NULL DEFAULT 0,
    write_ag     INTEGER NOT NULL DEFAULT 5,
    created_by   TEXT    NOT NULL DEFAULT 'SysOp',
    created_at   TEXT    NOT NULL,
    post_count   INTEGER NOT NULL DEFAULT 0,
    last_post_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    subboard_id    INTEGER NOT NULL REFERENCES subboards(id),
    thread_id      INTEGER,           -- NULL until set; root = own id
    parent_id      INTEGER,           -- direct parent (NULL for root posts)
    author_id      INTEGER NOT NULL REFERENCES users(id),
    author_handle  TEXT    NOT NULL,
    subject        TEXT    NOT NULL,
    body           TEXT    NOT NULL,
    posted_at      TEXT    NOT NULL,
    is_deleted     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages (subboard_id, thread_id, posted_at);

CREATE TABLE IF NOT EXISTS board_visits (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    subboard_id INTEGER NOT NULL REFERENCES subboards(id),
    visited_at  TEXT    NOT NULL,
    PRIMARY KEY (user_id, subboard_id)
);
"""


def init_message_tables() -> None:
    """Create message base tables if they don't exist.  Safe to call repeatedly."""
    with get_connection() as conn:
        conn.executescript(MSGBASE_SCHEMA)
        conn.commit()
    log.debug("Message base tables ready.")


# ─────────────────────────────────────────────────────────────────────────────
# Subboard management
# ─────────────────────────────────────────────────────────────────────────────

def create_subboard(name: str, description: str,
                    read_ag: int, write_ag: int,
                    created_by: str) -> int:
    """
    Create a new subboard.  Returns the new subboard ID.
    """
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO subboards (name, description, read_ag, write_ag,
               created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name.strip(), description.strip(), read_ag, write_ag, created_by, now),
        )
        conn.commit()
        return cur.lastrowid


def get_subboard(subboard_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM subboards WHERE id = ?", (subboard_id,)
        ).fetchone()


def get_all_subboards() -> list[sqlite3.Row]:
    """Return all subboards ordered by id."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM subboards ORDER BY id"
        ).fetchall()


def get_subboards_for_user(user_ag: int) -> list[sqlite3.Row]:
    """Return subboards where read_ag <= user_ag (boards the user can see)."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM subboards WHERE read_ag <= ? ORDER BY id",
            (user_ag,),
        ).fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# Thread / message queries
# ─────────────────────────────────────────────────────────────────────────────

def get_thread_list(subboard_id: int) -> list[sqlite3.Row]:
    """
    Return root posts for a subboard, sorted newest-activity-first.
    Each row includes: id, subject, author_handle, posted_at,
    response_count, last_activity.

    A root post has thread_id = id (set after first insert).
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                m.id,
                m.subject,
                m.author_handle,
                m.posted_at,
                COUNT(r.id)                          AS response_count,
                COALESCE(MAX(r.posted_at), m.posted_at) AS last_activity
            FROM messages m
            LEFT JOIN messages r
                ON  r.thread_id   = m.id
                AND r.id         != m.id
                AND r.is_deleted  = 0
            WHERE m.subboard_id = ?
              AND m.thread_id   = m.id   -- root posts only
              AND m.is_deleted  = 0
            GROUP BY m.id
            ORDER BY last_activity DESC
            """,
            (subboard_id,),
        ).fetchall()


def get_thread_list_since(subboard_id: int, since: str) -> list[sqlite3.Row]:
    """Thread list filtered to threads with any activity after `since`."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT
                m.id,
                m.subject,
                m.author_handle,
                m.posted_at,
                COUNT(r.id)                          AS response_count,
                COALESCE(MAX(r.posted_at), m.posted_at) AS last_activity
            FROM messages m
            LEFT JOIN messages r
                ON  r.thread_id   = m.id
                AND r.id         != m.id
                AND r.is_deleted  = 0
            WHERE m.subboard_id = ?
              AND m.thread_id   = m.id
              AND m.is_deleted  = 0
            GROUP BY m.id
            HAVING last_activity > ?
            ORDER BY last_activity DESC
            """,
            (subboard_id, since),
        ).fetchall()


def get_thread_messages(thread_id: int) -> list[sqlite3.Row]:
    """All messages in a thread, root first then responses by posted_at."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM messages
            WHERE thread_id = ? AND is_deleted = 0
            ORDER BY posted_at ASC, id ASC
            """,
            (thread_id,),
        ).fetchall()


def get_message(message_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Posting
# ─────────────────────────────────────────────────────────────────────────────

def post_new_thread(subboard_id: int, author_id: int, author_handle: str,
                    subject: str, body: str) -> int:
    """
    Post a new root message (starts a thread).
    Returns the new message ID (which also becomes the thread_id).
    """
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (subboard_id, thread_id, parent_id, author_id, author_handle,
                subject, body, posted_at)
               VALUES (?, NULL, NULL, ?, ?, ?, ?, ?)""",
            (subboard_id, author_id, author_handle, subject.strip(), body, now),
        )
        msg_id = cur.lastrowid
        # Root post's thread_id = its own id
        conn.execute(
            "UPDATE messages SET thread_id = ? WHERE id = ?",
            (msg_id, msg_id),
        )
        conn.execute(
            """UPDATE subboards
               SET post_count   = post_count + 1,
                   last_post_at = ?
               WHERE id = ?""",
            (now, subboard_id),
        )
        conn.commit()
    log.info("New thread #%d in board #%d by %s", msg_id, subboard_id, author_handle)
    return msg_id


def post_response(subboard_id: int, thread_id: int, parent_id: int,
                  author_id: int, author_handle: str,
                  subject: str, body: str) -> int:
    """
    Post a response to an existing thread.
    Returns the new message ID.
    """
    now = _now()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (subboard_id, thread_id, parent_id, author_id, author_handle,
                subject, body, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (subboard_id, thread_id, parent_id,
             author_id, author_handle, subject.strip(), body, now),
        )
        msg_id = cur.lastrowid
        conn.execute(
            "UPDATE subboards SET last_post_at = ? WHERE id = ?",
            (now, subboard_id),
        )
        conn.commit()
    log.info("Response #%d to thread #%d by %s", msg_id, thread_id, author_handle)
    return msg_id


# ─────────────────────────────────────────────────────────────────────────────
# Deletion
# ─────────────────────────────────────────────────────────────────────────────

def delete_message(message_id: int, requesting_user_id: int,
                   requesting_user_ag: int) -> tuple[bool, str]:
    """
    Soft-delete a message.  Returns (success, reason).
    SysOps (AG 31) may delete any message.
    Others may only delete their own messages.
    """
    msg = get_message(message_id)
    if msg is None:
        return False, "Message not found."
    if msg["is_deleted"]:
        return False, "Already deleted."

    is_sysop = requesting_user_ag >= 31
    is_owner = msg["author_id"] == requesting_user_id

    if not (is_sysop or is_owner):
        return False, "You may only delete your own messages."

    with get_connection() as conn:
        conn.execute(
            "UPDATE messages SET is_deleted = 1 WHERE id = ?",
            (message_id,),
        )
        conn.commit()
    return True, "Message deleted."


# ─────────────────────────────────────────────────────────────────────────────
# Board visit tracking (for NEW messages)
# ─────────────────────────────────────────────────────────────────────────────

def record_visit(user_id: int, subboard_id: int) -> None:
    """Record that a user visited a board right now."""
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO board_visits (user_id, subboard_id, visited_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, subboard_id)
               DO UPDATE SET visited_at = excluded.visited_at""",
            (user_id, subboard_id, now),
        )
        conn.commit()


def get_last_visit(user_id: int, subboard_id: int) -> str | None:
    """Return ISO timestamp of last visit, or None if never visited."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT visited_at FROM board_visits WHERE user_id=? AND subboard_id=?",
            (user_id, subboard_id),
        ).fetchone()
    return row["visited_at"] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
