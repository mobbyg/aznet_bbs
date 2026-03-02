"""
server/systext.py — CNet SysText File Loader and Renderer

Systext files are the human-readable display files CNet uses for every
screen, prompt, and help text.  Unlike bbstext (which is a binary indexed
array), systext files are named files read by filename from a directory.

FORMATS USED IN SYSTEXT FILES
──────────────────────────────
1. CNet MCI codes  (0x19 + command):
   Same as bbstext — colour, newline, bold, etc.
   Handled by server/terminal.render_mci().

2. CNet DC1 codes  (0x11 + content + '}'):
   Used for variable substitution and control directives.

   Variable substitutions — embedded in display text:
     v1    handle (user's handle)
     v01   BBS name
     v11   last-call date/time string
     v12   current date/time string
     v46   time-of-day word that follows "Good " in sys.welcome
           ("morning", "afternoon", "evening", "night")
     v47   total caller count (as string)
     v48   current subboard name
     v49   current subboard description

   Control directives — skipped / stubbed:
     #N program    door / external program call
     $0 text       sound effect
     tN #N         pagination timer (more-prompt threshold)
     jeN           jump-to-end at line N

VARIABLE DICT
─────────────
Callers pass a dict keyed by variable name string:
    {
        'v1':  handle,
        'v01': bbs_name,
        'v11': last_call_str,
        'v12': now_str,
        'v46': greeting,
        'v47': str(call_count),
        'v48': subboard_name,
        'v49': subboard_desc,
    }

Any missing key falls back to an empty string so the file always renders.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .terminal import render_mci, DC1_ESCAPE, MCI_ESCAPE

# ─────────────────────────────────────────────────────────────────────────────
# DC1 variable name pattern
# ─────────────────────────────────────────────────────────────────────────────

# Matches variable references like v1, v01, v12, v46, v47, v48, v49
_VAR_RE = re.compile(r'^v0*(\d+)$', re.IGNORECASE)

# Directives to skip (first char of DC1 content)
_SKIP_PREFIXES = ('#', '$', 't', 'j')


def _build_default_variables() -> dict[str, str]:
    """Return a variables dict pre-populated with safe defaults."""
    now = datetime.now()
    hour = now.hour
    # v46 is the time-of-day word that follows "Good " in sys.welcome.
    # The word "Good" is literal in the systext file; v46 completes it.
    if hour < 12:
        greeting = "morning"
    elif hour < 17:
        greeting = "afternoon"
    elif hour < 21:
        greeting = "evening"
    else:
        greeting = "night"

    return {
        'v1':  '',
        'v01': 'ANet BBS',
        'v11': 'Unknown',
        'v12': now.strftime('%A, %B %d %Y  %I:%M %p'),
        'v46': greeting,
        'v47': '0',
        'v48': '',
        'v49': '',
    }


def _lookup_var(name: str, variables: dict[str, str]) -> str:
    """
    Look up a DC1 variable name in the variables dict.

    Tries the exact name first, then normalises by stripping leading zeros
    in the numeric part so 'v01' and 'v1' both resolve.
    """
    name = name.strip().lower()
    if name in variables:
        return variables[name]
    # Strip leading zeros: v01 -> v1, v047 -> v47
    m = _VAR_RE.match(name)
    if m:
        normalised = f'v{m.group(1)}'
        if normalised in variables:
            return variables[normalised]
    return ''


def _render_dc1_segment(content: str, variables: dict[str, str]) -> bytes:
    """
    Render the content of one DC1 code (everything between 0x11 and '}').
    Returns bytes to insert into the output stream, or b'' to skip.
    """
    if not content:
        return b''
    # Skip control directives
    if content[0] in _SKIP_PREFIXES:
        return b''
    # Variable substitution
    value = _lookup_var(content, variables)
    return value.encode('latin-1', errors='replace')


def split_pages(raw: bytes) -> list[bytes]:
    """
    Split a raw systext file at CNet pagination markers (\\x11t... directives).

    CNet embeds  \\x11tNN #0}  at the point where the "Want to see more [Yes]?"
    text appears.  That prompt text is literal bytes *before* the \\x11t marker,
    so each chunk already contains its own "more?" question.  After rendering
    the chunk the caller should pause for input before sending the next one.

    Also strips the companion \\x11je ... } and \\x11ja ... } jump directives
    that immediately follow \\x11t, since they are branching instructions that
    have no meaning outside CNet's native interpreter.

    Returns a list of one or more byte chunks.  A single-element list means
    the file has no pagination markers.
    """
    pages: list[bytes] = []
    current = bytearray()
    i = 0
    n = len(raw)

    while i < n:
        b = raw[i]
        if b == DC1_ESCAPE:
            i += 1
            start = i
            while i < n and raw[i] != ord('}'):
                i += 1
            content = raw[start:i].decode('latin-1', errors='replace')
            i += 1  # consume '}'

            # Pagination break — save current chunk and start a new one
            if content and content[0] == 't':
                pages.append(bytes(current))
                current = bytearray()
            # Jump directives (je / ja / j*) — silently discard
            elif content and content[0] == 'j':
                pass
            else:
                # Preserve all other DC1 codes intact for render_systext()
                current += b'\x11' + content.encode('latin-1', errors='replace') + b'}'
        else:
            current.append(b)
            i += 1

    if current:
        pages.append(bytes(current))

    return pages if pages else [raw]


def render_systext(raw: bytes, variables: dict[str, str]) -> bytes:
    """
    Full two-pass render for a systext file chunk:
      1. Walk bytes handling DC1 (0x11 ... '}') variable substitutions.
      2. Pass each line through render_mci() for MCI colour/newline codes.

    Lines are split on 0x0A.  The MCI renderer already adds ANSI_RESET at
    the end of every line so colour cannot bleed between lines.

    Returns assembled ANSI bytes ready to send to the telnet writer.
    """
    # --- Pass 1: resolve DC1 variables ---
    step1 = bytearray()
    i = 0
    n = len(raw)

    while i < n:
        b = raw[i]
        if b == DC1_ESCAPE:
            # Consume content up to '}'
            i += 1
            start = i
            while i < n and raw[i] != ord('}'):
                i += 1
            content = raw[start:i].decode('latin-1', errors='replace')
            step1 += _render_dc1_segment(content, variables)
            i += 1  # consume '}'
        else:
            step1.append(b)
            i += 1

    # --- Pass 2: render MCI codes line by line ---
    # NOTE: We do NOT honour the quiet flag here.  In bbstext, q1 marks an
    # entire record as log-only.  In systext files, q1 sometimes appears
    # *after* visible text (e.g. sys.nuser) where it has a different meaning.
    # All content is output; the caller decides what gets sent to the user.
    out = bytearray()
    for line in bytes(step1).split(b'\n'):
        line = line.rstrip(b'\r')
        result = render_mci(line)
        out += result.output
        if result.newlines == 0:
            # Line had no embedded \x19nN — add a natural line break
            out += b'\r\n'

    return bytes(out)


# ─────────────────────────────────────────────────────────────────────────────
# SystextFile — directory-based loader
# ─────────────────────────────────────────────────────────────────────────────

class SystextFile:
    """
    Loads and renders named CNet systext files from a directory.

    Usage:
        st = SystextFile(config.SYSTEXT_DIR)

        # Basic render (no variable substitution):
        await writer.write(st.render('sys.start'))

        # With variables (for sys.welcome etc.):
        vars = st.make_variables(
            handle     = user.handle,
            last_call  = user.last_call,
            call_count = user.call_count,
        )
        await writer.write(st.render('sys.welcome', vars))
    """

    def __init__(self, systext_dir: str | Path):
        self._dir = Path(systext_dir)
        self._cache: dict[str, bytes] = {}

    # ── File existence ───────────────────────────────────────────────────────

    def exists(self, filename: str) -> bool:
        """Return True if the named systext file is present on disk."""
        return (self._dir / filename).is_file()

    # ── Raw bytes access ─────────────────────────────────────────────────────

    def _raw(self, filename: str) -> bytes:
        """Return raw bytes for a systext file, with simple in-memory cache."""
        if filename not in self._cache:
            path = self._dir / filename
            if path.is_file():
                self._cache[filename] = path.read_bytes()
            else:
                self._cache[filename] = b''
        return self._cache[filename]

    # ── Rendered output ──────────────────────────────────────────────────────

    def render(
        self,
        filename: str,
        variables: Optional[dict[str, str]] = None,
    ) -> bytes:
        """
        Render a named systext file to ANSI bytes.

        Returns b'' if the file does not exist.
        """
        raw = self._raw(filename)
        if not raw:
            return b''
        vars_ = _build_default_variables()
        if variables:
            vars_.update(variables)
        return render_systext(raw, vars_)

    def render_pages(
        self,
        filename: str,
        variables: Optional[dict[str, str]] = None,
    ) -> list[bytes]:
        """
        Render a named systext file split into pages at CNet pagination markers.

        Returns a list of rendered ANSI byte chunks.  If the file has no
        pagination markers the list has exactly one element (same as render()).

        The caller is responsible for prompting the user between pages —
        the "Want to see more [Yes]?" text is already included at the end
        of each chunk that precedes a break.
        """
        raw = self._raw(filename)
        if not raw:
            return [b'']
        vars_ = _build_default_variables()
        if variables:
            vars_.update(variables)
        chunks = split_pages(raw)
        return [render_systext(chunk, vars_) for chunk in chunks]

    # ── Variable builder ─────────────────────────────────────────────────────

    @staticmethod
    def make_variables(
        handle: str = '',
        bbs_name: str = 'ANet BBS',
        last_call: Optional[str] = None,
        call_count: int = 0,
        subboard_name: str = '',
        subboard_desc: str = '',
    ) -> dict[str, str]:
        """
        Build a variables dict for systext rendering from common session data.

        Args:
            handle        : logged-in user's handle
            bbs_name      : name of this BBS (from Config.BBS_NAME)
            last_call     : formatted last-call date string, or None
            call_count    : total call count for this user
            subboard_name : current subboard name (for v48)
            subboard_desc : current subboard description (for v49)
        """
        now  = datetime.now()
        hour = now.hour
        # v46 is the time-of-day word that follows the literal "Good " in sys.welcome
        if hour < 12:
            greeting = "morning"
        elif hour < 17:
            greeting = "afternoon"
        elif hour < 21:
            greeting = "evening"
        else:
            greeting = "night"

        return {
            'v1':  handle,
            'v01': bbs_name,
            'v11': last_call or 'first time',
            'v12': now.strftime('%A, %B %d %Y  %I:%M %p'),
            'v46': greeting,
            'v47': str(call_count),
            'v48': subboard_name,
            'v49': subboard_desc,
        }
