"""
server/editor.py — CNet Line Editor

Implements CNet's authentic "dot command" line editor from Chapter 10.

Usage:
    editor = LineEditor(session, subject="Re: Hello")
    body = await editor.run()
    if body is None:
        # user typed .A to abort
    else:
        # body is the composed text

DOT COMMANDS (typed at the start of a new line):
    .S   Save  — accept and return the composed text
    .A   Abort — discard everything, return None
    .L   List  — display all lines with line numbers
    .D   Delete last line
    .H   Help  — show command summary
    .N   New   — clear all text and start over
    ..   Literal period at the start of a line

The prompt shows the current line number, e.g.:  1:
"""

import logging

log = logging.getLogger("anet.editor")

# Max characters per line before we hard-wrap
_LINE_WIDTH = 74
# Max lines a user may enter
_MAX_LINES  = 100


class LineEditor:
    """
    CNet-style line-by-line message editor.

    Args:
        session      : the BBSSession (used for send() and readline())
        subject      : optional subject to display in the header
        quote_lines  : optional list of str lines to pre-populate as a quote
        max_lines    : maximum number of lines allowed (default 100)
    """

    def __init__(self, session, subject: str = "", quote_lines: list[str] | None = None,
                 max_lines: int = _MAX_LINES):
        self._session   = session
        self._subject   = subject
        self._max_lines = max_lines
        self._lines: list[str] = []

        if quote_lines:
            # Pre-populate quoted text with "> " prefix
            handle = getattr(session, "handle", "User")
            initials = _initials(handle)
            for line in quote_lines:
                self._lines.append(f"{initials}> {line}")

    # ── Public ───────────────────────────────────────────────────────────────

    async def run(self) -> str | None:
        """
        Run the interactive editor.
        Returns the composed body text, or None if the user aborted.
        """
        await self._print_header()
        await self._session.send_line(b"  Type your message.  Enter . commands at the start of a line.")
        await self._session.send_line(b"  .S=Save  .A=Abort  .L=List  .V=Visual editor  .H=Help\r\n")

        # If we have pre-loaded quote lines, show them
        if self._lines:
            await self._list_lines()

        while True:
            line_no = len(self._lines) + 1
            prompt  = f"{line_no}: ".encode()
            await self._session.send(prompt)

            raw = await self._session.readline_with_timeout()
            if raw is None:
                # Timeout — treat as abort
                await self._session.send_line(b"\r\n[Editor timeout - message discarded]\r\n")
                return None

            text = raw

            # ── Dot command? ────────────────────────────────────────────────
            if text.startswith("."):
                result = await self._handle_dot(text)
                if result == "SAVE":
                    return self._finalise()
                elif result == "ABORT":
                    return None
                elif result == "CONTINUE":
                    continue
                # "HANDLED" — already printed output, loop
                continue

            # ── Normal text line ────────────────────────────────────────────
            if len(self._lines) >= self._max_lines:
                await self._session.send_line(
                    f"  [Maximum {self._max_lines} lines reached — use .S to save or .A to abort]\r\n"
                    .encode()
                )
                continue

            # Word-wrap if line is too long (rare — terminal usually handles this)
            wrapped = _wrap_line(text, _LINE_WIDTH)
            for wl in wrapped:
                if len(self._lines) < self._max_lines:
                    self._lines.append(wl)

    # ── Dot command dispatcher ────────────────────────────────────────────────

    async def _handle_dot(self, text: str) -> str:
        """
        Process a dot command.
        Returns: "SAVE", "ABORT", "CONTINUE", or "HANDLED".
        """
        # ".." = literal period at start of line
        if text == "..":
            self._lines.append(".")
            return "HANDLED"

        cmd = text[1:2].upper()

        if cmd == "S":
            if not self._lines:
                await self._session.send_line(b"  [Nothing to save - type some text first]\r\n")
                return "HANDLED"
            await self._session.send_line(b"  [Saving...]\r\n")
            return "SAVE"

        elif cmd == "A":
            await self._session.send(b"  Abort message? (Y/N): ")
            confirm = await self._session.readline_with_timeout()
            if confirm and confirm.strip().upper() in ("Y", "YES"):
                await self._session.send_line(b"  [Message discarded]\r\n")
                return "ABORT"
            await self._session.send_line(b"  [Continue typing]\r\n")
            return "HANDLED"

        elif cmd == "L":
            await self._list_lines()
            return "HANDLED"

        elif cmd == "D":
            if not self._lines:
                await self._session.send_line(b"  [Nothing to delete]\r\n")
            else:
                deleted = self._lines.pop()
                await self._session.send_line(
                    f"  [Deleted: {deleted[:60]}{'...' if len(deleted) > 60 else ''}]\r\n"
                    .encode()
                )
            return "HANDLED"

        elif cmd == "N":
            await self._session.send(b"  Clear all text? (Y/N): ")
            confirm = await self._session.readline_with_timeout()
            if confirm and confirm.strip().upper() in ("Y", "YES"):
                self._lines.clear()
                await self._session.send_line(b"  [Text cleared - start over]\r\n")
            else:
                await self._session.send_line(b"  [Continuing]\r\n")
            return "HANDLED"

        elif cmd == "H":
            await self._print_help()
            return "HANDLED"

        elif cmd == "V":
            # Switch to visual editor
            from server.editor import VisualEditor
            await self._session.send_line(b"  [Switching to visual editor...]\r\n")
            ve = VisualEditor(self._session, subject=self._subject, 
                            quote_lines=self._lines)
            result = await ve.run()
            if result is not None:
                # Visual editor returned composed text
                # Replace our lines with the visual editor's output
                self._lines = result.splitlines()
                return "SAVE"
            else:
                # Visual editor aborted or returned to line — continue
                await self._session.send_line(b"  [Returned to line editor]\r\n")
                return "CONTINUE"

        else:
            await self._session.send_line(
                f"  [Unknown command: {text[:10]}  —  type .H for help]\r\n".encode()
            )
            return "HANDLED"

    # ── Display helpers ───────────────────────────────────────────────────────

    async def _print_header(self) -> None:
        await self._session.send_line("\r\n" + "-" * 74 + "\r\n")
        if self._subject:
            subj_line = f"  Subject: {self._subject}"
            await self._session.send_line(subj_line.encode() + b"\r\n")
        await self._session.send_line("-" * 74 + "\r\n")

    async def _list_lines(self) -> None:
        if not self._lines:
            await self._session.send_line(b"  [No text entered yet]\r\n")
            return
        await self._session.send_line(b"\r\n")
        for i, line in enumerate(self._lines, 1):
            await self._session.send_line(f"{i:>3}: {line}\r\n".encode())
        await self._session.send_line(
            f"  [{len(self._lines)} line{'s' if len(self._lines) != 1 else ''}]\r\n"
            .encode()
        )

    async def _print_help(self) -> None:
        help_text = (
            "\r\n"
            "  ── Line Editor Dot Commands ──────────────────────\r\n"
            "  .S   Save message and post it\r\n"
            "  .A   Abort — discard message\r\n"
            "  .L   List all lines with numbers\r\n"
            "  .D   Delete the last line\r\n"
            "  .N   New — clear all text, start over\r\n"
            "  .V   Switch to visual (full-screen) editor\r\n"
            "  .H   This help message\r\n"
            "  ..   Insert a literal period at line start\r\n"
            "  ──────────────────────────────────────────────────\r\n"
        )
        await self._session.send_line(help_text.encode())

    # ── Finalise ──────────────────────────────────────────────────────────────

    def _finalise(self) -> str:
        """Join all lines into a single body string."""
        return "\r\n".join(self._lines)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _initials(handle: str) -> str:
    """Return up to 3-character initials for quoting."""
    parts = handle.split()
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _wrap_line(text: str, width: int) -> list[str]:
    """
    Hard-wrap a single line at `width` characters if it's too long.
    Tries to break at word boundaries.
    Returns a list of lines (usually just [text]).
    """
    if len(text) <= width:
        return [text]
    lines = []
    while len(text) > width:
        # Find last space at or before width
        break_at = text.rfind(" ", 0, width)
        if break_at <= 0:
            break_at = width
        lines.append(text[:break_at])
        text = text[break_at:].lstrip()
    if text:
        lines.append(text)
    return lines


