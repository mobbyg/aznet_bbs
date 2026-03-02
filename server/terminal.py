"""
server/terminal.py — CNet MCI Parser and ANSI Terminal Renderer

CNet's Message Command Interpreter (MCI) uses 0x19 (decimal 25) as its
escape byte.  Every sequence begins with 0x19 followed by a one-letter
command code and usually one parameter character.

This module targets CNet v5 bbstext format.  v5 strings are COMPLETE —
unlike v3 where the C source hardcoded the highlighted first letter and the
bbstext record started mid-word, v5 stores the full string.  The old
"first-letter capitalise" workaround has been removed.

CNet v5 also uses a second escape system in systext files: DC1 (0x11).
The DC1 sequences are handled by server/systext.py.  If a DC1 byte is
encountered here it is silently skipped so bbstext rendering is unaffected.

MCI codes handled (0x19 + code char + optional parameter):
  n N     -> N x CR+LF newline
  c N     -> Set ANSI foreground colour  (N = hex digit 0-f)
  f N     -> Reset all text attributes
  q 0/1   -> Quiet OFF / ON  (suppress - log-only strings use q1)
  h 7/c   -> h7 = bold on,  hc = clear screen (home + erase)
  u 0/1   -> Underline OFF / ON
  o 0/1   -> Bold (over-bright) OFF / ON
  r 0/1   -> Reverse video OFF / ON
  > N     -> Cursor forward N columns
  < N     -> Cursor back N columns
  @ N     -> Column positioning hint (informational)
  z N     -> Column-width / centering hint (informational)
  : N     -> Indent / alignment hint (informational)
  i 0/1   -> Input flag marker (informational, not rendered)
  ? 0/1   -> Default-answer marker  (informational, not rendered)
  a N     -> Attribute (treat as reset)

ANSI colour mapping  (CNet 0-15):
  0-7   -> standard ANSI foreground  \x1b[3Nm
  8-15  -> bright  ANSI foreground   \x1b[9Nm
"""

from pathlib import Path
import re as _re

MCI_ESCAPE = 0x19    # CNet MCI escape byte  (decimal 25)
DC1_ESCAPE = 0x11    # CNet DC1 escape byte  (decimal 17) -- handled by systext.py

ANSI_RESET     = b'\x1b[0m'
ANSI_BOLD      = b'\x1b[1m'
ANSI_BOLD_OFF  = b'\x1b[22m'
ANSI_UNDER     = b'\x1b[4m'
ANSI_UNDER_OFF = b'\x1b[24m'
ANSI_REV       = b'\x1b[7m'
ANSI_REV_OFF   = b'\x1b[27m'
ANSI_CLEAR     = b'\x1b[2J\x1b[H'

_ANSI_FG = {
    0:  b'\x1b[30m',
    1:  b'\x1b[31m',
    2:  b'\x1b[32m',
    3:  b'\x1b[33m',
    4:  b'\x1b[34m',
    5:  b'\x1b[35m',
    6:  b'\x1b[36m',
    7:  b'\x1b[37m',
    8:  b'\x1b[90m',
    9:  b'\x1b[91m',
    10: b'\x1b[92m',
    11: b'\x1b[93m',
    12: b'\x1b[94m',
    13: b'\x1b[95m',
    14: b'\x1b[96m',
    15: b'\x1b[97m',
}


def _ansi_color(n: int) -> bytes:
    return _ANSI_FG.get(n & 0xF, b'\x1b[37m')


class MCIResult:
    """
    Result of rendering a bbstext record through the MCI parser.

    Attributes:
        output   : bytes ready to send to the client terminal
        quiet    : True if the record is q1-suppressed (log-only string)
        newlines : count of CR+LF sequences emitted
    """
    __slots__ = ('output', 'quiet', 'newlines')

    def __init__(self, output: bytes, quiet: bool, newlines: int):
        self.output   = output
        self.quiet    = quiet
        self.newlines = newlines


