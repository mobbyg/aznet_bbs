"""
server/session.py — Per-Caller Session State Machine

This is the heart of the BBS.  Every time a user connects, a new BBSSession
object is created and its run() coroutine is launched as an asyncio Task.

ASYNCIO PRIMER (READ THIS):
─────────────────────────────────────────────────────────────────────────────
asyncio runs everything in a single thread using an "event loop".  Instead of
blocking the whole program when we wait for a user to type, we use `await` to
pause THIS task and let other tasks run.

  await writer.drain()      → "pause here until the network buffer clears"
  await self.readline()     → "pause here until the user presses Enter"
  await asyncio.sleep(1)    → "pause here for 1 second"

None of these block other callers.  While one session is waiting for input,
the event loop handles all the other sessions.

A session has STATES.  It starts at LOGIN and moves forward:
  LOGIN_ENTER → LOGIN_PASSWORD → MAIN_MENU → (future states)

We implement this as a simple dispatch loop rather than a complex state
machine framework — Python async/await makes sequential code readable enough
that we can just write it as a coroutine that flows from top to bottom.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
from datetime import datetime

from config import Config
from server.terminal import BBSText
from server import database as db
from server.database import write_activity
from server.boards import BoardArea

log = logging.getLogger('anet.session')


class BBSSession:
    """
    Represents one connected caller.

    Args:
        node_id : integer node slot assigned to this connection (0-based)
        reader  : asyncio.StreamReader — incoming bytes from the client
        writer  : asyncio.StreamWriter — outgoing bytes to the client
        bbstext : shared BBSText instance loaded from bbstext.txt
        peer    : string like "192.168.1.5:54321" for logging
    """

    def __init__(
        self,
        node_id: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bbstext: BBSText,
        peer: str,
    ):
        self.node_id   = node_id
        self.reader    = reader
        self.writer    = writer
        self.bbstext   = bbstext
        self.peer      = peer

        # User state (populated after login)
        self.user_id      : int | None = None
        self.handle       : str        = ''
        self.access_group : int        = 0
        self.location     : str        = peer.split(':')[0]   # default to IP

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main coroutine for this session.  Called once per connection.
        Handles the full lifecycle: login → menu → disconnect.

        Any unhandled exception is caught here so one bad session never
        crashes the server.
        """
        log.info("Node %d — connection from %s", self.node_id, self.peer)
        write_activity(f"Connection from {self.peer}", self.node_id)
        db.register_node_waiting(self.node_id)

        try:
            await self._run_login()
        except asyncio.TimeoutError:
            await self.send_line("\r\nIdle timeout. Goodbye.\r\n")
        except (ConnectionResetError, BrokenPipeError):
            log.info("Node %d — connection dropped by client", self.node_id)
        except Exception as exc:
            log.exception("Node %d — unhandled error: %s", self.node_id, exc)
        finally:
            db.clear_node(self.node_id)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            log.info("Node %d — session ended", self.node_id)
            write_activity(f"Session ended  (user: {self.handle or 'none'})", self.node_id)

    # -----------------------------------------------------------------------
    # Login sequence
    # Mirrors the CNet login flow from Chapter 5 of the manual.
    # bbstext.txt line numbers are referenced by name for clarity.
    # -----------------------------------------------------------------------

    async def _run_login(self) -> None:
        """Full login sequence: banner → handle → password → menu."""

        # ── Banner / port announcement (bbstext line 15) ──────────────────
        # Line 15: "Port %-2d   \x19c6%s"
        # %0 = node number,  %1 = status string
        await self.send(self.bbstext.render(15, {0: self.node_id, 1: "ANet BBS"}))
        await self.send_line()

        # ── Press RETURN prompt (bbstext line 19) ─────────────────────────
        # Line 19: "Press RETURN to enter system: \x19i0"
        await self.send(self.bbstext.render(19))
        await self.readline_with_timeout(Config.LOGIN_TIMEOUT)  # wait for any key

        # ── Handle prompt loop ─────────────────────────────────────────────
        for attempt in range(Config.MAX_LOGIN_TRIES):
            # Line 29: "\r\nEnter NEW if you have no account.\r\n"
            if attempt == 0:
                await self.send(self.bbstext.render(29))

            # Line 30: "Enter your handle.\r\n: "
            await self.send(self.bbstext.render(30))
            handle = await self.readline_with_timeout(Config.LOGIN_TIMEOUT)
            handle = handle.strip()

            if not handle:
                continue

            # ── New user branch ────────────────────────────────────────────
            if handle.upper() == 'NEW':
                await self._run_new_user()
                return

            # ── Existing user: password ────────────────────────────────────
            # Line 37: "\r\nEnter your password.\r\n: "
            await self.send(self.bbstext.render(37))

            # Password entry: disable echo so it's not visible
            await self.send_noecho_on()
            password = await self.readline_with_timeout(Config.LOGIN_TIMEOUT)
            await self.send_noecho_off()
            await self.send_line()   # newline after hidden password entry

            password = password.strip()

            # ── Verify ─────────────────────────────────────────────────────
            # Line 38: "\r\nVerifying..."  (note: no newline, we add it)
            await self.send(self.bbstext.render(38))
            await self.send_line()

            user = db.authenticate_user(handle, password)

            if user is None:
                # Line 39: "Incorrect password.\r\n"
                await self.send(self.bbstext.render(39))
                log.warning("Node %d — failed login for handle '%s'", self.node_id, handle)
                continue

            # ── Successful login ───────────────────────────────────────────
            self.user_id      = user['id']
            self.handle       = user['handle']
            self.access_group = user['access_group']

            db.update_last_call(self.user_id)
            db.update_node_online(
                node_id      = self.node_id,
                user_id      = self.user_id,
                handle       = self.handle,
                location     = self.location,
                access_group = self.access_group,
            )

            log.info("Node %d — '%s' logged in (AG %d)", self.node_id, self.handle, self.access_group)
            write_activity(f"Login: {self.handle} (AG {self.access_group}) from {self.location}", self.node_id)
            await self._run_main_menu()
            return

        # ── Too many failed attempts ───────────────────────────────────────
        # Line 42: "\r\n@ Connection closed\r\n"
        await self.send(self.bbstext.render(42))

    # -----------------------------------------------------------------------
    # New user registration
    # -----------------------------------------------------------------------

    async def _run_new_user(self) -> None:
        """Simple new user registration flow."""

        await self.send_line("\r\n--- New User Registration ---\r\n")

        # ── Choose a handle ────────────────────────────────────────────────
        while True:
            await self.send_line("Enter the handle you want to use: ")
            handle = (await self.readline_with_timeout(Config.LOGIN_TIMEOUT)).strip()

            if len(handle) < 2:
                await self.send_line("Handle must be at least 2 characters.\r\n")
                continue

            if db.get_user_by_handle(handle) is not None:
                await self.send_line("That handle is already taken. Please choose another.\r\n")
                continue

            break

        # ── Real name ─────────────────────────────────────────────────────
        await self.send_line("Enter your real name: ")
        real_name = (await self.readline_with_timeout(Config.LOGIN_TIMEOUT)).strip()

        # ── Location ──────────────────────────────────────────────────────
        await self.send_line("Enter your location (City, State/Country): ")
        location = (await self.readline_with_timeout(Config.LOGIN_TIMEOUT)).strip()
        self.location = location or self.location

        # ── Password ──────────────────────────────────────────────────────
        while True:
            await self.send_line("Choose a password (min 4 chars): ")
            await self.send_noecho_on()
            pw1 = (await self.readline_with_timeout(Config.LOGIN_TIMEOUT)).strip()
            await self.send_noecho_off()
            await self.send_line()

            await self.send_line("Confirm password: ")
            await self.send_noecho_on()
            pw2 = (await self.readline_with_timeout(Config.LOGIN_TIMEOUT)).strip()
            await self.send_noecho_off()
            await self.send_line()

            if pw1 != pw2:
                await self.send_line("Passwords don't match. Try again.\r\n")
                continue
            if len(pw1) < 4:
                await self.send_line("Password too short (min 4 chars).\r\n")
                continue
            break

        # ── Create account ─────────────────────────────────────────────────
        try:
            user_id = db.create_user(
                handle    = handle,
                password  = pw1,
                real_name = real_name,
                location  = location,
            )
        except ValueError as exc:
            await self.send_line(f"\r\nError: {exc}\r\n")
            return

        self.user_id      = user_id
        self.handle       = handle
        self.access_group = Config.DEFAULT_NEW_USER_AG

        db.update_node_online(
            node_id      = self.node_id,
            user_id      = user_id,
            handle       = handle,
            location     = location,
            access_group = self.access_group,
        )

        await self.send_line(f"\r\nWelcome to ANet, {handle}!\r\n")
        log.info("Node %d — new user '%s' registered", self.node_id, handle)
        write_activity(f"NEW USER: {handle} from {location}", self.node_id)
        await self._run_main_menu()

    # -----------------------------------------------------------------------
    # Main Menu
    # This is a stub for Milestone 1.  Full menus come in Milestone 2.
    # -----------------------------------------------------------------------

    async def _run_main_menu(self) -> None:
        """
        Main command level — the top-level BBS menu.
        Currently a stub that shows a prompt and accepts Q to quit.
        Full command dispatch (subboards, mail, files, etc.) comes next.
        """
        await self.send_line(f"\r\nWelcome, {self.handle}!  Type ? for help, Q to logoff.\r\n")

        while True:
            # Simple prompt for now — will be replaced with full bbstext.txt menu
            await self.send(b"\r\nANet> ")
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None:
                # Timeout at main menu — log the user off gracefully
                await self.send_line(b"\r\n[Session timeout]\r\n")
                return
            command = raw.strip().upper()

            if not command:
                continue

            if command in ('Q', 'QUIT', 'LOGOFF', 'BYE', 'G', 'GOODBYE'):
                await self.send_line("\r\nThanks for calling ANet.  Goodbye!\r\n")
                return

            elif command == '?':
                await self._send_help()

            elif command == 'WHO':
                await self._cmd_who()

            elif command in ('B', 'BOARDS', 'BASE'):
                area = BoardArea(self)
                await area.run()

            elif command == 'NEWBOARD':
                if self.access_group >= 31:
                    await self._cmd_newboard()
                else:
                    await self.send_line("\r\n  SysOp access required for NEWBOARD.\r\n")

            else:
                await self.send_line(f"Unknown command: {command}  (type ? for help)\r\n")

    async def _send_help(self) -> None:
        help_text = (
            "\r\n"
            "  B / BOARDS  — Enter the message boards\r\n"
            "  WHO         — Who is online\r\n"
            "  Q           — Logoff\r\n"
            "  ?           — This help\r\n"
            "\r\n"
            "  SysOp only:\r\n"
            "  NEWBOARD    — Create a new message board\r\n"
            "\r\n"
        )
        await self.send_line(help_text)

    async def _cmd_who(self) -> None:
        """Show who is currently online."""
        sessions = db.get_active_sessions()
        if not sessions:
            await self.send_line("\r\nNo one appears to be online.\r\n")
            return

        await self.send_line("\r\n  Node  Handle               Status\r\n")
        await self.send_line("  ----  -------------------  ----------\r\n")
        for s in sessions:
            handle = s['handle'] or '(connecting)'
            status = s['status']
            await self.send_line(f"  {s['node_id']:<4}  {handle:<21}  {status}\r\n")
        await self.send_line("\r\n")

    async def _cmd_newboard(self) -> None:
        """SysOp command: create a new message subboard interactively."""
        from server import msgbase
        await self.send_line(b"\r\n  -- Create New Board --\r\n")

        await self.send(b"  Board name: ")
        raw = await self.readline_with_timeout()
        if not raw or not raw.strip():
            await self.send_line(b"  [Cancelled]\r\n")
            return
        name = raw.strip()[:40]

        await self.send(b"  Description (optional): ")
        raw = await self.readline_with_timeout()
        description = raw.strip()[:80] if raw else ""

        await self.send(b"  Minimum AG to READ  (0-31, Enter=0): ")
        raw = await self.readline_with_timeout()
        try:
            read_ag = max(0, min(31, int(raw.strip()))) if raw and raw.strip() else 0
        except ValueError:
            read_ag = 0

        await self.send(b"  Minimum AG to WRITE (0-31, Enter=5): ")
        raw = await self.readline_with_timeout()
        try:
            write_ag = max(0, min(31, int(raw.strip()))) if raw and raw.strip() else 5
        except ValueError:
            write_ag = 5

        # Confirm
        await self.send_line(
            f"\r\n  Name        : {name}\r\n"
            f"  Description : {description}\r\n"
            f"  Read AG     : {read_ag}\r\n"
            f"  Write AG    : {write_ag}\r\n"
        )
        await self.send(b"  Create this board? (Y/N): ")
        confirm = await self.readline_with_timeout()
        if not confirm or confirm.strip().upper() not in ("Y", "YES"):
            await self.send_line(b"  [Cancelled]\r\n")
            return

        board_id = msgbase.create_subboard(name, description, read_ag, write_ag,
                                           self.handle)
        await self.send_line(
            f"  Board '{name}' created (#{board_id}).\r\n".encode()
        )

    # -----------------------------------------------------------------------
    # I/O helpers
    # -----------------------------------------------------------------------

    async def send(self, data: bytes | str) -> None:
        """
        Write bytes (or a string) to the client and flush the buffer.

        `await writer.drain()` is the asyncio way of saying "wait until all
        buffered data has actually been sent over the network before continuing".
        Without drain(), we could queue up megabytes in RAM and overwhelm a
        slow connection.
        """
        if isinstance(data, str):
            data = data.encode('latin-1', errors='replace')
        self.writer.write(data)
        await self.writer.drain()

    async def send_line(self, text: str | bytes = '') -> None:
        """Send text followed by \\r\\n."""
        if isinstance(text, str):
            text = text.encode('latin-1', errors='replace')
        await self.send(text + b'\r\n' if not text.endswith(b'\r\n') else text)

    async def readline_with_timeout(self, timeout: float = None) -> str | None:
        """
        Read a line of input from the client with a timeout.

        Returns the decoded line (str), or None on timeout or connection close.

        asyncio.wait_for() wraps any awaitable with a deadline.  If the user
        doesn't press Enter within `timeout` seconds, TimeoutError is raised —
        we catch it and return None so callers can handle it gracefully rather
        than propagating an exception.
        """
        if timeout is None:
            timeout = Config.SESSION_TIMEOUT
        try:
            raw = await asyncio.wait_for(
                self.reader.readline(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None
        if not raw:
            return None
        return raw.decode('latin-1', errors='replace').rstrip('\r\n')

    # -- Echo control (Telnet IAC sequences) ---------------------------------
    # These tell the client terminal to stop echoing keys the user types,
    # so passwords appear blank.  The TelnetProtocol layer handles the full
    # negotiation; here we just send the relevant IAC sequences directly.

    async def send_noecho_on(self) -> None:
        """Stop echoing — used for password fields.
        The client handed us echo control at connection time (IAC WILL ECHO
        in the initial negotiation), so we just flip the flag on the reader."""
        self.reader.echo = False

    async def send_noecho_off(self) -> None:
        """Resume echoing after a password field."""
        self.reader.echo = True

