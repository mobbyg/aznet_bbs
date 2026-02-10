"""
server/boards.py — Message Base Navigation

Handles the interactive board-list → board → thread → read → respond
navigation flow, called from session.py.

Flow:
  BoardArea.run()
      └── _board_list()         show numbered list of accessible boards
              └── _enter_board(board)   board-level prompt
                      ├── _thread_list()    show all threads
                      ├── _read_thread()    read a thread + respond/pass
                      └── _post_new()       start a new thread

DISPLAY CONVENTIONS (CNet-authentic):
  Board list:    #  Name                    R/W   Posts  Last Post
  Thread list:   #  Subject                 Resp  Author         Date
  Message hdr:   From: / Date: / Subject:
  Respond/pass:  R=Respond  P=Pass  Q=Quit  N=New Post  .=Re-read
"""

import logging
from datetime import datetime, timezone

from server import msgbase
from server.editor import LineEditor

log = logging.getLogger("anet.boards")


def _fmt_date(iso: str | None) -> str:
    """Format an ISO timestamp to short display date."""
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


class BoardArea:
    """
    Encapsulates all board/thread navigation for one session.
    Created fresh each time the user enters the message base.
    """

    def __init__(self, session):
        self._session = session

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main entry point.  Called from session.py when user types B/BOARDS.
        Runs the board list loop until the user quits.
        """
        await self._board_list()

    # ── Board list ────────────────────────────────────────────────────────────

    async def _board_list(self) -> None:
        s = self._session
        while True:
            boards = msgbase.get_subboards_for_user(s.access_group)

            if not boards:
                await s.send_line(b"\r\n  No message boards available.\r\n")
                return

            await self._print_board_list(boards)

            await s.send(b"\r\nBoard #, N=New boards, Q=Quit: ")
            raw = await s.readline_with_timeout()
            if raw is None:
                return

            cmd = raw.strip().upper()

            if cmd in ("Q", "QUIT", ""):
                return

            if cmd == "N":
                # Show only boards with activity since last visit
                await self._show_new_activity(boards)
                continue

            # Numeric board selection
            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(boards):
                    await self._enter_board(boards[idx])
                    continue
            await s.send_line(b"  Invalid selection.\r\n")

    async def _print_board_list(self, boards) -> None:
        s = self._session
        await s.send_line(b"\r\n")
        await s.send_line(b"  \x1b[1;32m#   Name                           R/W   Posts  Last Post\x1b[0m\r\n")
        await s.send_line(b"  " + b"\xc4" * 60 + b"\r\n")

        for i, b in enumerate(boards, 1):
            can_write = "RW" if b["write_ag"] <= s.access_group else "R "
            last = _fmt_date(b["last_post_at"])
            name = (b["name"] or "")[:30].ljust(30)
            row = f"  {i:<3} {name}  {can_write}    {b['post_count']:<6} {last}\r\n"
            await s.send_line(row.encode())

        await s.send_line(b"  " + b"\xc4" * 60 + b"\r\n")

    async def _show_new_activity(self, boards) -> None:
        s = self._session
        found_any = False
        await s.send_line(b"\r\n  Boards with new activity:\r\n")
        for b in boards:
            last_visit = msgbase.get_last_visit(s.user_id, b["id"])
            since = last_visit or "1970-01-01T00:00:00"
            new_threads = msgbase.get_thread_list_since(b["id"], since)
            if new_threads:
                found_any = True
                await s.send_line(
                    f"  [{b['id']}] {b['name']}: {len(new_threads)} new thread(s)\r\n"
                    .encode()
                )
        if not found_any:
            await s.send_line(b"  No new activity since your last visit.\r\n")

    # ── Inside a board ────────────────────────────────────────────────────────

    async def _enter_board(self, board) -> None:
        s = self._session
        bid = board["id"]

        # Access check
        if board["read_ag"] > s.access_group:
            await s.send_line(b"\r\n  Access denied to this board.\r\n")
            return

        # Record visit timestamp (before we show anything, so "new" is accurate)
        last_visit = msgbase.get_last_visit(s.user_id, bid)
        msgbase.record_visit(s.user_id, bid)

        await s.send_line(f"\r\n  \x1b[1;32m{board['name']}\x1b[0m".encode() + b"\r\n")
        if board["description"]:
            await s.send_line(f"  {board['description']}\r\n".encode())

        # Show new activity indicator
        if last_visit:
            since = last_visit
            new_threads = msgbase.get_thread_list_since(bid, since)
            if new_threads:
                await s.send_line(
                    f"  {len(new_threads)} new thread(s) since your last visit.\r\n"
                    .encode()
                )
        else:
            await s.send_line(b"  (First visit to this board)\r\n")

        while True:
            await s.send(b"\r\n  L=List  N=New Post  #=Read Thread  Q=Quit: ")
            raw = await s.readline_with_timeout()
            if raw is None:
                return

            cmd = raw.strip().upper()

            if cmd in ("Q", "QUIT", ""):
                return

            elif cmd in ("L", "LIST"):
                await self._thread_list(board)

            elif cmd in ("N", "NEW"):
                if board["write_ag"] > s.access_group:
                    await s.send_line(b"\r\n  Write access denied to this board.\r\n")
                else:
                    await self._post_new(board)

            elif cmd.isdigit():
                # User typed a thread number from the list
                threads = msgbase.get_thread_list(bid)
                idx = int(cmd) - 1
                if 0 <= idx < len(threads):
                    await self._read_thread(threads[idx], board)
                else:
                    await s.send_line(b"  Invalid thread number.\r\n")
            else:
                await s.send_line(b"  L=List  N=New Post  #=Read Thread  Q=Quit\r\n")

    # ── Thread list ───────────────────────────────────────────────────────────

    async def _thread_list(self, board) -> None:
        s = self._session
        threads = msgbase.get_thread_list(board["id"])

        if not threads:
            await s.send_line(b"\r\n  No messages in this board yet.  Use N to post.\r\n")
            return

        last_visit = msgbase.get_last_visit(s.user_id, board["id"])

        await s.send_line(b"\r\n")
        await s.send_line(
            b"  \x1b[1;32m#    Subject                        Resp  Author          Date\x1b[0m\r\n"
        )
        await s.send_line(b"  " + b"\xc4" * 70 + b"\r\n")

        for i, t in enumerate(threads, 1):
            is_new = last_visit and t["last_activity"] > last_visit
            marker   = "*" if is_new else " "
            subject  = (t["subject"] or "(no subject)")[:29].ljust(30)
            author   = (t["author_handle"] or "?")[:14].ljust(15)
            resp_cnt = str(t["response_count"]).rjust(4)
            date     = _fmt_date(t["last_activity"])
            row = f"  {marker}{i:<3} {subject} {resp_cnt}  {author} {date}\r\n"
            await s.send_line(row.encode())

        await s.send_line(b"  " + b"\xc4" * 70 + b"\r\n")
        await s.send_line(
            f"  {len(threads)} thread(s)   * = new activity\r\n".encode()
        )

    # ── Read a thread ─────────────────────────────────────────────────────────

    async def _read_thread(self, thread_row, board) -> None:
        """
        Read all messages in a thread sequentially, with respond/pass prompt
        after each message.
        """
        s = self._session
        thread_id = thread_row["id"]
        messages  = msgbase.get_thread_messages(thread_id)

        if not messages:
            await s.send_line(b"\r\n  [Thread is empty or deleted]\r\n")
            return

        msg_idx = 0
        while msg_idx < len(messages):
            msg = messages[msg_idx]
            await self._print_message(msg, msg_idx, len(messages))

            # Respond/pass prompt
            await s.send(
                b"\r\n  R=Respond  P=Pass  N=New Post  .=Re-read  Q=Quit: "
            )
            raw = await s.readline_with_timeout()
            if raw is None:
                return

            cmd = raw.strip().upper()

            if cmd == "R":
                if board["write_ag"] > s.access_group:
                    await s.send_line(b"\r\n  Write access denied.\r\n")
                else:
                    await self._respond(msg, thread_row, board)
                    # Reload messages in case a response was added
                    messages = msgbase.get_thread_messages(thread_id)
                    msg_idx += 1

            elif cmd in ("P", ""):
                # Pass — move to next message in thread
                msg_idx += 1

            elif cmd == "N":
                if board["write_ag"] > s.access_group:
                    await s.send_line(b"\r\n  Write access denied.\r\n")
                else:
                    await self._post_new(board)

            elif cmd == ".":
                # Re-read current message — don't advance
                pass

            elif cmd == "Q":
                return

            else:
                await s.send_line(
                    b"  R=Respond  P=Pass  N=New Post  .=Re-read  Q=Quit\r\n"
                )

        await s.send_line(b"\r\n  [End of thread]\r\n")

    async def _print_message(self, msg, idx: int, total: int) -> None:
        """Print a single message with CNet-style header."""
        s = self._session
        date_str = _fmt_time(msg["posted_at"])
        subj     = msg["subject"] or "(no subject)"
        author   = msg["author_handle"] or "?"
        resp_label = "Post" if idx == 0 else f"Response {idx}"

        await s.send_line(b"\r\n")
        await s.send_line(b"  " + b"\xc4" * 70 + b"\r\n")
        await s.send_line(
            f"  \x1b[1;32m{resp_label} {idx + 1}/{total}\x1b[0m\r\n".encode()
        )
        await s.send_line(f"  From:    {author}\r\n".encode())
        await s.send_line(f"  Date:    {date_str}\r\n".encode())
        await s.send_line(f"  Subject: {subj}\r\n".encode())
        await s.send_line(b"  " + b"\xc4" * 70 + b"\r\n")
        await s.send_line(b"\r\n")

        # Print body, word-wrapping long lines
        body = msg["body"] or ""
        for line in body.splitlines():
            # Indent body text slightly
            await s.send_line(f"  {line}\r\n".encode())

    # ── Post new thread ───────────────────────────────────────────────────────

    async def _post_new(self, board) -> None:
        s = self._session
        await s.send_line(b"\r\n  -- New Post --\r\n")

        await s.send(b"  Subject: ")
        raw = await s.readline_with_timeout()
        if raw is None or not raw.strip():
            await s.send_line(b"  [Cancelled]\r\n")
            return

        subject = raw.strip()
        if len(subject) > 72:
            subject = subject[:72]

        editor = LineEditor(s, subject=subject)
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
        await s.send_line(
            f"\r\n  Message posted (#{msg_id}).\r\n".encode()
        )

    # ── Respond to an existing thread ─────────────────────────────────────────

    async def _respond(self, parent_msg, thread_row, board) -> None:
        s = self._session
        parent_subject = parent_msg["subject"] or ""
        subject = parent_subject if parent_subject.startswith("Re: ") \
                  else f"Re: {parent_subject}"

        await s.send_line(b"\r\n  -- Response --\r\n")

        # Offer to quote the parent message
        await s.send(b"  Quote previous message? (Y/N): ")
        raw = await s.readline_with_timeout()
        quote_lines = None
        if raw and raw.strip().upper() in ("Y", "YES"):
            body_lines = (parent_msg["body"] or "").splitlines()
            # Limit quote to first 5 lines to avoid bloat
            quote_lines = body_lines[:5]

        editor = LineEditor(s, subject=subject, quote_lines=quote_lines)
        body   = await editor.run()

        if body is None:
            await s.send_line(b"  [Response discarded]\r\n")
            return

        msg_id = msgbase.post_response(
            subboard_id   = board["id"],
            thread_id     = thread_row["id"],
            parent_id     = parent_msg["id"],
            author_id     = s.user_id,
            author_handle = s.handle,
            subject       = subject,
            body          = body,
        )
        await s.send_line(
            f"\r\n  Response posted (#{msg_id}).\r\n".encode()
        )