def render_mci(raw: bytes) -> MCIResult:
    """
    Walk a raw bbstext record (bytes) and convert CNet MCI codes to ANSI bytes.

    v5 strings are complete -- no first-letter capitalisation is applied.
    DC1 (0x11) sequences are silently consumed.
    """
    out           = bytearray()
    quiet         = False
    newline_count = 0
    i             = 0
    n             = len(raw)

    while i < n:
        b = raw[i]

        # DC1 (0x11) -- skip until closing '}'
        if b == DC1_ESCAPE:
            i += 1
            while i < n and raw[i] != ord('}'):
                i += 1
            i += 1
            continue

        # MCI (0x19)
        if b == MCI_ESCAPE:
            i += 1
            if i >= n:
                break
            cmd = raw[i]
            i += 1

            if cmd == ord('n'):
                digits = []
                while i < n and chr(raw[i]).isdigit():
                    digits.append(chr(raw[i]))
                    i += 1
                count = int(''.join(digits)) if digits else 1
                out += b'\r\n' * count
                newline_count += count

            elif cmd == ord('c'):
                if i < n:
                    try:
                        out += _ansi_color(int(chr(raw[i]), 16))
                    except ValueError:
                        pass
                    i += 1

            elif cmd == ord('f'):
                if i < n and chr(raw[i]).isdigit():
                    i += 1
                out += ANSI_RESET

            elif cmd == ord('a'):
                if i < n and chr(raw[i]).isdigit():
                    i += 1
                out += ANSI_RESET

            elif cmd == ord('q'):
                if i < n:
                    quiet = (raw[i] == ord('1'))
                    i += 1

            elif cmd == ord('h'):
                if i < n:
                    sub = chr(raw[i])
                    i += 1
                    if sub == '7':
                        out += ANSI_BOLD
                    elif sub == 'c':
                        out += ANSI_CLEAR

            elif cmd == ord('u'):
                if i < n:
                    out += ANSI_UNDER if raw[i] == ord('1') else ANSI_UNDER_OFF
                    i += 1

            elif cmd == ord('o'):
                if i < n:
                    out += ANSI_BOLD if raw[i] == ord('1') else ANSI_BOLD_OFF
                    i += 1

            elif cmd == ord('r'):
                if i < n:
                    out += ANSI_REV if raw[i] == ord('1') else ANSI_REV_OFF
                    i += 1

            elif cmd == ord('>'):
                digits = []
                while i < n and chr(raw[i]).isdigit():
                    digits.append(chr(raw[i]))
                    i += 1
                count = int(''.join(digits)) if digits else 1
                out += f'\x1b[{count}C'.encode()

            elif cmd == ord('<'):
                digits = []
                while i < n and chr(raw[i]).isdigit():
                    digits.append(chr(raw[i]))
                    i += 1
                count = int(''.join(digits)) if digits else 1
                out += f'\x1b[{count}D'.encode()

            elif cmd in (ord('i'), ord('?'), ord('g'), ord('w'), ord('b'), ord('a')):
                # Input flag / get-input / wait / bell / alert — informational only
                if i < n and chr(raw[i]).isdigit():
                    i += 1

            elif cmd in (ord('z'), ord(':'), ord('@')):
                # Column/alignment hints -- informational only
                if i < n and chr(raw[i]).isdigit():
                    i += 1

            else:
                # Unknown -- pass through unchanged
                out.append(MCI_ESCAPE)
                out.append(cmd)

            continue

        # Regular byte — always emit; q0/q1 control Telnet echo, not our output
        out.append(b)
        i += 1

    out += ANSI_RESET
    return MCIResult(bytes(out), quiet, newline_count)


class BBSText:
    """
    Loads the binary CNet bbstext file into memory, keyed by 0-based record
    index (matching CNet's native numbering).

    Usage:
        bbs = BBSText("data/bbstext")
        output_bytes = bbs.render(18)
        output_bytes = bbs.render(14, {0: node_id, 1: "ANet BBS"})
    """

    def __init__(self, path: str | Path):
        self._records: dict[int, bytes] = {}
        self._load(Path(path))

    def _load(self, path: Path) -> None:
        raw = path.read_bytes()
        for idx, record in enumerate(raw.split(b'\n')):
            self._records[idx] = record.rstrip(b'\r')

    def raw(self, record: int) -> bytes:
        return self._records.get(record, b'')

    def render(self, record: int, substitutions: dict | None = None) -> bytes:
        """
        Render record N through the MCI parser and return ANSI bytes.

        Quiet mode (q0/q1) is respected character-by-character during
        rendering, so records like rec 67 that use q0→text→q1 are displayed
        correctly.  Pure log-only records (no text ever in non-quiet mode)
        produce only an ANSI reset, which is harmless.
        """
        raw = self._records.get(record, b'')
        if not raw:
            return b''
        if substitutions:
            raw = _apply_printf_subs(raw, substitutions)
        return render_mci(raw).output


