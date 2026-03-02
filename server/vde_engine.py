"""
server/vde_engine.py — Visual Data Editor Core Engine (CNet/5 authentic)

Renders a full-screen form editor that exactly matches the CNet/5 VDE visual
style as confirmed from live BBS screenshots.

VISUAL DESIGN (from screenshots)
──────────────────────────────────
  Row 1 : Status bar — node + BBS name + handle + date/time
           Bright white on blue, full width.
  Row 2 : Left = context info (e.g. "Physical subbd#: 2")
           Right = "CNet/5 VisualDataEditor" on blue
  Row 3 : Left = more context (e.g. "Subboard list #: 2")
           Right = "Use cursor keys; ENTER to select" on blue
  Row 4 : Blank
  Row 5+: Nav buttons (always blue bg), then data fields.

COLOR SCHEME
─────────────
  Label cell   (20 chars + colon):  white on BLUE  (\x1b[0;37;44m)
  Value cell   (space + value):     yellow on BLACK (\x1b[0;33;40m)
  Cursor row   (entire row):        bright white on BLUE (\x1b[1;37;44m)
  Nav items    (always):            white on BLUE  (\x1b[0;37;44m)
  Ghosted      (no value shown):    dim on black (\x1b[2;37;40m)
  Action items (KILL etc):          bright red on black (\x1b[1;31;40m)
  Edit mode    (inline edit):       black on yellow (\x1b[0;30;43m)

COLUMN LAYOUTS (0-indexed char positions)
────────────────────────────────────────────
  1-col : label 0-19, colon 20, value 22-79
  2-col : left(label 0-19, colon 20, value 22-50)  right(label 52-71, colon 72, value 74-79)
  3-col : col0(label 0-19,  colon 20, value 22-25)
          col1(label 27-46, colon 47, value 49-52)
          col2(label 54-73, colon 74, value 76-79)

NAV ITEM FORMAT
────────────────
  << items: padded to 30 chars on blue
  >> items: "Label name           >>" (padded so >> at col 20)
"""

from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from datetime import datetime
from config import Config

log = logging.getLogger('anet.vde_engine')

# ── ANSI helpers ──────────────────────────────────────────────────────────────
_BLU   = '\x1b[0;37;44m'    # white on blue  (labels, nav)
_YLW   = '\x1b[0;33;40m'    # yellow on black (values)
_CUR   = '\x1b[1;37;44m'    # bright white on blue (cursor)
_DIM   = '\x1b[2;37;40m'    # dim on black (ghosted)
_RED   = '\x1b[1;31;40m'    # bright red on black (action/danger)
_STS   = '\x1b[1;37;44m'    # bright white on blue (status bar)
_VDE   = '\x1b[0;36;44m'    # cyan on blue (VDE header right)
_RST   = '\x1b[0m'
_BLK   = '\x1b[0;40m'
_EDIT  = '\x1b[0;30;43m'    # black on yellow (edit mode)
_EDITC = '\x1b[1;37;43m'    # bright on yellow (edit cursor char)

def _goto(r, c): return f'\x1b[{r};{c}H'
def _clrline(): return '\x1b[2K'

CLR  = '\x1b[2J\x1b[H'
W    = 80   # terminal width assumption
NAV_W = 30  # width of nav item blue block

# Layout: list of (label_0idx, val_0idx, val_width) for each column
LAYOUTS = {
    1: [(0, 22, 57)],
    2: [(0, 22, 28), (52, 74, 6)],
    3: [(0, 22, 4), (27, 49, 4), (54, 76, 4)],
}

# ── Key constants ──────────────────────────────────────────────────────────────
KEY_UP='UP'; KEY_DOWN='DOWN'; KEY_LEFT='LEFT'; KEY_RIGHT='RIGHT'
KEY_PGUP='PGUP'; KEY_PGDN='PGDN'; KEY_HOME='HOME'; KEY_END='END'
KEY_ENTER='ENTER'; KEY_BS='BS'; KEY_DEL='DEL'; KEY_ESC='ESC'
KEY_CTRL_X='CTRL_X'; KEY_CTRL_V='CTRL_V'
KEY_TIMEOUT='TIMEOUT'; KEY_EOF='EOF'