# ══════════════════════════════════════════════════════════════════════════════
# VISUAL EDITOR — Full-screen ANSI editor (Chapter 10, "The Text Editors")
# ══════════════════════════════════════════════════════════════════════════════

"""
CNet Visual Editor — full-screen ANSI word-processor-like editor.

Commands (all Ctrl keys):
  ^B   Beginning of line
  ^N   End of line
  ^U   Top of document
  ^O   Bottom of document
  ^A   Page up
  ^Z   Page down
  ^K   Kill from cursor to end of line
  ^V   Verify screen (redraw)
  ^X S Save and exit
  ^X A Abort (with confirmation)
  ^X L Return to line editor

Visual settings:
  - White text on blue background
  - Full-screen display (uses terminal height from user profile)
  - Status line shows subject, addressee, row/col position
  - Auto word-wrap at 74 characters
  - Supports Simple and Full ANSI modes
"""

import asyncio


# ANSI codes for visual editor
_VE_CLR    = '\x1b[2J\x1b[H'           # clear screen, home
_VE_BG     = '\x1b[0;37;44m'           # white on blue
_VE_STATUS = '\x1b[1;37;44m'           # bright white on blue (status)
_VE_RST    = '\x1b[0m'                 # reset


def _goto_ve(row: int, col: int) -> str:
    """Return ANSI goto sequence (1-indexed)."""
    return f'\x1b[{row};{col}H'


