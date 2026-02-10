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
        await self._session.send_line(b"  .S=Save  .A=Abort  .L=List  .D=Del last  .H=Help\r\n")

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
