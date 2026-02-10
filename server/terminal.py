"""
server/terminal.py — MCI Code Parser and ANSI Terminal Renderer

CNet used a system called MCI (Message Command Interpreter) to embed display
commands inside bbstext.txt strings. The escape character is 0x19 (decimal 25,
written as \x19 in Python). Everything after \x19 until whitespace is a command.

MCI codes we handle (from analysis of bbstext.txt):
  \x19n1       → CR+LF newline
  \x19nN       → N newlines
  \x19cN       → Set foreground color (N = 0–9, a–f for 0–15)
  \x19f1       → Reset text attributes (bold/color off)
  \x19q0       → Quiet OFF  (resume normal output)
  \x19q1       → Quiet ON   (suppress output — used for log-only strings)
  \x19hc       → Clear screen (home + clear)
  \x19h7       → Bold/highlight on
  \x19i0       → Input flag: no default (user must type something)
  \x19?0       → Default answer: No
  \x19?1       → Default answer: Yes

ANSI color mapping (CNet color → ANSI SGR code):
  CNet uses 0–15.  0–7 map to standard ANSI foregrounds (30–37).
  8–15 map to bright ANSI foregrounds (90–97).
"""

import re
from pathlib import Path

MCI_ESCAPE = b'\x19'   # The CNet MCI escape byte

# --------------------------------------------------------------------------
# ANSI escape sequence builders
# --------------------------------------------------------------------------

ANSI_RESET  = b'\x1b[0m'
ANSI_BOLD   = b'\x1b[1m'
ANSI_CLEAR  = b'\x1b[2J\x1b[H'   # Erase screen + move cursor to top-left

# Standard ANSI foreground colors (CNet 0–7 → ANSI 30–37)
# Bright variants      (CNet 8–15 → ANSI 90–97)
_ANSI_FG = {
    0:  b'\x1b[30m',   # Black
    1:  b'\x1b[31m',   # Red
    2:  b'\x1b[32m',   # Green
    3:  b'\x1b[33m',   # Yellow
    4:  b'\x1b[34m',   # Blue
    5:  b'\x1b[35m',   # Magenta
    6:  b'\x1b[36m',   # Cyan
    7:  b'\x1b[37m',   # White
    8:  b'\x1b[90m',   # Bright Black (dark grey)
    9:  b'\x1b[91m',   # Bright Red
    10: b'\x1b[92m',   # Bright Green
    11: b'\x1b[93m',   # Bright Yellow
    12: b'\x1b[94m',   # Bright Blue
    13: b'\x1b[95m',   # Bright Magenta
    14: b'\x1b[96m',   # Bright Cyan
    15: b'\x1b[97m',   # Bright White
}


def ansi_color(n: int) -> bytes:
    """Return the ANSI escape bytes for CNet color index n (0–15)."""
    return _ANSI_FG.get(n, b'\x1b[37m')


# --------------------------------------------------------------------------
# MCI processing
# --------------------------------------------------------------------------

class MCIResult:
    """
    Holds the result of rendering a bbstext.txt line through the MCI parser.

    Attributes:
        output   : bytes ready to send to the client terminal
        quiet    : True if \x19q1 was active — caller should not transmit this
        newlines : count of \x19n codes encountered (useful for spacing logic)
    """
    __slots__ = ('output', 'quiet', 'newlines')

    def __init__(self, output: bytes, quiet: bool, newlines: int):
        self.output   = output
        self.quiet    = quiet
        self.newlines = newlines


