"""
server/news.py — BBS News / Bulletins subsystem

CNet architecture: SQLite manages the index (metadata, flags, order),
the filesystem holds the content files (data/news/*.news).

Each news item has:
  - A description (shown in the list)
  - A filename in data/news/ (holds the actual text/ANSI content)
  - A post_date  — item is hidden until this date is reached
  - A purge_date — item is auto-killed after this date
  - Standard flags: access_groups, item_disabled, disable_mci, etc.

The old 'body' column is kept as a migration fallback for existing items
that were created before file-based storage was added.

DB TABLE: news_items
  id, sort_order, title, body (legacy), filename, access_groups,
  item_type, flags_required, item_use_rate, post_date, purge_date,
  item_disabled, disable_mci, disable_more, disable_wordwrap,
  disable_sysop_mci, delete_when_purged,
  posted_at, posted_by_id, posted_by_handle, is_active
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from server.database import get_connection

log = logging.getLogger("anet.news")

# Directory where news content files are stored
NEWS_DIR = Path(getattr(Config, 'NEWS_DIR', 'data/news'))


def _news_dir() -> Path:
    """Return the news directory, creating it if needed."""
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    return NEWS_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order        INTEGER NOT NULL DEFAULT 0,
    title             TEXT    NOT NULL,
    body              TEXT    NOT NULL DEFAULT '',
    filename          TEXT    NOT NULL DEFAULT '',
    access_groups     TEXT    NOT NULL DEFAULT '0-31',
    item_type         TEXT    NOT NULL DEFAULT 'Text',
    flags_required    TEXT    NOT NULL DEFAULT '',
    item_use_rate     INTEGER NOT NULL DEFAULT 0,
    post_date         TEXT    DEFAULT NULL,
    purge_date        TEXT    DEFAULT NULL,
    item_disabled     INTEGER NOT NULL DEFAULT 0,
    disable_mci       INTEGER NOT NULL DEFAULT 0,
    disable_more      INTEGER NOT NULL DEFAULT 0,
    disable_wordwrap  INTEGER NOT NULL DEFAULT 0,
    disable_sysop_mci INTEGER NOT NULL DEFAULT 0,
    delete_when_purged INTEGER NOT NULL DEFAULT 0,
    posted_at         TEXT    NOT NULL,
    posted_by_id      INTEGER REFERENCES users(id),
    posted_by_handle  TEXT    DEFAULT '',
    is_active         INTEGER NOT NULL DEFAULT 1
);
"""

_MIGRATIONS = [
    ('item_type',          "TEXT NOT NULL DEFAULT 'Text'"),
    ('flags_required',     "TEXT NOT NULL DEFAULT ''"),
    ('item_use_rate',      'INTEGER NOT NULL DEFAULT 0'),
    ('post_date',          'TEXT DEFAULT NULL'),
    ('purge_date',         'TEXT DEFAULT NULL'),
    ('item_disabled',      'INTEGER NOT NULL DEFAULT 0'),
    ('disable_mci',        'INTEGER NOT NULL DEFAULT 0'),
    ('disable_more',       'INTEGER NOT NULL DEFAULT 0'),
    ('disable_wordwrap',   'INTEGER NOT NULL DEFAULT 0'),
    ('disable_sysop_mci',  'INTEGER NOT NULL DEFAULT 0'),
    ('delete_when_purged', 'INTEGER NOT NULL DEFAULT 0'),
    # columns already present from previous session:
    ('sort_order',         'INTEGER NOT NULL DEFAULT 0'),
    ('filename',           "TEXT NOT NULL DEFAULT ''"),
    ('access_groups',      "TEXT NOT NULL DEFAULT '0-31'"),
]


def init_news_tables() -> None:
    """Create / migrate news tables.  Safe to call on every startup."""
    with get_connection() as conn:
        conn.executescript(_SCHEMA)
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(news_items)").fetchall()]
        for col, defn in _MIGRATIONS:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE news_items ADD COLUMN {col} {defn}")
        conn.commit()
    _news_dir()   # ensure directory exists
    log.debug("News tables ready.")


# ─────────────────────────────────────────────────────────────────────────────
# File helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_filename(item_id: int) -> str:
    """Generate a unique filename for a new news item."""
    ts = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    return f"{ts}_{item_id}.news"


def write_content(filename: str, body: str) -> None:
    """Write body text to data/news/<filename>."""
    path = _news_dir() / filename
    path.write_text(body, encoding='latin-1', errors='replace')


def read_content(row) -> str:
    """
    Read content for a news item row.
    Tries the file first; falls back to the DB body column.
    """
    filename = row['filename'] if row['filename'] else ''
    if filename:
        path = _news_dir() / filename
        try:
            return path.read_text(encoding='latin-1', errors='replace')
        except (OSError, IOError):
            pass
    # Fallback: legacy DB body
    return row['body'] or ''