async def read_key(reader, timeout=300.0):
    try:
        raw = await asyncio.wait_for(reader.read(1), timeout=timeout)
    except asyncio.TimeoutError:
        return KEY_TIMEOUT
    if not raw:
        return KEY_EOF
    b = raw[0]
    if b == 0xFF:   # Telnet IAC
        try: await asyncio.wait_for(reader.read(2), timeout=0.2)
        except: pass
        return await read_key(reader, timeout)
    if b == 0x1B:   # ESC / ANSI
        try:
            nxt = await asyncio.wait_for(reader.read(1), timeout=0.15)
        except asyncio.TimeoutError:
            return KEY_ESC
        if not nxt or nxt[0] == 0x1B: return KEY_ESC
        if nxt[0] == ord('['):
            params = b''
            for _ in range(8):
                try:
                    ch = await asyncio.wait_for(reader.read(1), timeout=0.1)
                except: break
                if not ch: break
                params += ch
                if 0x40 <= params[-1] <= 0x7E: break
            p = params.decode('latin-1', errors='replace')
            return {'A':KEY_UP,'B':KEY_DOWN,'C':KEY_RIGHT,'D':KEY_LEFT,
                    'H':KEY_HOME,'F':KEY_END,'5~':KEY_PGUP,'6~':KEY_PGDN,
                    '1~':KEY_HOME,'4~':KEY_END,'3~':KEY_DEL}.get(p, KEY_ESC)
        return KEY_ESC
    ctrl = {0x0D:KEY_ENTER,0x08:KEY_BS,0x7F:KEY_BS,
            0x18:KEY_CTRL_X,0x16:KEY_CTRL_V}
    if b == 0x0A:   # bare LF — swallow it (CR already fired KEY_ENTER)
        return await read_key(reader, timeout)
    if b in ctrl:
        if b == 0x0D:   # CR — consume trailing LF if it arrives immediately
            try:
                nxt = await asyncio.wait_for(reader.read(1), timeout=0.05)
                if nxt and nxt[0] != 0x0A:
                    # Not LF — can't un-read, but this is very rare
                    pass
            except asyncio.TimeoutError:
                pass
        return ctrl[b]
    if 0x20 <= b <= 0x7E: return chr(b)
    return await read_key(reader, timeout)


# ── Field dataclass ────────────────────────────────────────────────────────────
@dataclass
class VDEField:
    """
    One field in a VDE form.
    ftype: 'str'|'int'|'bool'|'bool3'|'nav'|'action'|'sep'
    col:   0=left, 1=middle, 2=right  (for multi-column screens)
    """
    label       : str
    ftype       : str        = 'str'
    db_key      : str | None = None
    width       : int        = 20
    implemented : bool       = True
    choices     : list | None = None
    min_val     : int | None  = None
    max_val     : int | None  = None
    sub_fn      : Any        = None
    confirm     : bool       = False
    col         : int        = 0

    @property
    def is_editable(self):
        return self.implemented and self.ftype not in ('sep', 'nav', 'action')

    def format_value(self, val, val_w=30):
        if val is None:
            s = ''
        elif self.ftype in ('bool', 'bool3'):
            choices = self.choices or (['No','Yes'] if self.ftype=='bool' else ['No','Yes','Def'])
            try:
                s = choices[int(val)] if val is not None else choices[0]
            except (IndexError, TypeError, ValueError):
                s = str(val)
        else:
            s = str(val) if val is not None else ''
        return s[:val_w]