def render_mci(raw: str, substitutions: dict | None = None) -> MCIResult:
    """
    Parse a raw bbstext.txt line, replace % format codes with substitutions,
    and convert all MCI codes to ANSI escape bytes.

    Args:
        raw           : the raw string from bbstext.txt (may contain \x19 codes)
        substitutions : optional dict for %-style format replacement before
                        MCI parsing.  Keys are positional: {0: value, 1: value}
                        Maps to the C-style %s / %d placeholders in order.

    Returns:
        MCIResult with .output (bytes to send) and metadata.

    Example:
        render_mci("Port %-2d   \x19c6%s", {0: 3, 1: "Waiting for call"})
        → sends "Port  3   " in cyan + "Waiting for call"
    """
    if substitutions:
        # Replace %d and %s placeholders in order of appearance
        # We do a simple sequential replacement rather than named args to match
        # the original CNet C-style printf formatting.
        raw = _apply_printf_subs(raw, substitutions)

    result_bytes = bytearray()
    quiet        = False
    newline_count = 0
    i = 0
    text = raw  # working string

    while i < len(text):
        ch = text[i]

        if ch == '\x19':
            # MCI escape — consume the next character(s) to identify the command
            i += 1
            if i >= len(text):
                break

            cmd = text[i]
            i += 1

            if cmd == 'n':
                # \x19nN  — N newlines
                count_str = ''
                while i < len(text) and text[i].isdigit():
                    count_str += text[i]
                    i += 1
                count = int(count_str) if count_str else 1
                result_bytes += b'\r\n' * count
                newline_count += count

            elif cmd == 'c':
                # \x19cN  — color.  N is a single hex digit (0–9, a–f)
                if i < len(text):
                    hex_digit = text[i]
                    i += 1
                    try:
                        color_index = int(hex_digit, 16)
                        result_bytes += ansi_color(color_index)
                    except ValueError:
                        pass  # Unknown color, skip

            elif cmd == 'f':
                # \x19fN  — formatting reset
                # Consume the digit but just emit a reset
                if i < len(text) and text[i].isdigit():
                    i += 1
                result_bytes += ANSI_RESET

            elif cmd == 'q':
                # \x19q0 = quiet off, \x19q1 = quiet on
                if i < len(text):
                    flag = text[i]
                    i += 1
                    quiet = (flag == '1')

            elif cmd == 'h':
                # \x19hc = clear screen, \x19h7 = bold on
                if i < len(text):
                    sub = text[i]
                    i += 1
                    if sub == 'c':
                        result_bytes += ANSI_CLEAR
                    elif sub == '7':
                        result_bytes += ANSI_BOLD

            elif cmd == 'i':
                # \x19i0/i1 — input flag, informational only (used by session logic)
                if i < len(text) and text[i].isdigit():
                    i += 1
                # Not rendered as output

            elif cmd == '?':
                # \x19?0 / \x19?1 — default answer, informational only
                if i < len(text) and text[i].isdigit():
                    i += 1
                # Not rendered as output

            else:
                # Unknown MCI code — emit as-is so nothing is silently lost
                result_bytes += ('\x19' + cmd).encode('latin-1', errors='replace')

        else:
            # Regular character — encode as latin-1 (safe superset of ASCII that
            # matches what old BBS software expected)
            result_bytes += ch.encode('latin-1', errors='replace')
            i += 1

    # Always reset color at end of line so we don't bleed into the next line
    result_bytes += ANSI_RESET

    return MCIResult(
        output   = bytes(result_bytes),
        quiet    = quiet,
        newlines = newline_count,
    )


def _apply_printf_subs(template: str, subs: dict) -> str:
    """
    Replace C-style %s / %d / %-Nd placeholders in order with the values
    in the subs dict (keyed 0, 1, 2 …).

    This is intentionally simple: it replaces each format spec from left to
    right with the next value.  It handles %-2d style width specs too.
    """
    # Regex matches %[flags][width]s or %[flags][width]d
    pattern = re.compile(r'%-?\d*[sd]')
    idx = 0
    result = []
    last = 0
    for m in pattern.finditer(template):
        result.append(template[last:m.start()])
        if idx in subs:
            fmt = m.group()           # e.g. "%-2d" or "%s"
            try:
                result.append(fmt % subs[idx])
            except (TypeError, ValueError):
                result.append(str(subs[idx]))
        else:
            result.append(m.group())  # leave untouched if no sub provided
        idx += 1
        last = m.end()
    result.append(template[last:])
    return ''.join(result)


# --------------------------------------------------------------------------
# BBSText loader
# --------------------------------------------------------------------------

class BBSText:
    """
    Loads bbstext.txt into memory, keyed by 1-based line number.
    Provides render() to get ANSI bytes for any line, with optional substitutions.

    Usage:
        bbs = BBSText("data/bbstext.txt")
        output = bbs.render(30)           # "Enter your handle.\\r\\n: "
        output = bbs.render(15, {0: 3, 1: "Waiting for call"})
    """

    def __init__(self, path: str | Path):
        self._lines: dict[int, str] = {}
        self._load(Path(path))

    def _load(self, path: Path) -> None:
        with open(path, 'r', encoding='latin-1') as fh:
            for lineno, raw in enumerate(fh, start=1):
                # Strip the trailing newline only — preserve internal content
                self._lines[lineno] = raw.rstrip('\n').rstrip('\r')

    def raw(self, lineno: int) -> str:
        """Return the raw (un-rendered) string for a line number."""
        return self._lines.get(lineno, '')

    def render(self, lineno: int, substitutions: dict | None = None) -> bytes:
        """
        Render line `lineno` through the MCI parser.
        Returns bytes ready to write to a telnet StreamWriter.
        Quiet lines return b'' (they are log-only strings).
        """
        raw = self._lines.get(lineno, '')
        if not raw:
            return b''
        result = render_mci(raw, substitutions)
        if result.quiet:
            return b''
        return result.output