def delete_content_file(filename: str) -> None:
    """Remove a news content file from disk (used by delete_when_purged)."""
    if not filename:
        return
    try:
        (_news_dir() / filename).unlink(missing_ok=True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Date helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_visible_now(row) -> bool:
    """
    Return True if item should be visible to users right now.
    post_date: if set and in the future, item is hidden.
    item_disabled: if 1, item is hidden.
    """
    if row['item_disabled']:
        return False
    post = row['post_date']
    if post:
        try:
            if post > _now_iso():
                return False
        except Exception:
            pass
    return True


def _is_purge_due(row) -> bool:
    """Return True if the item's purge date has passed."""
    purge = row['purge_date']
    if not purge:
        return False
    try:
        return purge <= _now_iso()
    except Exception:
        return False


def _auto_purge(items: list) -> list:
    """
    Check each item for an overdue purge date.
    Kills expired items (and optionally deletes content file).
    Returns only the still-active items.
    """
    live = []
    for item in items:
        if _is_purge_due(item):
            _do_kill(item['id'],
                     delete_file=bool(item['delete_when_purged']),
                     filename=item['filename'])
            log.info("News item #%d auto-purged: %s", item['id'], item['title'])
        else:
            live.append(item)
    return live


def _do_kill(item_id: int, delete_file: bool = False,
             filename: str = '') -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE news_items SET is_active = 0 WHERE id = ?", (item_id,))
        conn.commit()
    if delete_file and filename:
        delete_content_file(filename)


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def get_all_items(sysop: bool = False) -> list:
    """
    Return active news items in display order.
    Non-sysops only see items that pass _is_visible_now().
    Auto-purges expired items.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM news_items
               WHERE is_active = 1
               ORDER BY sort_order ASC, posted_at ASC"""
        ).fetchall()
    rows = _auto_purge(rows)
    if sysop:
        return rows
    return [r for r in rows if _is_visible_now(r)]


def get_item_by_id(item_id: int):
    """Return a single news item row by DB id, or None."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM news_items WHERE id = ? AND is_active = 1",
            (item_id,)
        ).fetchone()


def get_new_since(since_iso: str | None, sysop: bool = False) -> list:
    """
    Return active items posted after since_iso.
    If since_iso is None, returns all visible items.
    Respects post_date and item_disabled for non-sysops.
    """
    with get_connection() as conn:
        if since_iso is None:
            rows = conn.execute(
                """SELECT * FROM news_items WHERE is_active = 1
                   ORDER BY sort_order ASC, posted_at ASC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM news_items
                   WHERE is_active = 1 AND posted_at > ?
                   ORDER BY sort_order ASC, posted_at ASC""",
                (since_iso,)
            ).fetchall()
    rows = _auto_purge(rows)
    if sysop:
        return rows
    return [r for r in rows if _is_visible_now(r)]


def get_next_sort_order() -> int:
    """Return the next available sort_order value."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(sort_order) FROM news_items WHERE is_active = 1"
        ).fetchone()
        return (row[0] or 0) + 1


# ─────────────────────────────────────────────────────────────────────────────
# Write
# ─────────────────────────────────────────────────────────────────────────────

def post_item(
    title: str,
    body: str,
    posted_by_id: int,
    posted_by_handle: str,
) -> int:
    """
    Post a new news item.
    Writes body to a file in data/news/, stores filename in DB.
    Returns the new item id.
    """
    now   = _now_iso()
    order = get_next_sort_order()

    # Insert first to get the id, then name the file after it
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO news_items
               (sort_order, title, body, filename, posted_at,
                posted_by_id, posted_by_handle, is_active)
               VALUES (?, ?, '', '', ?, ?, ?, 1)""",
            (order, title.strip(), now, posted_by_id, posted_by_handle),
        )
        item_id = cur.lastrowid
        filename = _make_filename(item_id)
        write_content(filename, body)
        conn.execute(
            "UPDATE news_items SET filename = ? WHERE id = ?",
            (filename, item_id)
        )
        conn.commit()

    log.info("News item #%d posted by %s: %s → %s",
             item_id, posted_by_handle, title, filename)
    return item_id


def update_item_vde(item_id: int, changes: dict) -> None:
    """
    Apply VDE field changes to a news item.
    Only updates columns that exist in the changes dict.
    """
    allowed = {
        'title', 'filename', 'access_groups', 'item_type',
        'flags_required', 'item_use_rate', 'post_date', 'purge_date',
        'item_disabled', 'disable_mci', 'disable_more', 'disable_wordwrap',
        'disable_sysop_mci', 'delete_when_purged',
    }
    fields = {k: v for k, v in changes.items() if k in allowed}
    if not fields:
        return
    cols   = ', '.join(f"{k} = ?" for k in fields)
    vals   = list(fields.values()) + [item_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE news_items SET {cols} WHERE id = ?", vals)
        conn.commit()


def kill_item(item_id: int, delete_file: bool = False) -> bool:
    """Soft-delete a news item.  Returns True if found."""
    item = get_item_by_id(item_id)
    if not item:
        return False
    _do_kill(item_id, delete_file=delete_file, filename=item['filename'])
    return True


# ─────────────────────────────────────────────────────────────────────────────
# User tracking
# ─────────────────────────────────────────────────────────────────────────────

def update_last_news_read(user_id: int) -> None:
    """Stamp the user's last_news_read with the current UTC time."""
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET last_news_read = ? WHERE id = ?",
                (_now_iso(), user_id)
            )
            conn.commit()
    except Exception:
        pass


def get_last_news_read(user_id: int) -> str | None:
    """Return the user's last_news_read ISO string, or None."""
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT last_news_read FROM users WHERE id = ?",
                (user_id,)
            ).fetchone()
        return row['last_news_read'] if row else None
    except Exception:
        return None