# ── Form engine ────────────────────────────────────────────────────────────────
class VDEForm:
    """
    Full-screen VDE form.

    session    : BBSSession
    title      : label for status bar
    fields     : list[VDEField]
    data       : {db_key: value}
    info_lines : up to 2 context strings for header rows 2-3
    num_cols   : 1, 2, or 3
    """

    def __init__(self, session, title, fields, data,
                 info_lines=None, num_cols=1):
        self.s          = session
        self.title      = title
        self.fields     = fields
        self.data       = dict(data)
        self._orig      = dict(data)
        self.info_lines = info_lines or []
        self.num_cols   = num_cols
        self._dirty     = {}
        self._cursor    = 0
        sh = getattr(session, 'screen_height', 24)
        self._view_rows = max(6, sh - 5)
        self._vstart    = 0
        # Start cursor on first selectable field (not sep, not ghosted)
        for i, f in enumerate(fields):
            if f.ftype != 'sep' and f.implemented:
                self._cursor = i
                break

    async def run(self):
        self.s.reader.raw_keys = True   # VDE needs raw BS/DEL passthrough
        try:
            await self._redraw()
            while True:
                key = await read_key(self.s.reader, timeout=300.0)
                if key in (KEY_TIMEOUT, KEY_EOF): return None
                if key == KEY_CTRL_V: await self._redraw(); continue
                if key == KEY_CTRL_X: return await self._confirm_exit()
                if key == KEY_ESC: return None
                if   key == KEY_UP:    await self._move(-1)
                elif key == KEY_DOWN:  await self._move(1)
                elif key == KEY_PGUP:  await self._move(-self._view_rows)
                elif key == KEY_PGDN:  await self._move(self._view_rows)
                elif key == KEY_HOME:  await self._jump(0)
                elif key == KEY_END:   await self._jump(len(self.fields)-1)
                elif key == KEY_ENTER:
                    result = await self._activate()
                    if result == '__save__': return dict(self._dirty) if self._dirty else {}
                    if result == '__exit__': return None
                    if isinstance(result, dict):
                        self._dirty.update(result)
                        self.data.update(result)
                    await self._redraw()
        finally:
            self.s.reader.raw_keys = False   # always restore

    async def _move(self, delta):
        n = len(self.fields)
        if not n: return
        pos = self._cursor
        d = 1 if delta > 0 else -1
        moved = 0
        while moved < abs(delta):
            pos = (pos + d) % n
            f = self.fields[pos]
            # Skip separators and ghosted (not implemented) fields
            if f.ftype != 'sep' and f.implemented:
                moved += 1
            if pos == self._cursor: break
        if pos == self._cursor: return
        self._cursor = pos
        self._ensure_visible()
        await self._redraw()

    async def _jump(self, idx):
        self._cursor = max(0, min(len(self.fields)-1, idx))
        self._ensure_visible()
        await self._redraw()

    def _ensure_visible(self):
        if self.num_cols > 1: return
        if self._cursor < self._vstart:
            self._vstart = self._cursor
        elif self._cursor >= self._vstart + self._view_rows:
            self._vstart = self._cursor - self._view_rows + 1

    async def _activate(self):
        f = self.fields[self._cursor]
        if f.ftype == 'sep': return None
        if f.ftype == 'action':
            if f.db_key == '__kill__' and f.confirm:
                ans = await self._prompt_line('Kill this account? (Y/N): ')
                if ans and ans.upper().startswith('Y'):
                    return {'__kill__': True}
                return None
            if f.db_key == '__save__': return '__save__'
            if f.db_key == '__exit__': return '__exit__'
            return None
        if f.ftype == 'nav':
            if f.sub_fn: return await f.sub_fn(self.s, self.data)
            if f.db_key in ('__exit__', '__prev__'): return '__exit__'
            return None
        if not f.implemented or not f.db_key: return None
        # Inline edit
        new_val = await self._edit_inline(f)
        if new_val is not None:
            self.data[f.db_key] = new_val
            self._dirty[f.db_key] = new_val
        return None

    async def _confirm_exit(self):
        if self._dirty:
            ans = await self._prompt_line('Save changes before exiting? [Y/n]: ')
            if ans is None or ans.strip().upper() in ('', 'Y', 'YES'):
                return dict(self._dirty)
        return None

    async def _edit_inline(self, f):
        if f.ftype in ('bool', 'bool3'):
            choices = f.choices or (['No','Yes'] if f.ftype=='bool' else ['No','Yes','Def'])
            cur = self.data.get(f.db_key)
            try: idx = int(cur) if cur is not None else 0
            except: idx = 0
            return (idx + 1) % len(choices)

        # Determine the row/column of this field on screen
        row, layout = self._field_screen_pos(self._cursor)
        if row is None: return None
        _, val_0idx, val_w = layout
        val_col = val_0idx + 1 + 1  # +1 for space after colon, +1 for 1-indexed

        current = str(self.data.get(f.db_key, '') or '')
        value = current
        cp = len(value)

        await self._draw_edit_val(row, val_col, value, cp, val_w)
        while True:
            key = await read_key(self.s.reader, timeout=300.0)
            if key == KEY_ENTER: break
            elif key == KEY_ESC: return None
            elif key == KEY_BS:
                if cp > 0: value = value[:cp-1]+value[cp:]; cp -= 1
            elif key == KEY_DEL:
                if cp < len(value): value = value[:cp]+value[cp+1:]
            elif key == KEY_LEFT: cp = max(0, cp-1)
            elif key == KEY_RIGHT: cp = min(len(value), cp+1)
            elif key == KEY_HOME: cp = 0
            elif key == KEY_END: cp = len(value)
            elif isinstance(key, str) and len(key)==1 and key.isprintable():
                if len(value) < val_w:
                    value = value[:cp]+key+value[cp:]; cp += 1
            await self._draw_edit_val(row, val_col, value, cp, val_w)

        if f.ftype == 'int':
            try:
                iv = int(value.strip())
                if f.min_val is not None and iv < f.min_val: return None
                if f.max_val is not None and iv > f.max_val: return None
                return iv
            except ValueError: return None
        return value.strip()

    async def _draw_edit_val(self, row, col1, value, cp, val_w):
        disp   = value.ljust(val_w)[:val_w]
        before = disp[:cp]
        at     = disp[cp] if cp < len(disp) else ' '
        after  = disp[cp+1:]
        out = _goto(row, col1) + _EDIT + before + _EDITC + at + _EDIT + after + _RST
        await self.s.send(out.encode())

    async def _prompt_line(self, prompt):
        """Show a prompt on row 22, read a line."""
        out = _goto(22, 1) + _CUR + f'  {prompt}'.ljust(W-1) + _RST
        await self.s.send(out.encode())
        try:
            raw = await asyncio.wait_for(self.s.reader.readline(), timeout=60.0)
        except asyncio.TimeoutError:
            return None
        if not raw: return None
        return raw.decode('latin-1', errors='replace').rstrip('\r\n')

    def _field_screen_pos(self, fi):
        """Return (row, layout_tuple) for the field at index fi, or (None, ...)."""
        f = self.fields[fi]
        layouts = LAYOUTS[self.num_cols]
        col = min(f.col, len(layouts)-1)
        layout = layouts[col]

        nav_count = sum(1 for x in self.fields if x.ftype == 'nav')
        start_row = 5 + nav_count + (1 if nav_count else 0)

        if self.num_cols == 1:
            data_fields = [x for x in self.fields if x.ftype not in ('nav',)]
            try: fi_in_data = data_fields.index(f)
            except ValueError: return None, layout
            row = start_row + fi_in_data - self._vstart
            if row < start_row or row > 22: return None, layout
            return row, layout
        else:
            col_fields = [x for x in self.fields if x.ftype not in ('nav',) and x.col == col]
            try: fi_in_col = col_fields.index(f)
            except ValueError: return None, layout
            return start_row + fi_in_col, layout

    # ── Full screen redraw ────────────────────────────────────────────────────

    async def _redraw(self):
        out = [CLR, self._header()]
        out.append(self._body())
        await self.s.send(''.join(out).encode())

    def _header(self):
        node   = getattr(self.s, 'node_id', 0)
        handle = getattr(self.s, 'handle', '')
        bbs    = Config.BBS_NAME[:20]
        now    = datetime.now().strftime('%a %d-%b-%Y %I:%M%p')

        left1  = f'{node}  {bbs}  {handle}'
        row1   = (_goto(1,1) + _STS
                  + left1.ljust(48)[:48]
                  + now.rjust(W-48)[:W-48])

        vde_title = 'CNet/5 VisualDataEditor'
        vde_hint  = 'Use cursor keys; ENTER to select'
        rw = len(vde_hint) + 1

        il = self.info_lines
        l2 = il[0][:W-rw-1].ljust(W-rw-1) if len(il) > 0 else ' '*(W-rw-1)
        l3 = il[1][:W-rw-1].ljust(W-rw-1) if len(il) > 1 else ' '*(W-rw-1)

        row2 = (_goto(2,1) + _RST + l2 + _VDE + vde_title.rjust(rw))
        row3 = (_goto(3,1) + _RST + l3 + _VDE + vde_hint.rjust(rw))
        row4 = _goto(4,1) + _BLK + ' '*W
        return row1 + row2 + row3 + row4

    def _body(self):
        out = []
        row = 5

        # Nav items (always at top)
        nav_fields = [f for f in self.fields if f.ftype == 'nav']
        for f in nav_fields:
            is_cur = (self.fields.index(f) == self._cursor)
            out.append(self._render_nav(row, f, is_cur))
            row += 1
        if nav_fields:
            out.append(_goto(row,1) + _BLK + ' '*W)
            row += 1

        data_fields = [f for f in self.fields if f.ftype not in ('nav',)]

        if self.num_cols == 1:
            layouts = LAYOUTS[1]
            for fi, f in enumerate(data_fields):
                if fi < self._vstart: continue
                if row > 22: break
                is_cur = (self.fields.index(f) == self._cursor)
                out.append(self._render_field(row, f, is_cur, layouts[0]))
                row += 1
        else:
            layouts = LAYOUTS[self.num_cols]
            cols = {c: [f for f in data_fields if f.col == c]
                    for c in range(self.num_cols)}
            max_rows = max((len(v) for v in cols.values()), default=0)
            for ri in range(max_rows):
                if row > 22: break
                # Clear row
                out.append(_goto(row,1) + _BLK + ' '*W)
                for ci in range(self.num_cols):
                    col_fields = cols.get(ci, [])
                    if ri < len(col_fields):
                        f = col_fields[ri]
                        is_cur = (self.fields.index(f) == self._cursor)
                        lay = layouts[min(ci, len(layouts)-1)]
                        out.append(self._render_field_at(row, f, is_cur, lay))
                row += 1

        while row <= 23:
            out.append(_goto(row,1) + _BLK + ' '*W)
            row += 1
        return ''.join(out)

    def _render_nav(self, row, f, is_cur):
        color = _CUR if is_cur else _BLU
        if f.sub_fn or (f.db_key and f.db_key.startswith('__sub')):
            text = f'{f.label:<{NAV_W-3}} >>'
        else:
            text = f.label
        text = text[:NAV_W].ljust(NAV_W)
        return _goto(row,1) + color + text + _RST

    def _render_field(self, row, f, is_cur, layout):
        """Render field in single-column mode (full-row)."""
        lstart, vstart, vw = layout
        if f.ftype == 'sep':
            return _goto(row,1) + _BLK + ' '*W
        if f.ftype == 'action':
            col = _CUR if is_cur else _RED
            text = f'  {f.label}'.ljust(W)[:W]
            return _goto(row,1) + col + text + _RST

        lc = (f.label[:20].ljust(20) + ':')   # label cell (21 chars)

        if not f.implemented:
            return _goto(row,1) + _DIM + lc + _RST

        val = self.data.get(f.db_key, '') if f.db_key else ''
        vs = f.format_value(val, vw)

        if is_cur:
            full = (lc + ' ' + vs).ljust(W)[:W]
            return _goto(row,1) + _CUR + full + _RST
        else:
            return _goto(row,1) + _BLU + lc + _YLW + ' ' + vs + _RST

    def _render_field_at(self, row, f, is_cur, layout):
        """Render field at a column position (multi-column mode)."""
        lstart, vstart, vw = layout
        col1 = lstart + 1   # 1-indexed

        if f.ftype == 'sep':
            return ''
        if f.ftype == 'action':
            col = _CUR if is_cur else _RED
            w2  = vstart + vw - lstart + 1
            text = f.label[:w2].ljust(w2)
            return _goto(row, col1) + col + text + _RST
        if f.ftype == 'nav':
            color = _CUR if is_cur else _BLU
            w2 = vstart + vw - lstart + 1
            text = f'{f.label:<{w2-3}} >>'.ljust(w2)[:w2]
            return _goto(row, col1) + color + text + _RST

        lc = f.label[:20].ljust(20) + ':'

        if not f.implemented:
            return _goto(row, col1) + _DIM + lc + _RST

        val = self.data.get(f.db_key, '') if f.db_key else ''
        vs  = f.format_value(val, vw)

        if is_cur:
            w2   = vstart + vw - lstart + 2
            full = (lc + ' ' + vs)[:w2]
            return _goto(row, col1) + _CUR + full + _RST
        else:
            return (_goto(row, col1) + _BLU + lc
                    + _goto(row, vstart+1) + _YLW + ' ' + vs + _RST)