class VisualEditor:
    """
    Full-screen visual editor matching CNet PRO behavior.
    
    Args:
        session      : BBSSession
        subject      : message subject (shown in status line)
        addressee    : optional recipient name (shown in status line)
        quote_lines  : optional list of str to pre-load as quoted text
        max_lines    : maximum lines allowed (default 250)
    """
    
    def __init__(self, session, subject: str = "", addressee: str = "",
                 quote_lines: list[str] | None = None, max_lines: int = 250):
        self._s = session
        self._subject = subject[:40]
        self._addressee = addressee[:20]
        self._max_lines = max_lines
        
        # Text buffer: list of lines (each line is a str)
        self._lines: list[str] = []
        if quote_lines:
            handle = getattr(session, "handle", "User")
            initials = _initials(handle)
            for line in quote_lines:
                self._lines.append(f"{initials}> {line}")
        
        # Cursor position (0-indexed row, col in the text buffer)
        self._row = 0
        self._col = 0
        
        # Top visible line (for scrolling)
        self._top_line = 0
        
        # Terminal dimensions
        self._width = getattr(session, 'screen_width', 80)
        self._height = getattr(session, 'screen_height', 24)
        
        # Edit area: rows 2 to height-1 (row 1 is status)
        self._edit_rows = self._height - 1
        
        # ANSI mode (Simple vs Full) from user profile
        ansi_level = getattr(session, 'ansi_level', 'Full')
        self._full_ansi = (ansi_level == 'Full')
    
    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────
    
    async def run(self) -> str | None:
        """
        Run the visual editor.
        Returns composed text, or None if aborted.
        """
        # Switch terminal to raw key mode for VDE-style input
        self._s.reader.raw_keys = True
        try:
            await self._draw_screen()
            while True:
                key = await self._read_key()
                if key is None:
                    # Timeout
                    await self._s.send_line(b"\r\n[Editor timeout]\r\n")
                    return None
                
                result = await self._handle_key(key)
                if result == 'SAVE':
                    return self._get_text()
                elif result == 'ABORT':
                    return None
                elif result == 'TO_LINE':
                    # Return to line editor — re-import and run it
                    from server.editor import LineEditor
                    await self._s.send(_VE_RST.encode())
                    await self._s.send(_VE_CLR.encode())
                    le = LineEditor(self._s, subject=self._subject, 
                                   quote_lines=self._lines)
                    return await le.run()
        finally:
            self._s.reader.raw_keys = False
            await self._s.send(_VE_RST.encode())
    
    # ──────────────────────────────────────────────────────────────────────────
    # Key reading
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _read_key(self, timeout: float = 300.0) -> str | None:
        """
        Read a single keypress. Returns the key as a string, or None on timeout.
        Handles ANSI escape sequences for arrow keys.
        """
        try:
            raw = await asyncio.wait_for(self._s.reader.read(1), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if not raw:
            return None
        
        b = raw[0]
        
        # Telnet IAC — skip
        if b == 0xFF:
            try:
                await asyncio.wait_for(self._s.reader.read(2), timeout=0.2)
            except:
                pass
            return await self._read_key(timeout)
        
        # ESC / ANSI sequences
        if b == 0x1B:
            try:
                nxt = await asyncio.wait_for(self._s.reader.read(1), timeout=0.15)
            except asyncio.TimeoutError:
                return 'ESC'
            if not nxt or nxt[0] == 0x1B:
                return 'ESC'
            if nxt[0] == ord('['):
                # CSI sequence — read params
                params = b''
                for _ in range(8):
                    try:
                        ch = await asyncio.wait_for(self._s.reader.read(1), timeout=0.1)
                    except:
                        break
                    if not ch:
                        break
                    params += ch
                    if 0x40 <= params[-1] <= 0x7E:
                        break
                p = params.decode('latin-1', errors='replace')
                # Map arrow keys
                arrow_map = {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                            'H': 'HOME', 'F': 'END', '5~': 'PGUP', '6~': 'PGDN'}
                return arrow_map.get(p, 'UNKNOWN')
            return 'ESC'
        
        # Ctrl keys (0x01-0x1A)
        if 0x01 <= b <= 0x1A:
            return f'CTRL_{chr(0x40 + b)}'  # CTRL_A, CTRL_B, etc.
        
        # Backspace / Delete
        if b in (0x08, 0x7F):
            return 'BS'
        
        # Enter
        if b in (0x0D, 0x0A):
            # Consume trailing LF if present
            if b == 0x0D:
                try:
                    nxt = await asyncio.wait_for(self._s.reader.read(1), timeout=0.05)
                    if nxt and nxt[0] != 0x0A:
                        pass  # can't un-read
                except asyncio.TimeoutError:
                    pass
            return 'ENTER'
        
        # Printable character
        if 0x20 <= b <= 0x7E:
            return chr(b)
        
        # Unknown — skip
        return await self._read_key(timeout)
    
    # ──────────────────────────────────────────────────────────────────────────
    # Key handling
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _handle_key(self, key: str) -> str | None:
        """
        Handle a keypress. Returns 'SAVE', 'ABORT', 'TO_LINE', or None.
        """
        # Multi-key commands (^X ...)
        if key == 'CTRL_X':
            await self._show_status_msg("^X-")
            sub = await self._read_key(timeout=5.0)
            if sub is None:
                await self._draw_screen()
                return None
            sub = sub.upper()
            if sub == 'S':
                return 'SAVE'
            elif sub == 'A':
                # Confirm abort
                await self._show_status_msg("Abort - are you sure? (Y/N)")
                conf = await self._read_key(timeout=10.0)
                if conf and conf.upper() == 'Y':
                    return 'ABORT'
                await self._draw_screen()
                return None
            elif sub == 'L':
                return 'TO_LINE'
            else:
                await self._draw_screen()
                return None
        
        # Navigation
        elif key == 'CTRL_B':
            self._col = 0
            await self._update_cursor()
        elif key == 'CTRL_N':
            self._col = len(self._get_line(self._row))
            await self._update_cursor()
        elif key == 'CTRL_U':
            self._row = 0
            self._col = 0
            self._top_line = 0
            await self._draw_screen()
        elif key == 'CTRL_O':
            self._row = max(0, len(self._lines) - 1)
            self._col = len(self._get_line(self._row))
            self._ensure_visible()
            await self._draw_screen()
        elif key == 'CTRL_A':
            await self._page_up()
        elif key == 'CTRL_Z':
            await self._page_down()
        elif key == 'UP':
            await self._move_up()
        elif key == 'DOWN':
            await self._move_down()
        elif key == 'LEFT':
            await self._move_left()
        elif key == 'RIGHT':
            await self._move_right()
        elif key == 'PGUP':
            await self._page_up()
        elif key == 'PGDN':
            await self._page_down()
        elif key == 'HOME':
            self._col = 0
            await self._update_cursor()
        elif key == 'END':
            self._col = len(self._get_line(self._row))
            await self._update_cursor()
        
        # Editing
        elif key == 'CTRL_K':
            await self._kill_to_eol()
        elif key == 'CTRL_V':
            await self._draw_screen()
        elif key == 'BS':
            await self._backspace()
        elif key == 'ENTER':
            await self._insert_newline()
        elif len(key) == 1 and key.isprintable():
            await self._insert_char(key)
        
        return None
    
    # ──────────────────────────────────────────────────────────────────────────
    # Text buffer operations
    # ──────────────────────────────────────────────────────────────────────────
    
    def _get_line(self, row: int) -> str:
        """Return the line at the given row, or empty string."""
        if 0 <= row < len(self._lines):
            return self._lines[row]
        return ""
    
    def _set_line(self, row: int, text: str):
        """Set the line at the given row."""
        while len(self._lines) <= row:
            self._lines.append("")
        self._lines[row] = text
    
    def _get_text(self) -> str:
        """Return the entire text buffer as a single string."""
        return "\n".join(self._lines)
    
    async def _insert_char(self, ch: str):
        """Insert a character at the cursor position."""
        line = self._get_line(self._row)
        new_line = line[:self._col] + ch + line[self._col:]
        
        # Auto word-wrap if line exceeds 74 chars
        if len(new_line) > 74:
            await self._wrap_current_line()
        else:
            self._set_line(self._row, new_line)
            self._col += 1
            await self._redraw_line(self._row)
            await self._update_cursor()
    
    async def _backspace(self):
        """Delete the character before the cursor."""
        if self._col > 0:
            line = self._get_line(self._row)
            new_line = line[:self._col - 1] + line[self._col:]
            self._set_line(self._row, new_line)
            self._col -= 1
            await self._redraw_line(self._row)
            await self._update_cursor()
        elif self._row > 0:
            # Join with previous line
            prev = self._get_line(self._row - 1)
            curr = self._get_line(self._row)
            self._col = len(prev)
            self._set_line(self._row - 1, prev + curr)
            del self._lines[self._row]
            self._row -= 1
            self._ensure_visible()
            await self._draw_screen()
    
    async def _insert_newline(self):
        """Insert a newline at the cursor."""
        if len(self._lines) >= self._max_lines:
            await self._show_status_msg("Maximum lines reached")
            await asyncio.sleep(1)
            await self._draw_screen()
            return
        
        line = self._get_line(self._row)
        left = line[:self._col]
        right = line[self._col:]
        self._set_line(self._row, left)
        self._lines.insert(self._row + 1, right)
        self._row += 1
        self._col = 0
        self._ensure_visible()
        await self._draw_screen()
    
    async def _kill_to_eol(self):
        """Delete from cursor to end of line."""
        line = self._get_line(self._row)
        new_line = line[:self._col]
        self._set_line(self._row, new_line)
        await self._redraw_line(self._row)
        await self._update_cursor()
    
    async def _wrap_current_line(self):
        """Word-wrap the current line if it's too long."""
        line = self._get_line(self._row)
        if len(line) <= 74:
            return
        
        # Find last space at or before col 74
        break_at = line.rfind(' ', 0, 74)
        if break_at <= 0:
            break_at = 74
        
        left = line[:break_at].rstrip()
        right = line[break_at:].lstrip()
        
        self._set_line(self._row, left)
        if self._row + 1 < len(self._lines):
            # Prepend to next line
            next_line = self._get_line(self._row + 1)
            self._set_line(self._row + 1, right + ' ' + next_line if next_line else right)
        else:
            self._lines.insert(self._row + 1, right)
        
        # Adjust cursor
        if self._col > len(left):
            self._col = self._col - len(left) - 1
            self._row += 1
        
        self._ensure_visible()
        await self._draw_screen()
    
    # ──────────────────────────────────────────────────────────────────────────
    # Cursor movement
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _move_up(self):
        if self._row > 0:
            self._row -= 1
            self._col = min(self._col, len(self._get_line(self._row)))
            self._ensure_visible()
            if self._top_line != self._get_top():
                await self._draw_screen()
            else:
                await self._update_cursor()
    
    async def _move_down(self):
        if self._row < len(self._lines) - 1:
            self._row += 1
            self._col = min(self._col, len(self._get_line(self._row)))
            self._ensure_visible()
            if self._top_line != self._get_top():
                await self._draw_screen()
            else:
                await self._update_cursor()
    
    async def _move_left(self):
        if self._col > 0:
            self._col -= 1
        elif self._row > 0:
            self._row -= 1
            self._col = len(self._get_line(self._row))
            self._ensure_visible()
            if self._top_line != self._get_top():
                await self._draw_screen()
            else:
                await self._update_cursor()
        else:
            await self._update_cursor()
    
    async def _move_right(self):
        line = self._get_line(self._row)
        if self._col < len(line):
            self._col += 1
        elif self._row < len(self._lines) - 1:
            self._row += 1
            self._col = 0
            self._ensure_visible()
            if self._top_line != self._get_top():
                await self._draw_screen()
            else:
                await self._update_cursor()
        else:
            await self._update_cursor()
    
    async def _page_up(self):
        self._row = max(0, self._row - self._edit_rows)
        self._col = min(self._col, len(self._get_line(self._row)))
        self._ensure_visible()
        await self._draw_screen()
    
    async def _page_down(self):
        self._row = min(len(self._lines) - 1, self._row + self._edit_rows)
        self._col = min(self._col, len(self._get_line(self._row)))
        self._ensure_visible()
        await self._draw_screen()
    
    def _ensure_visible(self):
        """Adjust _top_line so cursor row is visible."""
        if self._row < self._top_line:
            self._top_line = self._row
        elif self._row >= self._top_line + self._edit_rows:
            self._top_line = self._row - self._edit_rows + 1
    
    def _get_top(self) -> int:
        """Calculate what _top_line should be for current cursor."""
        if self._row < self._top_line:
            return self._row
        elif self._row >= self._top_line + self._edit_rows:
            return self._row - self._edit_rows + 1
        return self._top_line
    
    # ──────────────────────────────────────────────────────────────────────────
    # Screen drawing
    # ──────────────────────────────────────────────────────────────────────────
    
    async def _draw_screen(self):
        """Redraw the entire screen."""
        out = [_VE_CLR, _VE_BG]
        
        # Row 1: Status line
        status = self._make_status_line()
        out.append(_goto_ve(1, 1) + _VE_STATUS + status + _VE_BG)
        
        # Rows 2 to height: Edit area
        for screen_row in range(self._edit_rows):
            text_row = self._top_line + screen_row
            term_row = screen_row + 2
            out.append(_goto_ve(term_row, 1))
            if text_row < len(self._lines):
                line = self._lines[text_row][:self._width - 1]
                out.append(line.ljust(self._width - 1))
            else:
                out.append(' ' * (self._width - 1))
        
        out.append(_VE_RST)
        await self._s.send(''.join(out).encode('latin-1', errors='replace'))
        await self._update_cursor()
    
    async def _redraw_line(self, text_row: int):
        """Redraw a single line in the edit area."""
        screen_row = text_row - self._top_line
        if 0 <= screen_row < self._edit_rows:
            term_row = screen_row + 2
            line = self._get_line(text_row)[:self._width - 1]
            out = (_goto_ve(term_row, 1) + _VE_BG + 
                   line.ljust(self._width - 1) + _VE_RST)
            await self._s.send(out.encode('latin-1', errors='replace'))
    
    async def _update_cursor(self):
        """Move the terminal cursor to match editor cursor position."""
        screen_row = self._row - self._top_line
        term_row = screen_row + 2
        term_col = self._col + 1
        out = _goto_ve(term_row, term_col)
        await self._s.send(out.encode())
    
    def _make_status_line(self) -> str:
        """Build the status line text."""
        left = f"Subject: {self._subject}"
        if self._addressee:
            left += f"  To: {self._addressee}"
        right = f"Row:{self._row + 1} Col:{self._col + 1}"
        gap = self._width - len(left) - len(right)
        if gap < 1:
            gap = 1
        return left + ' ' * gap + right
    
    async def _show_status_msg(self, msg: str):
        """Temporarily show a message in the status line."""
        out = (_goto_ve(1, 1) + _VE_STATUS + 
               msg[:self._width].ljust(self._width) + _VE_BG)
        await self._s.send(out.encode('latin-1', errors='replace'))
