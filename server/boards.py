"""
server/boards.py — Message Base Navigation

Handles the interactive board-list → board → thread → read → respond
navigation flow, called from session.py.

Flow:
  BoardArea.run()
      └── _board_list()         show numbered list of accessible boards
              └── _enter_board(board)   board-level loop
                      ├── _scan_board()     scan for new items (S command)
                      ├── _thread_list()    show all threads (L command)
                      ├── _read_thread()    read a thread + respond/pass
                      └── _post_new()       start a new thread (N command)

BBSTEXT RECORDS USED
─────────────────────
  rec  66  — "Reading message"
  rec  76  — "Access denied."
  rec 325  — "Scan, Quit, item# or ?=Help"
  rec 327  — "Browse, Scan, Read, Post, Quit, ?=Menu"
  rec 378  — "Read new items now (Yes,Browse,Scan,[N/y])?"
  rec 440  — "New responses:"
  rec 453  — "There have been %d responses."
  rec 454  — "There has been 1 response."
  rec 474  — 'Publicly respond to "%s":'
  rec 476  — "Responding"
  rec 477  — "Response filing..."
  rec 521  — "This subboard is empty."
  rec 666  — "Enter item#, Scan, Quit, ?=Menu"
  rec 1044 — "Item %d"
  rec 1045 — "; response %d of %d"

SYSTEXT FILES USED
───────────────────
  base     — board command help
  base0    — board list help
  mess     — message reading help
  post     — posting help
"""

import asyncio
import logging
from datetime import datetime

from server import msgbase
from server.editor import LineEditor

log = logging.getLogger("anet.boards")

# ANSI colour shortcuts used in hand-built subboard headers
_C_WHITE  = b'\x1b[1;37m'
_C_CYAN   = b'\x1b[36m'
_C_GREEN  = b'\x1b[1;32m'
_C_YELLOW = b'\x1b[1;33m'
_C_RESET  = b'\x1b[0m'
_HR       = b'\xc4' * 70   # CP437 horizontal rule


# ─────────────────────────────────────────────────────────────────────────────
# Date helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "---"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d-%b-%y")
    except ValueError:
        return iso[:10]


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "---"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d-%b-%y %H:%M")
    except ValueError:
        return iso[:16]


# ─────────────────────────────────────────────────────────────────────────────
# BoardArea
# ─────────────────────────────────────────────────────────────────────────────

