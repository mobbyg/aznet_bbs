"""
server/mail.py — Private Mail System

Handles user-to-user private mail on ANet BBS.

DB TABLE: mail_items
─────────────────────
  id               INTEGER PK
  from_id          INTEGER → users(id)
  from_handle      TEXT
  to_id            INTEGER → users(id)
  to_handle        TEXT
  subject          TEXT
  body             TEXT
  sent_at          TEXT   (ISO 8601)
  read_at          TEXT   (NULL = unread)
  reply_to_id      INTEGER → mail_items(id)   (NULL = original message)
  is_deleted       INTEGER DEFAULT 0           (soft-delete by recipient)

BBSTEXT RECORDS USED
─────────────────────
  200  — "No mail in inbox."
  218  — "Mail-Read/INBOX: ?,Quit,Scan,Reply,Rescan [ENTER=next]:"
  224  — "There are %d new mail item(s) in %s, %d marked URGENT.  Read now [N/y]?"
  225  — "There are %d new mail item(s) in %s.  Read now [N/y]?"
  226  — "There are %d old mail item(s) in %s.  Use MR to read."
  245  — "%c%c%c %3d %s %-20.20s %s"   (mail list row)
  246  — "   From: %s"
  249  — "    Date: %s"
  250  — "Subject: %s"
  253  — "    Item: %d (of %d)"
  708  — "Re:"
  709  — "Re: %-.75s"

SYSTEXT FILES USED
───────────────────
  mail       — mail area command help
  mail-read  — mail-read command help
  nmail      — body of the welcome letter sent to every new user
"""

from __future__ import annotations

import logging
from datetime import datetime

from server.database import get_connection

log = logging.getLogger("anet.mail")

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id          INTEGER REFERENCES users(id),
    from_handle      TEXT    NOT NULL DEFAULT '',
    to_id            INTEGER REFERENCES users(id),
    to_handle        TEXT    NOT NULL DEFAULT '',
    subject          TEXT    NOT NULL DEFAULT '(no subject)',
    body             TEXT    NOT NULL DEFAULT '',
    sent_at          TEXT    NOT NULL,
    read_at          TEXT,
    reply_to_id      INTEGER REFERENCES mail_items(id),
    is_deleted       INTEGER NOT NULL DEFAULT 0
);
"""


def init_mail_tables() -> None:
    """Create mail tables if they don't exist. Safe to call on every startup."""
    with get_connection() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    log.debug("Mail tables ready.")


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def get_inbox(user_id: int) -> list:
    """Return all non-deleted inbox messages for a user, newest first."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT * FROM mail_items
               WHERE to_id = ? AND is_deleted = 0
               ORDER BY sent_at DESC""",
            (user_id,),
        ).fetchall()


def get_unread_count(user_id: int) -> int:
    """Return count of unread (never-opened) messages for a user."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM mail_items
               WHERE to_id = ? AND is_deleted = 0 AND read_at IS NULL""",
            (user_id,),
        ).fetchone()
    return row[0] if row else 0


def get_mail_item(mail_id: int):
    """Return a single mail item row or None."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM mail_items WHERE id = ?", (mail_id,)
        ).fetchone()


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────

def send_mail(
    from_id: int,
    from_handle: str,
    to_id: int,
    to_handle: str,
    subject: str,
    body: str,
    reply_to_id: int | None = None,
) -> int:
    """
    Send a mail item.  Returns the new mail_items id.
    """
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO mail_items
               (from_id, from_handle, to_id, to_handle, subject, body,
                sent_at, reply_to_id, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (from_id, from_handle, to_id, to_handle,
             subject.strip(), body, now, reply_to_id),
        )
        conn.commit()
    log.info("Mail #%d: %s → %s: %s", cur.lastrowid, from_handle, to_handle, subject[:40])
    return cur.lastrowid


def mark_read(mail_id: int) -> None:
    """Record that a mail item has been read (first-open timestamp)."""
    now = datetime.utcnow().isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE mail_items SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (now, mail_id),
        )
        conn.commit()


def kill_mail(mail_id: int, user_id: int) -> bool:
    """
    Soft-delete a mail item for the recipient.
    Returns True if the item was found and deleted.
    """
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE mail_items SET is_deleted = 1
               WHERE id = ? AND to_id = ? AND is_deleted = 0""",
            (mail_id, user_id),
        )
        conn.commit()
    return cur.rowcount > 0