class BBSMenu:
    """
    Loads the CNet bbsmenu file.

    Format:
        N; Context description
           CMD, ALIAS~optional help text
           CMD2

    Context numbers:
        1  = Maintenance
        2  = Available everywhere
        3  = Main prompt
        4  = Base/Uploads (subboard)
        5  = Respond or Pass
        10 = Editor (empty or with text)
        11 = Editor (with text only)
    """

    def __init__(self, path: str | Path):
        self._contexts: dict[int, list[tuple[str, list[str], str]]] = {}
        self._descriptions: dict[int, str] = {}
        self._load(Path(path))

    def _load(self, path: Path) -> None:
        current_ctx = None
        for line in path.read_text(encoding='latin-1').splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if line[0].isdigit() and ';' in line:
                parts = line.split(';', 1)
                try:
                    current_ctx = int(parts[0].strip())
                    self._descriptions[current_ctx] = parts[1].strip()
                    self._contexts[current_ctx] = []
                except ValueError:
                    pass
            elif line[0] in (' ', '\t') and current_ctx is not None:
                entry = stripped
                desc = ''
                if '~' in entry:
                    entry, desc = entry.split('~', 1)
                raw_cmds = [c.strip() for c in entry.split(',')]
                primary = raw_cmds[0].upper() if raw_cmds else ''
                aliases = [c.upper() for c in raw_cmds[1:] if c.strip()]
                if primary:
                    self._contexts[current_ctx].append((primary, aliases, desc))

    def commands(self, context: int) -> list[tuple[str, list[str], str]]:
        return self._contexts.get(context, [])

    def context_name(self, context: int) -> str:
        return self._descriptions.get(context, f'Context {context}')

    def resolve(self, typed: str, *extra_contexts: int) -> str | None:
        """
        Given a string the user typed, return the canonical (primary) command
        name from the bbsmenu, or None if it matches nothing.

        Search order: extra_contexts in order given, then context 2 (global).
        Matching is case-insensitive against both the primary command and all
        its aliases.

        Example:
            resolve('LOGOFF')       → 'OFF'
            resolve('EDIT PREFS')   → 'EP'
            resolve('ET')           → 'ET'
            resolve('WHO')          → 'WHO'
        """
        upper = typed.strip().upper()
        check = list(extra_contexts)
        if 2 not in check:
            check.append(2)
        for ctx in check:
            for primary, aliases, _ in self._contexts.get(ctx, []):
                if upper == primary or upper in aliases:
                    return primary
        return None

    def context_name(self, context: int) -> str:
        return self._descriptions.get(context, f'Context {context}')

    def help_text(self, context: int) -> str:
        cmds = self.commands(context)
        if not cmds:
            return ''
        lines = [f'  Commands  ({self.context_name(context)}):']
        for primary, aliases, desc in cmds:
            all_names = ', '.join([primary] + aliases)
            if desc:
                lines.append(f'  {all_names:<22} {desc}')
            else:
                lines.append(f'  {all_names}')
        return '\r\n'.join(lines) + '\r\n'


_PRINTF_PAT = _re.compile(rb'%-?\d*\.?\d*[sdc]')


def _apply_printf_subs(raw: bytes, subs: dict) -> bytes:
    """
    Replace C-style %s / %d / %-Nd placeholders in raw with values from
    subs (keyed 0, 1, 2 ... in left-to-right order).
    """
    idx    = 0
    result = bytearray()
    last   = 0

    for m in _PRINTF_PAT.finditer(raw):
        result += raw[last:m.start()]
        if idx in subs:
            val = subs[idx]
            fmt = m.group().decode('latin-1')
            try:
                result += (fmt % val).encode('latin-1', errors='replace')
            except (TypeError, ValueError):
                result += str(val).encode('latin-1', errors='replace')
        else:
            result += m.group()
        idx  += 1
        last  = m.end()

    result += raw[last:]
    return bytes(result)