class BoardArea:
    """
    Encapsulates all board/thread navigation for one session.
    Created fresh each time the user enters the message base.
    """

    def __init__(self, session):
        self._s = session

    def _bb(self, rec: int, subs: dict | None = None) -> bytes:
        """Shortcut: render a bbstext record."""
        return self._s.bbstext.render(rec, subs)

    def _st(self, filename: str) -> bytes:
        """Shortcut: render a systext file."""
        from server.systext import SystextFile
        vars_ = SystextFile.make_variables(handle=self._s.handle)
        return self._s.systext.render(filename, vars_)

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._board_list()

    # ── Board list ────────────────────────────────────────────────────────────

    async def _board_list(self) -> None:
        s = self._s
        while True:
            boards = msgbase.get_subboards_for_user(s.access_group)
            if not boards:
                await s.send(self._bb(521))   # "This subboard is empty."
                return

            await self._print_board_list(boards)

            # rec 325: "\r\nScan, Quit, item# or ?=Help\r\n"
            await s.send(self._bb(325))
            raw = await s.readline_with_timeout()
            if raw is None:
                return

            cmd = raw.strip().upper()

            if not cmd or cmd in ('Q', 'QUIT'):
                return

            if cmd == '?':
                await s.send(self._st('base0'))
                continue

            if cmd in ('S', 'SCAN'):
                await self._scan_all_boards(boards)
                continue

            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(boards):
                    await self._enter_board(boards[idx])
                    continue

            await s.send_line(b"  Invalid selection.\r\n")

    async def _print_board_list(self, boards) -> None:
        s = self._s
        await s.send(b"\r\n")
        await s.send(
            _C_GREEN +
            b"  #   Name                           R/W   Posts  Last Post" +
            _C_RESET + b"\r\n"
        )
        await s.send(b"  " + _HR + b"\r\n")

        for i, b in enumerate(boards, 1):
            can_write = "RW" if b["write_ag"] <= s.access_group else "R "
            last  = _fmt_date(b["last_post_at"])
            name  = (b["name"] or "")[:30].ljust(30)
            posts = str(b["post_count"]).ljust(6)

            # Highlight boards with new activity since last visit
            last_visit = msgbase.get_last_visit(s.user_id, b["id"])
            has_new = (last_visit and b["last_post_at"] and
                       b["last_post_at"] > last_visit)
            color = _C_YELLOW if has_new else _C_RESET

            row = f"  {i:<3} {name}  {can_write}    {posts} {last}\r\n"
            await s.send(color + row.encode() + _C_RESET)

        await s.send(b"  " + _HR + b"\r\n")

    # ── Inside a board ────────────────────────────────────────────────────────

    async def _enter_board(self, board) -> None:
        s = self._s
        bid = board["id"]

        if board["read_ag"] > s.access_group:
            await s.send(self._bb(76))   # "Access denied."
            return

        last_visit = msgbase.get_last_visit(s.user_id, bid)
        msgbase.record_visit(s.user_id, bid)

        # Subboard banner — equivalent of bbstext rec 315 with DC1 v48/v49
        name = (board["name"] or "").strip()
        desc = (board["description"] or "").strip()
        await s.send(
            b"\r\n" +
            _C_WHITE + b"*Subboard " +
            _C_CYAN  + b"(" + _C_WHITE + name.encode() + _C_CYAN + b") " +
            _C_WHITE + desc.encode() +
            _C_RESET + b"\r\n"
        )

        # New activity summary
        new_threads = []
        if last_visit:
            new_threads = msgbase.get_thread_list_since(bid, last_visit)
            if new_threads:
                count = len(new_threads)
                if count == 1:
                    await s.send(self._bb(454))              # "There has been 1 response."
                else:
                    await s.send(self._bb(453, {0: count}))  # "There have been N responses."

                # rec 378: "Read new items now (Yes,Browse,Scan,[N/y])?"
                await s.send(self._bb(378))
                ans = (await s.readline_with_timeout() or '').strip().upper()
                if ans in ('Y', 'YES', ''):
                    for t in new_threads:
                        if not await self._read_thread(t, board):
                            return   # user quit mid-scan
                    return
        else:
            await s.send_line(b"  (First visit to this board)\r\n")

        await self._board_loop(board, last_visit)

    async def _board_loop(self, board, last_visit: str | None) -> None:
        """Inner prompt loop for a single board."""
        s = self._s
        while True:
            # rec 327: "\r\nBrowse, Scan, Read, Post, Quit, ?=Menu\r\n"
            await s.send(self._bb(327))
            raw = await s.readline_with_timeout()
            if raw is None:
                return

            cmd = raw.strip().upper()

            if not cmd or cmd in ('Q', 'QUIT'):
                return

            elif cmd in ('?', 'MENU'):
                await s.send(self._st('base'))

            elif cmd in ('L', 'LIST', 'B', 'BROWSE'):
                await self._thread_list(board)

            elif cmd in ('S', 'SCAN'):
                await self._scan_board(board, last_visit)

            elif cmd in ('N', 'P', 'POST'):
                if board["write_ag"] > s.access_group:
                    await s.send(self._bb(76))
                else:
                    await self._post_new(board)

            elif cmd.isdigit():
                threads = msgbase.get_thread_list(board["id"])
                idx = int(cmd) - 1
                if 0 <= idx < len(threads):
                    await self._read_thread(threads[idx], board)
                else:
                    await s.send_line(
                        f"  Thread {cmd} not found (1-{len(threads)}).\r\n".encode()
                    )
            else:
                await s.send(self._bb(327))

    # ── Thread list ───────────────────────────────────────────────────────────

    async def _thread_list(self, board) -> None:
        s = self._s
        threads = msgbase.get_thread_list(board["id"])

        if not threads:
            await s.send(self._bb(521))   # "This subboard is empty."
            return

        last_visit = msgbase.get_last_visit(s.user_id, board["id"])

        await s.send(b"\r\n")
        await s.send(
            _C_GREEN +
            b"  #    Subject                        Resp  Author          Date" +
            _C_RESET + b"\r\n"
        )
        await s.send(b"  " + _HR + b"\r\n")

        for i, t in enumerate(threads, 1):
            is_new  = last_visit and t["last_activity"] > last_visit
            marker  = b"*" if is_new else b" "
            subject = (t["subject"] or "(no subject)")[:29].ljust(30)
            author  = (t["author_handle"] or "?")[:14].ljust(15)
            resp    = str(t["response_count"]).rjust(4)
            date    = _fmt_date(t["last_activity"])
            color   = _C_YELLOW if is_new else _C_RESET

            row = f"  {i:<3} {subject} {resp}  {author} {date}\r\n"
            await s.send(color + marker + row.encode() + _C_RESET)

        await s.send(b"  " + _HR + b"\r\n")
        await s.send(f"  {len(threads)} thread(s)   * = new activity\r\n".encode())

    # ── Scan for new items ────────────────────────────────────────────────────

    async def _scan_board(self, board, last_visit: str | None) -> None:
        """S inside a board — reads new threads sequentially."""
        threads = msgbase.get_thread_list_since(
            board["id"], last_visit or "1970-01-01T00:00:00"
        )
        if not threads:
            await self._s.send(self._bb(521))
            return
        for t in threads:
            if not await self._read_thread(t, board):
                break

    async def _scan_all_boards(self, boards) -> None:
        """S at the board list — scan every readable board for new items."""
        s = self._s
        found_any = False
        for board in boards:
            if board["read_ag"] > s.access_group:
                continue
            last_visit = msgbase.get_last_visit(s.user_id, board["id"])
            new = msgbase.get_thread_list_since(
                board["id"], last_visit or "1970-01-01T00:00:00"
            )
            if not new:
                continue
            found_any = True
            name = (board["name"] or "").strip()
            await s.send(
                b"\r\n" + _C_WHITE + b"*Subboard " +
                _C_CYAN  + b"(" + _C_WHITE + name.encode() + _C_CYAN + b")" +
                _C_RESET + b"\r\n"
            )
            msgbase.record_visit(s.user_id, board["id"])
            for t in new:
                if not await self._read_thread(t, board):
                    return

        if not found_any:
            await s.send_line(b"\r\n  No new activity in any board.\r\n")

    # ── Read a thread ─────────────────────────────────────────────────────────

    async def _read_thread(self, thread_row, board) -> bool:
        """
        Read all messages in a thread sequentially.

        Returns True  — finished normally (Pass or end-of-thread).
        Returns False — user typed Q (quit entirely).
        """
        s = self._s
        thread_id = thread_row["id"]
        messages  = msgbase.get_thread_messages(thread_id)

        if not messages:
            await s.send_line(b"\r\n  [Thread is empty or deleted]\r\n")
            return True

        msg_idx = 0
        while msg_idx < len(messages):
            msg   = messages[msg_idx]
            total = len(messages)

            await self._print_message(msg, msg_idx, total)

            # rec 666: "\r\nEnter item#, Scan, Quit, ?=Menu\r\n"
            await s.send(self._bb(666))
            raw = await s.readline_with_timeout()
            if raw is None:
                return False

            cmd = raw.strip().upper()

            if cmd == 'R':
                if board["write_ag"] > s.access_group:
                    await s.send(self._bb(76))
                else:
                    await self._respond(msg, thread_row, board)
                    messages = msgbase.get_thread_messages(thread_id)
                msg_idx += 1

            elif cmd in ('P', ''):
                # Pass — advance to next message
                msg_idx += 1

            elif cmd == 'A':
                pass   # Again — re-display current, don't advance

            elif cmd == 'N':
                if board["write_ag"] > s.access_group:
                    await s.send(self._bb(76))
                else:
                    await self._post_new(board)

            elif cmd == '?':
                await s.send(self._st('mess'))

            elif cmd == 'Q':
                return False

            elif cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(messages):
                    msg_idx = idx
                else:
                    await s.send_line(
                        f"\r\n  No item {cmd} (1-{len(messages)}).\r\n".encode()
                    )
            else:
                await s.send(self._bb(666))

        await s.send_line(b"\r\n  [End of thread]\r\n")
        return True

    async def _print_message(self, msg, idx: int, total: int) -> None:
        """Print a single message with an authentic CNet-style header."""
        s        = self._s
        date_str = _fmt_time(msg["posted_at"])
        subj     = msg["subject"] or "(no subject)"
        author   = msg["author_handle"] or "?"

        await s.send(b"\r\n  " + _HR + b"\r\n")

        # "Item 1" (or "Item 1; response 2 of 4")
        item_hdr = self._bb(1044, {0: idx + 1})
        if total > 1:
            item_hdr += self._bb(1045, {0: idx, 1: total - 1})
        await s.send(b"  " + item_hdr + b"\r\n")

        await s.send(_C_CYAN + b"  From:    " + _C_WHITE + author.encode()   + _C_RESET + b"\r\n")
        await s.send(_C_CYAN + b"  Date:    " + _C_WHITE + date_str.encode() + _C_RESET + b"\r\n")
        await s.send(_C_CYAN + b"  Subject: " + _C_WHITE + subj.encode()     + _C_RESET + b"\r\n")
        await s.send(b"  " + _HR + b"\r\n\r\n")

        body = msg["body"] or ""
        for line in body.splitlines():
            await s.send_line(f"  {line}".encode())

        await s.send(b"\r\n")

    # ── Post new thread ───────────────────────────────────────────────────────

    async def _post_new(self, board) -> None:
        from server.editor import VisualEditor
        s = self._s
        await s.send(self._st('post'))

        await s.send(b"  Subject: ")
        raw = await s.readline_with_timeout()
        if raw is None or not raw.strip():
            await s.send_line(b"  [Cancelled]\r\n")
            return

        subject = raw.strip()[:72]

        # rec 342: "Posting"
        await s.send(self._bb(342) + b"\r\n")
        await s.send_line(b"  (Use ^X S to save, ^X A to abort, ^X L for line editor)\r\n")
        await asyncio.sleep(0.3)

        editor = VisualEditor(s, subject=subject)
        body   = await editor.run()
        if body is None:
            await s.send_line(b"  [Message discarded]\r\n")
            return

        msg_id = msgbase.post_new_thread(
            subboard_id   = board["id"],
            author_id     = s.user_id,
            author_handle = s.handle,
            subject       = subject,
            body          = body,
        )
        await s.send_line(f"\r\n  Message posted (#{msg_id}).\r\n".encode())

    # ── Respond ───────────────────────────────────────────────────────────────

    async def _respond(self, parent_msg, thread_row, board) -> None:
        s = self._s
        parent_subj = parent_msg["subject"] or ""
        subject = parent_subj if parent_subj.startswith("Re: ") \
                  else f"Re: {parent_subj}"

        # rec 474: 'Publicly respond to "Subject":'
        await s.send(self._bb(474, {0: parent_subj}))

        await s.send(b"\r\n  Quote previous message? (Y/N): ")
        raw = await s.readline_with_timeout()
        quote_lines = None
        if raw and raw.strip().upper() in ('Y', 'YES'):
            quote_lines = (parent_msg["body"] or "").splitlines()[:5]

        # rec 476: "Responding"
        await s.send(self._bb(476) + b"\r\n")

        editor = LineEditor(s, subject=subject, quote_lines=quote_lines)
        body   = await editor.run()
        if body is None:
            await s.send_line(b"  [Response discarded]\r\n")
            return

        # rec 477: "Response filing..."
        await s.send(self._bb(477))

        msg_id = msgbase.post_response(
            subboard_id   = board["id"],
            thread_id     = thread_row["id"],
            parent_id     = parent_msg["id"],
            author_id     = s.user_id,
            author_handle = s.handle,
            subject       = subject,
            body          = body,
        )
        await s.send_line(f"\r\n  Response posted (#{msg_id}).\r\n".encode())
