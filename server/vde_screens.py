"""
server/vde_screens.py — VDE Screen Definitions (CNet/5 authentic)

Field lists and layouts derived directly from the CNet PRO v5 binary definition
files (udata, subboard, agroup, etc.) as parsed from the VDE.zip upload.

Field binary data format (82 bytes per record):
  Bytes 0-20  : label text
  Byte  49    : ftype  (0x01=nav/str, 0x28=num, 0x17=action, 0x1b=bool, 0x35=bool3)
  Bytes 50-51 : GETUSER field ID (big-endian)
  Bytes 52-55 : min value (big-endian signed int)
  Bytes 56-59 : max value
  Byte  60    : display width
  Byte  61    : data type (0x02=str, 0x03/04/05=int, 0x06=bigint, 0x07=date,
                            0x09=menu, 0x0a=menulist, 0x01=bool, 0x16=int_ro)

SCREEN LAYOUTS
──────────────
  EA profile       : 2-col  (col 0 = profile data, col 1 = phone+nav+kill)
  EA credits       : 2-col  (col 0 = file/byte stats, col 1 = time/call/balance)
  EA priv flags    : 3-col  (all 43 CNet privilege flags)
  EA prefs/term    : 2-col  (col 0 = macros+prefs, col 1 = ANSI+screen)
  EB main          : 1-col  (title, path, >> sub-screen links)
  EG main          : 1-col  (access group config)
"""

from __future__ import annotations
from server.vde_engine import VDEField


def _row(obj) -> dict:
    """
    Safely convert a sqlite3.Row (or any row-like object) to a plain dict.
    sqlite3.Row supports index access and keys() but NOT .get() — this
    adapter bridges that gap so all _data functions can use .get() safely.
    """
    if isinstance(obj, dict):
        return obj
    try:
        return dict(obj)
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# EA — Edit Account: Profile screen  (main screen, 2-column layout)
# ─────────────────────────────────────────────────────────────────────────────

def make_ea_profile_fields(user: dict, sub_fns: dict) -> list[VDEField]:
    """
    Main EA profile screen — 2-column layout.
    Left col  (col=0): core profile data fields
    Right col (col=1): phone numbers + sub-screen links + kill action
    Nav items : << Exit at top
    """
    return [
        # ── Navigation (always rendered at top) ─────────────────────────────
        VDEField('<< Exit',           'nav', '__exit__'),

        # ── Left column (col=0): profile data ───────────────────────────────
        VDEField('Handle',            'str', 'handle',       width=20, col=0),
        VDEField('Real name',         'str', 'real_name',    width=25, col=0),
        VDEField('Organization',      'str', 'organization', width=30, col=0),
        VDEField('Sysop comment',     'str', 'notes',        width=33, col=0),
        VDEField('User banner',       'str', 'user_banner',  width=42, col=0),
        VDEField('Address',           'str', None,           width=30, col=0, implemented=False),
        VDEField('City and State',    'str', 'location',     width=30, col=0),
        VDEField('Zip/postal code',   'str', None,           width=10, col=0, implemented=False),
        VDEField('Country',           'str', None,           width=3,  col=0, implemented=False),
        VDEField('MAIL ID',           'str', 'email',        width=40, col=0),
        VDEField('Sex',               'str', 'gender',       width=6,  col=0),
        VDEField('High baud rate',    'int', None,           width=5,  col=0, implemented=False,
                 min_val=30, max_val=6000),
        VDEField('Birthday',          'str', 'dob',          width=17, col=0),
        VDEField('Access group',      'int', 'access_group', width=2,  col=0,
                 min_val=0, max_val=31),
        VDEField('Expiration date',   'str', None,           width=17, col=0, implemented=False),
        VDEField('Expiration access', 'int', None,           width=2,  col=0, implemented=False,
                 min_val=0, max_val=31),

        # ── Right column (col=1): spacers to align phone rows with Zip/postal ─
        # Left col rows: Handle(0) RealName(1) Org(2) SysopComment(3)
        # UserBanner(4) Address(5) CityState(6) Zip(7)
        # Phone verification aligns with Zip → 7 spacers
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('Phone verification', 'str', None,           width=5,  col=1, implemented=False),
        VDEField('Data  phone#',       'str', 'data_phone',   width=16, col=1),
        VDEField('Voice phone#',       'str', 'phone',        width=16, col=1),

        # ── Right col: sub-screen navigation links ───────────────────────────
        VDEField('Edit password',       'nav', '__sub_password__', col=1,
                 sub_fn=sub_fns.get('password')),
        VDEField('Credits/balances',    'nav', '__sub_credits__',  col=1,
                 sub_fn=sub_fns.get('credits')),
        VDEField('Limits/ratios/flags', 'nav', None,               col=1, implemented=False),
        VDEField('Privilege flags',     'nav', '__sub_privs__',    col=1,
                 sub_fn=sub_fns.get('privs')),
        VDEField('Preferences/term',    'nav', '__sub_prefs__',    col=1,
                 sub_fn=sub_fns.get('prefs')),

        # ── Right col: kill action ───────────────────────────────────────────
        VDEField('KILL THIS ACCOUNT',   'action', '__kill__', col=1, confirm=True),
    ]


def make_ea_profile_data(user: dict) -> dict:
    user = _row(user)
    return {
        'handle':       user.get('handle', ''),
        'real_name':    user.get('real_name', '') or '',
        'organization': user.get('organization', '') or '',
        'notes':        user.get('notes', '') or '',
        'user_banner':  user.get('user_banner', '') or '',
        'location':     user.get('location', '') or '',
        'email':        user.get('email', '') or '',
        'dob':          user.get('dob', '') or '',
        'gender':       user.get('gender', '') or '',
        'data_phone':   user.get('data_phone', '') or '',
        'phone':        user.get('phone', '') or '',
        'access_group': user.get('access_group', 5),
        'priv_flags':   user.get('priv_flags', 0) or 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EA — Edit Account: Credits/Balances sub-screen  (2-column)
# ─────────────────────────────────────────────────────────────────────────────

def make_ea_credits_fields() -> list[VDEField]:
    """
    Credits/Balances sub-screen — 2-column layout.
    Left col  (col=0): file/byte stats
    Right col (col=1): time/call/balance stats
    """
    return [
        VDEField('<< Exit',              'nav', '__exit__'),

        # ── Left column: file/byte statistics ───────────────────────────────
        VDEField('Uploads today',        'int', 'uploads_today',     width=6,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Downloads today',      'int', 'downloads_today',   width=6,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Total uploads',        'int', 'total_uploads',     width=10, col=0,
                 min_val=0, max_val=999999999),
        VDEField('Total downloads',      'int', 'total_downloads',   width=10, col=0,
                 min_val=0, max_val=999999999),
        VDEField('File Credits',         'int', 'file_credits',      width=10, col=0,
                 min_val=-999999999, max_val=999999999),
        VDEField('', 'sep', col=0),
        VDEField('Bytes uploaded today', 'int', 'bytes_up_today',    width=12, col=0,
                 min_val=0, max_val=999999999),
        VDEField('Bytes dnloaded today', 'int', 'bytes_dn_today',    width=12, col=0,
                 min_val=0, max_val=999999999),
        VDEField('Total KB uploaded',    'int', 'total_bytes_up',    width=12, col=0,
                 min_val=0, max_val=999999999),
        VDEField('Total KB downloaded',  'int', 'total_bytes_dn',    width=12, col=0,
                 min_val=0, max_val=999999999),
        VDEField('Byte credits',         'int', 'byte_credits',      width=12, col=0,
                 min_val=-999999999, max_val=999999999),

        # ── Right column: time/call/balance statistics ───────────────────────
        # Last call date — read-only display (ghosted)
        VDEField('Last call date',       'str', 'last_call',         width=17, col=1,
                 implemented=False),
        VDEField('Time today (1/10s)',   'int', 'time_today',        width=7,  col=1,
                 min_val=0, max_val=14400),
        VDEField('Calls today',          'int', 'calls_today',       width=7,  col=1,
                 min_val=0, max_val=32767),
        VDEField('Total calls',          'int', 'call_count',        width=10, col=1,
                 min_val=0, max_val=999999999),
        VDEField('Time credits (1/10s)', 'int', 'time_credits_tenths', width=10, col=1,
                 min_val=0, max_val=999999999),
        VDEField('', 'sep', col=1),
        VDEField('Balance (cents)',       'int', 'balance_cents',    width=12, col=1,
                 min_val=-20000000, max_val=20000000),
        VDEField('P-file points',         'int', 'pfile_points',     width=12, col=1,
                 min_val=-999999999, max_val=999999999),
        VDEField('Network credits',       'int', 'network_credits',  width=12, col=1,
                 min_val=-999999999, max_val=999999999),
        VDEField('Public messages',       'int', 'public_msg_count', width=10, col=1,
                 min_val=0, max_val=999999999),
        VDEField('Private messages',      'int', 'private_msg_count',width=10, col=1,
                 min_val=0, max_val=999999999),
    ]


def make_ea_credits_data(user: dict) -> dict:
    user = _row(user)
    return {
        'uploads_today':        user.get('uploads_today', 0) or 0,
        'downloads_today':      user.get('downloads_today', 0) or 0,
        'total_uploads':        user.get('total_uploads', 0) or 0,
        'total_downloads':      user.get('total_downloads', 0) or 0,
        'file_credits':         user.get('file_credits', 0) or 0,
        'bytes_up_today':       user.get('bytes_up_today', 0) or 0,
        'bytes_dn_today':       user.get('bytes_dn_today', 0) or 0,
        'total_bytes_up':       user.get('total_bytes_up', 0) or 0,
        'total_bytes_dn':       user.get('total_bytes_dn', 0) or 0,
        'byte_credits':         user.get('byte_credits', 0) or 0,
        'last_call':            user.get('last_call', '') or '',
        'time_today':           user.get('time_today', 0) or 0,
        'calls_today':          user.get('calls_today', 0) or 0,
        'call_count':           user.get('call_count', 0) or 0,
        'time_credits_tenths':  user.get('time_credits_tenths', 0) or 0,
        'balance_cents':        user.get('balance_cents', 0) or 0,
        'pfile_points':         user.get('pfile_points', 0) or 0,
        'network_credits':      user.get('network_credits', 0) or 0,
        'public_msg_count':     user.get('public_msg_count', 0) or 0,
        'private_msg_count':    user.get('private_msg_count', 0) or 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EA — Edit Account: Privilege Flags sub-screen  (3-column)
# ─────────────────────────────────────────────────────────────────────────────

# 43 flags from the udata binary, presented as one 3-col screen.
# Tuple: (label, ftype, is_3state, implemented)
_PRIV_DEFS = [
    # Privilege screen 1  (udata records 2-19)
    ('SYSTEM OPERATOR',       'bool',  False, False),   # 0  — AG-level, no DB hook
    ('Send EMail',            'bool',  False, False),   # 1
    ('Receive EMail',         'bool',  False, False),   # 2
    ('Set mail expiration',   'bool',  False, False),   # 3
    ('Send bulk mail',        'bool',  False, False),   # 4
    ('EXPANSION',             'bool',  False, False),   # 5
    ('Send urgent mail',      'bool',  False, False),   # 6
    ('Forward mail',          'bool',  False, False),   # 7
    ('Use doors',             'bool',  False, False),   # 8
    ('Use text/door lists',   'bool',  False, False),   # 9
    ('Use the UserList',      'bool',  False, False),   # 10
    ('Use CC, Join, OLMs',    'bool',  False, False),   # 11
    ('MCI level 1',           'bool',  False, False),   # 12
    ('MCI level 2',           'bool',  False, False),   # 13
    ('Relogon',               'bool',  False, False),   # 14
    ('Bypass bbsevents',      'bool',  False, False),   # 15
    ('Alias msg authors',     'bool',  False, False),   # 16
    ('Adopt orphans',         'bool',  False, False),   # 17
    # Privilege screen 2  (udata records 20-47)
    ('Read private msgs',     'bool',  False, True),    # 18
    ('Kill/edit any file',    'bool',  False, True),    # 19
    ('Kill/edit own files',   'bool',  False, True),    # 20
    ('Skip file validation',  'bool',  False, True),    # 21
    ('Write anonymously',     'bool',  False, True),    # 22
    ('Trace anonymous',       'bool',  False, True),    # 23
    ('Private messages',      'bool',  False, True),    # 24
    ('Conference control',    'bool',  False, True),    # 25
    ('Infinite file credit',  'bool',  False, True),    # 26
    ('Infinite byte credit',  'bool',  False, True),    # 27
    ('AutoCallBack @logon',   'bool3', True,  False),   # 28  — needs CallBack infra
    ('TimeLock exempt',       'bool',  False, True),    # 29
    ('Add new vote topics',   'bool',  False, True),    # 30
    ('Add new vote choices',  'bool',  False, True),    # 31
    ('Kill/Edit vote topic',  'bool',  False, True),    # 32
    ('Edit handle',           'bool',  False, True),    # 33
    ('Edit name, bday, sex',  'bool',  False, True),    # 34
    ('Edit address, st/zip',  'bool',  False, True),    # 35
    ('Edit voice phone#',     'bool3', True,  False),   # 36
    ('Edit data phone#',      'bool3', True,  False),   # 37
    ('Allow WHO banner',      'bool3', True,  True),    # 38
    ('Use TermLink',          'bool3', True,  False),   # 39
    ('Monitor another port',  'bool3', True,  False),   # 40
    # Extra flags from udata 170-179
    ('Empty trash on exit',   'bool',  False, False),   # 41
    ('Suspend account',       'bool',  False, True),    # 42
]

ALL_PRIV = _PRIV_DEFS


def make_ea_priv_fields() -> list[VDEField]:
    """3-column privilege flags screen — nav items first, then 3 cols of flags."""
    fields = [
        VDEField('<< Exit', 'nav', '__exit__'),
    ]
    n = len(ALL_PRIV)
    col_size = (n + 2) // 3   # ceil(n/3) → roughly equal thirds
    for i, (label, ftype, is3, impl) in enumerate(ALL_PRIV):
        col = min(i // col_size, 2)
        choices = ['No', 'Yes', 'Def'] if is3 else None
        fields.append(VDEField(
            label, ftype, f'__priv_{i}__',
            width=3, choices=choices, col=col, implemented=impl
        ))
    return fields


def make_ea_priv_data(user: dict) -> dict:
    user = _row(user)
    flags = user.get('priv_flags', 0) or 0
    data = {}
    for i in range(len(ALL_PRIV)):
        data[f'__priv_{i}__'] = (flags >> i) & 1
    return data


def pack_priv_data(priv_data: dict) -> int:
    flags = 0
    for i in range(len(ALL_PRIV)):
        val = priv_data.get(f'__priv_{i}__', 0)
        try:
            if int(val or 0):
                flags |= (1 << i)
        except (TypeError, ValueError):
            pass
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# EA — Edit Account: Preferences/Terminal sub-screen  (2-column)
# ─────────────────────────────────────────────────────────────────────────────

def make_ea_prefs_fields() -> list[VDEField]:
    """
    Preferences/Terminal sub-screen — 2-column layout.
    Left col  (col=0): macros + behaviour prefs
    Right col (col=1): ANSI + screen geometry
    """
    return [
        VDEField('<< Exit', 'nav', '__exit__'),

        # ── Left column: macros and behaviour ────────────────────────────────
        VDEField('Logon macro',          'str', 'logon_macro',   width=35, col=0),
        VDEField('Control-E macro',      'str', 'ctrl_e_macro',  width=35, col=0),
        VDEField('Control-F macro',      'str', 'ctrl_f_macro',  width=35, col=0),
        VDEField('', 'sep', col=0),
        VDEField('Organization',         'str', None,            width=30, col=0, implemented=False),
        VDEField('Response pausing',     'str', None,            width=12, col=0, implemented=False),
        VDEField('Help level',           'str', None,            width=10, col=0, implemented=False),
        VDEField('Time format',          'str', None,            width=8,  col=0, implemented=False),
        VDEField('Auto hide/muffle',     'str', None,            width=10, col=0, implemented=False),
        VDEField('Computer type',        'str', None,            width=10, col=0, implemented=False),
        VDEField('More? mode',           'bool',None,            width=3,  col=0, implemented=False),
        VDEField('Mail box open',        'bool',None,            width=3,  col=0, implemented=False),
        VDEField('Mail box forward to',  'int', None,            width=5,  col=0, implemented=False,
                 min_val=0, max_val=9999),

        # ── Right column: ANSI and screen settings ───────────────────────────
        # ANSI support — 3-way: None / Simple / Full
        VDEField('ANSI support',         'bool3','ansi_level',   width=8,  col=1,
                 choices=['None', 'Simple', 'Full']),
        VDEField('ANSI colors',          'bool', 'ansi_color',   width=3,  col=1),
        VDEField('Screen width',         'int',  'screen_width', width=3,  col=1,
                 min_val=22, max_val=255),
        VDEField('Screen height',        'int',  'screen_height',width=3,  col=1,
                 min_val=5, max_val=50),
        VDEField('ANSI tabs',            'bool', 'ansi_tabs',    width=3,  col=1),
        VDEField('Line feeds',           'bool', 'needs_lf',     width=3,  col=1),
        VDEField('', 'sep', col=1),
        VDEField('Graphics set',         'str',  None,           width=8,  col=1, implemented=False),
        VDEField('Time Zone',            'int',  None,           width=4,  col=1, implemented=False,
                 min_val=-23, max_val=23),
        VDEField('Text editor',          'str',  None,           width=10, col=1, implemented=False),
        VDEField('Text translation',     'str',  None,           width=8,  col=1, implemented=False),
        VDEField('Yank EOL sequence',    'str',  None,           width=8,  col=1, implemented=False),
        VDEField('Yank archive method',  'str',  None,           width=8,  col=1, implemented=False),
    ]


def make_ea_prefs_data(user: dict) -> dict:
    user = _row(user)
    # Map ansi_level string to index for bool3 display
    ansi_str = user.get('ansi_level', 'Simple') or 'Simple'
    ansi_map  = {'None': 0, 'Simple': 1, 'Full': 2}
    ansi_idx  = ansi_map.get(ansi_str, 1)
    return {
        'logon_macro':   user.get('logon_macro', '') or '',
        'ctrl_e_macro':  user.get('ctrl_e_macro', '') or '',
        'ctrl_f_macro':  user.get('ctrl_f_macro', '') or '',
        'ansi_level':    ansi_idx,
        'ansi_color':    user.get('ansi_color', 1) or 1,
        'ansi_tabs':     user.get('ansi_tabs', 0) or 0,
        'needs_lf':      user.get('needs_lf', 0) or 0,
        'screen_width':  user.get('screen_width', 80) or 80,
        'screen_height': user.get('screen_height', 24) or 24,
    }


def unpack_prefs_data(data: dict) -> dict:
    """Convert VDE prefs dict back to DB-ready values (ansi_level idx → string)."""
    out = dict(data)
    ansi_idx = out.get('ansi_level', 1)
    ansi_map = {0: 'None', 1: 'Simple', 2: 'Full'}
    try:
        out['ansi_level'] = ansi_map.get(int(ansi_idx), 'Simple')
    except (TypeError, ValueError):
        out['ansi_level'] = 'Simple'
    return out


# ─────────────────────────────────────────────────────────────────────────────
# EB — Edit Subboard: Main screen  (1-column)
# ─────────────────────────────────────────────────────────────────────────────

def make_eb_main_fields(sub_fns: dict) -> list[VDEField]:
    """
    Main EB screen — 1-column.
    Nav items at top; editable core fields; ghosted path/network fields;
    >> sub-screen links for access vars, flags, etc.
    """
    return [
        VDEField('<< Exit',           'nav', '__exit__'),

        # Core fields — display only (R/O)
        VDEField('Physical subbd#',   'int', 'id',           width=5,  implemented=False),
        VDEField('Subboard list #',   'int', 'list_num',     width=5,  implemented=False),

        # Editable
        VDEField('Title',             'str', 'name',         width=40),
        VDEField('Description',       'str', 'description',  width=60),

        VDEField('', 'sep'),

        # Ghosted system fields
        VDEField('Data dir path',     'str', None,           width=60, implemented=False),
        VDEField('Part0/CD/net',      'str', None,           width=60, implemented=False),
        VDEField('GO keyword/arg',    'str', None,           width=40, implemented=False),
        VDEField('Origin/dist.',      'str', None,           width=30, implemented=False),
        VDEField('Partitions',        'int', None,           width=4,  implemented=False),
        VDEField('Network',           'str', None,           width=16, implemented=False),
        VDEField('Filler (---)',      'str', None,           width=8,  implemented=False),
        VDEField('Keep buffers',      'bool',None,           width=3,  implemented=False),

        VDEField('', 'sep'),

        # Access levels (editable)
        VDEField('Read access AG',    'int', 'read_ag',      width=2,
                 min_val=0, max_val=31),
        VDEField('Write access AG',   'int', 'write_ag',     width=2,
                 min_val=0, max_val=31),

        VDEField('', 'sep'),

        # >> Sub-screen links (all ghosted — future expansion)
        VDEField('Access vars',       'nav', '__sub_access__',
                 sub_fn=sub_fns.get('access_vars'), implemented=False),
        VDEField('Other flags',       'nav', '__sub_flags__',
                 sub_fn=sub_fns.get('other_flags'), implemented=False),
        VDEField('Diz Attributes',    'nav', '__sub_diz__',
                 sub_fn=sub_fns.get('diz'),         implemented=False),
        VDEField('Edit SubOps',       'nav', '__sub_subops__',
                 sub_fn=sub_fns.get('subops'),      implemented=False),
    ]


def make_eb_main_data(board: dict) -> dict:
    board = _row(board)
    return {
        'id':          board.get('id', 0),
        'name':        board.get('name', '') or '',
        'description': board.get('description', '') or '',
        'read_ag':     board.get('read_ag', 0),
        'write_ag':    board.get('write_ag', 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EG — Edit Access Group  (1-column main + 2 sub-screens)
# ─────────────────────────────────────────────────────────────────────────────

def make_eg_fields(group: dict, sub_fns: dict) -> list[VDEField]:
    """
    Main EG screen — 1-column.
    Matches screenshot: Access group title, Def. days until exp., Def. exp. to access,
    then >> Edit privileges, >> Edit limits/ratios links.
    """
    return [
        VDEField('<< Exit',             'nav', '__exit__'),
        VDEField('Access group title',  'str', 'ag_title',     width=30),
        VDEField('Def. days until exp.','int', 'days_until_exp', width=4,
                 min_val=-1, max_val=9999),
        VDEField('Def. exp. to access', 'int', 'exp_to_access',  width=2,
                 min_val=0, max_val=31),
        VDEField('', 'sep'),
        VDEField('Edit privileges',     'nav', '__sub_privs__',
                 sub_fn=sub_fns.get('privs')),
        VDEField('Edit limits/ratios',  'nav', '__sub_limits__',
                 sub_fn=sub_fns.get('limits')),
    ]


def make_eg_data(group: dict) -> dict:
    return {
        'ag_number':      group.get('id', 0),
        'ag_title':       group.get('title', '') or '',
        'days_until_exp': group.get('days_until_exp', 0) or 0,
        'exp_to_access':  group.get('exp_to_access', 0) or 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# EG Edit Privileges — Access Group privilege flags  (3-column)
# As seen in screenshot 3: flags per-AG, includes << Previous screen,
# values are Yes/No/Sub (Sub = subordinate/inherited).
# ─────────────────────────────────────────────────────────────────────────────

# AG privilege flags from screenshot 3 (left→right, top→bottom per column):
# Col 0: Empty trash on exit, Account suspended, SYSTEM OPERATOR, Send EMail,
#         Receive EMail, Set mail expiration, Send bulk mail, Expansion,
#         Send urgent mail, Forward mail, Use doors, Use text/door lists,
#         Use the UserList, Use CC Join OLMs, MCI level 1, MCI level 2,
#         Relogon, Bypass bbsevents, Alias msg authors, Adopt orphans
# Col 1: Read private msgs, Kill/edit any file, Kill/edit own files,
#         Skip file validation, Write anonymously, Trace anonymous,
#         Private messages, Conference control, Infinite file credit,
#         Infinite byte credit, AutoCallBack @logon, TimeLock exempt,
#         Add new vote topics, Add new vote choices, Kill/Edit vote topic,
#         Edit handle, Edit name bday sex, Edit address st/zip
# Col 2: Expansion 3, Expansion 4, Edit voice phone#, Edit data phone#,
#         Allow WHO banner, Use TermLink, Monitor another port,
#         Alarm sysop @logon, Open screen @logon, Open capture @logon,
#         Send FIDO NetMail, Send Internet Mail, FIDO FReq and Attach,
#         Hold and Crash mail, NetMail Cost exempt, Costs are NetCredits,
#         Receive DL rewards, May page the sysop, Broadcast OLMs, SuperUser

# Tuple: (label, key_suffix, col, is_sub_allowed)
# Values: 0=No, 1=Yes, 2=Sub (inherited from parent AG)
_AG_PRIV_DEFS = [
    # Column 0
    ('Empty trash on exit',   'empty_trash',     0, True),
    ('Account suspended',     'acct_suspended',  0, False),
    ('SYSTEM OPERATOR',       'sysop',           0, True),
    ('Send EMail',            'send_email',      0, True),
    ('Receive EMail',         'recv_email',      0, True),
    ('Set mail expiration',   'mail_expire',     0, True),
    ('Send bulk mail',        'bulk_mail',       0, True),
    ('Expansion',             'expansion1',      0, True),
    ('Send urgent mail',      'urgent_mail',     0, True),
    ('Forward mail',          'fwd_mail',        0, True),
    ('Use doors',             'use_doors',       0, True),
    ('Use text/door lists',   'door_lists',      0, True),
    ('Use the UserList',      'userlist',        0, True),
    ('Use CC, Join, OLMs',    'cc_join_olm',     0, True),
    ('MCI level 1',           'mci_1',           0, True),
    ('MCI level 2',           'mci_2',           0, True),
    ('Relogon',               'relogon',         0, True),
    ('Bypass bbsevents',      'bypass_events',   0, True),
    ('Alias msg authors',     'alias_authors',   0, True),
    ('Adopt orphans',         'adopt_orphans',   0, True),
    # Column 1
    ('Read private msgs',     'read_priv_msgs',  1, True),
    ('Kill/edit any file',    'kill_any_file',   1, True),
    ('Kill/edit own files',   'kill_own_files',  1, True),
    ('Skip file validation',  'skip_filevalid',  1, True),
    ('Write anonymously',     'write_anon',      1, True),
    ('Trace anonymous',       'trace_anon',      1, True),
    ('Private messages',      'priv_msgs',       1, True),
    ('Conference control',    'conf_ctrl',       1, True),
    ('Infinite file credit',  'inf_file_cred',   1, True),
    ('Infinite byte credit',  'inf_byte_cred',   1, True),
    ('AutoCallBack @logon',   'auto_callback',   1, True),
    ('TimeLock exempt',       'timelock_exempt', 1, True),
    ('Add new vote topics',   'vote_topics',     1, True),
    ('Add new vote choices',  'vote_choices',    1, True),
    ('Kill/Edit vote topic',  'kill_vote',       1, True),
    ('Edit handle',           'edit_handle',     1, True),
    ('Edit name, bday, sex',  'edit_name',       1, True),
    ('Edit address, st/zip',  'edit_address',    1, True),
    # Column 2
    ('Expansion 3',           'expansion3',      2, True),
    ('Expansion 4',           'expansion4',      2, True),
    ('Edit voice phone#',     'edit_voice_ph',   2, True),
    ('Edit data phone#',      'edit_data_ph',    2, True),
    ('Allow WHO banner',      'who_banner',      2, True),
    ('Use TermLink',          'termlink',        2, True),
    ('Monitor another port',  'monitor_port',    2, True),
    ('Alarm sysop @logon',    'alarm_sysop',     2, True),
    ('Open screen @logon',    'open_screen',     2, True),
    ('Open capture @logon',   'open_capture',    2, True),
    ('Send FIDO NetMail',     'fido_netmail',    2, True),
    ('Send Internet Mail',    'inet_mail',       2, True),
    ('FIDO FReq and Attach',  'fido_freq',       2, True),
    ('Hold and Crash mail',   'hold_crash',      2, True),
    ('NetMail Cost exempt',   'netmail_exempt',  2, True),
    ('Costs are NetCredits',  'costs_netcred',   2, True),
    ('Receive DL rewards',    'dl_rewards',      2, True),
    ('May page the sysop',    'page_sysop',      2, True),
    ('Broadcast OLMs',        'broadcast_olm',   2, True),
    ('SuperUser',             'superuser',       2, False),
]

ALL_AG_PRIV = _AG_PRIV_DEFS


def make_eg_priv_fields() -> list[VDEField]:
    """
    Access Group privilege flags — 3-column screen.
    Values cycle: No → Yes → Sub (inherited).
    Includes << Exit and << Previous screen nav items.
    """
    fields = [
        VDEField('<< Exit',            'nav', '__exit__'),
        VDEField('<< Previous screen', 'nav', '__prev__'),
    ]
    for label, key, col, sub_ok in ALL_AG_PRIV:
        if sub_ok:
            # 3-state: No=0, Yes=1, Sub=2
            fields.append(VDEField(
                label, 'bool3', f'__agpriv_{key}__',
                width=3, choices=['No', 'Yes', 'Sub'], col=col
            ))
        else:
            # 2-state: No=0, Yes=1
            fields.append(VDEField(
                label, 'bool', f'__agpriv_{key}__',
                width=3, col=col
            ))
    return fields


def make_eg_priv_data(group: dict) -> dict:
    """Build data dict for AG privilege form from group['ag_privs'] bitmask or dict."""
    group = _row(group)
    ag_privs = group.get('ag_privs') or {}
    data = {}
    for label, key, col, sub_ok in ALL_AG_PRIV:
        data[f'__agpriv_{key}__'] = ag_privs.get(key, 0)
    return data


def pack_eg_priv_data(priv_data: dict) -> dict:
    """Convert form data back to ag_privs dict."""
    out = {}
    for label, key, col, sub_ok in ALL_AG_PRIV:
        val = priv_data.get(f'__agpriv_{key}__', 0)
        try:
            out[key] = int(val or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


# ─────────────────────────────────────────────────────────────────────────────
# EG Edit Limits/Ratios — Access Group limits  (2-column)
# As seen in screenshot 4.
# ─────────────────────────────────────────────────────────────────────────────

def make_eg_limits_fields() -> list[VDEField]:
    """
    Access Group limits/ratios screen — 2-column layout.
    Left col:  message base/file base flags (ghosted), network aliases,
               download/upload limits, file/byte credit ratios, CallerID, dict.
    Right col: calls/day, min/call, mins/day, mins idle, messages/call,
               feedbacks/call, editor lines, max email, inactivity days,
               lines per signature, daily door minutes, send log to user#.
    """
    return [
        VDEField('<< Exit',              'nav', '__exit__'),
        VDEField('<< Previous screen',   'nav', '__prev__'),

        # ── Left column ──────────────────────────────────────────────────────
        VDEField('Message base flags',   'str',  None,              width=8,  col=0,
                 implemented=False),
        VDEField('File base flags',      'str',  None,              width=8,  col=0,
                 implemented=False),
        VDEField('Other flags',          'str',  None,              width=8,  col=0,
                 implemented=False),
        VDEField('Log verbosity flags',  'str',  None,              width=8,  col=0,
                 implemented=False),
        VDEField('Network aliases',      'int',  'net_aliases',     width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Downloads/day',        'int',  'dl_per_day',      width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('DownBytes/day',        'int',  'dl_bytes_day',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Uploads/day',          'int',  'ul_per_day',      width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('UpBytes/day',          'int',  'ul_bytes_day',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('File credit ratio 1',  'int',  'file_ratio_1',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Byte credit ratio 1',  'int',  'byte_ratio_1',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('File credit ratio 2',  'int',  'file_ratio_2',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Byte credit ratio 2',  'int',  'byte_ratio_2',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('File credit ratio 3',  'int',  'file_ratio_3',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Byte credit ratio 3',  'int',  'byte_ratio_3',    width=5,  col=0,
                 min_val=0, max_val=9999),
        VDEField('Use of CallerID#',     'str',  None,              width=12, col=0,
                 implemented=False),
        VDEField('Dictionary entries',   'int',  'dict_entries',    width=5,  col=0,
                 min_val=0, max_val=9999),

        # ── Right column ─────────────────────────────────────────────────────
        VDEField('Calls/day (0-999)',    'int',  'calls_per_day',   width=5,  col=1,
                 min_val=0, max_val=999),
        VDEField('Min/call   (5-999)',   'int',  'min_per_call',    width=5,  col=1,
                 min_val=5, max_val=999),
        VDEField('Mins/day   (0-999)',   'int',  'min_per_day',     width=5,  col=1,
                 min_val=0, max_val=999),
        VDEField('Mins idle  (0-999)',   'int',  'min_idle',        width=5,  col=1,
                 min_val=0, max_val=999),
        VDEField('Messages/call',        'int',  'msgs_per_call',   width=5,  col=1,
                 min_val=0, max_val=9999),
        VDEField('Feedbacks/call',       'int',  'feedbacks_call',  width=5,  col=1,
                 min_val=0, max_val=9999),
        VDEField('Editor lines',         'int',  'editor_lines',    width=5,  col=1,
                 min_val=0, max_val=9999),
        VDEField('Maximum email (KB)',   'int',  'max_email_kb',    width=5,  col=1,
                 min_val=0, max_val=9999),
        VDEField('Inactivity days',      'int',  'inactivity_days', width=5,  col=1,
                 min_val=0, max_val=9999),
        VDEField('Lines per signature',  'int',  'sig_lines',       width=5,  col=1,
                 min_val=0, max_val=100),
        VDEField('Daily door minutes',   'int',  'door_mins_day',   width=5,  col=1,
                 min_val=0, max_val=9999),
        VDEField('Send log to user#',    'int',  'log_to_user',     width=5,  col=1,
                 min_val=0, max_val=9999),
    ]


def make_eg_limits_data(group: dict) -> dict:
    group = _row(group)
    lim = group.get('ag_limits') or {}
    return {
        'net_aliases':    lim.get('net_aliases', 0),
        'dl_per_day':     lim.get('dl_per_day', 0),
        'dl_bytes_day':   lim.get('dl_bytes_day', 0),
        'ul_per_day':     lim.get('ul_per_day', 0),
        'ul_bytes_day':   lim.get('ul_bytes_day', 0),
        'file_ratio_1':   lim.get('file_ratio_1', 2),
        'byte_ratio_1':   lim.get('byte_ratio_1', 0),
        'file_ratio_2':   lim.get('file_ratio_2', 0),
        'byte_ratio_2':   lim.get('byte_ratio_2', 0),
        'file_ratio_3':   lim.get('file_ratio_3', 0),
        'byte_ratio_3':   lim.get('byte_ratio_3', 0),
        'dict_entries':   lim.get('dict_entries', 0),
        'calls_per_day':  lim.get('calls_per_day', 0),
        'min_per_call':   lim.get('min_per_call', 999),
        'min_per_day':    lim.get('min_per_day', 360),
        'min_idle':       lim.get('min_idle', 20),
        'msgs_per_call':  lim.get('msgs_per_call', 0),
        'feedbacks_call': lim.get('feedbacks_call', 99),
        'editor_lines':   lim.get('editor_lines', 250),
        'max_email_kb':   lim.get('max_email_kb', 0),
        'inactivity_days':lim.get('inactivity_days', 0),
        'sig_lines':      lim.get('sig_lines', 0),
        'door_mins_day':  lim.get('door_mins_day', 0),
        'log_to_user':    lim.get('log_to_user', 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# News item VDE screen  (AT # command from News> prompt)
# Matches screenshot: Item type header, DOS filename, Description, Arguments,
# then 2-col flags layout.
# ─────────────────────────────────────────────────────────────────────────────

def make_news_item_fields() -> list[VDEField]:
    """
    Fields for the news item AT screen.
    Layout matches the CNet screenshot exactly:
      Row 1 header: Item type (ghosted — always Text for news)
      Nav: << Exit
      Left col : DOS filename, Description, Arguments (ghosted), sep,
                 Access groups, Flags required, Post date, Item use rate#,
                 Debit daily time (ghosted), Disable MCI, Disable More?,
                 Enable SkyPix (ghosted), Raw console startup (ghosted)
      Right col: Purge date, Item disabled, One user at a time (ghosted),
                 Disable word-wrap, Disable sysop MCI, Delete when purged
    """
    return [
        # ── Nav ──────────────────────────────────────────────────────────────
        VDEField('<< Exit', 'nav', '__exit__'),

        # ── Left column (col=0) ──────────────────────────────────────────────
        VDEField('DOS filename',       'str',  'filename',          width=30, col=0),
        VDEField('Description',        'str',  'title',             width=50, col=0),
        VDEField('Arguments',          'str',  None,                width=30, col=0, implemented=False),
        VDEField('',                   'sep',                                 col=0),
        VDEField('Access groups',      'str',  'access_groups',     width=10, col=0),
        VDEField('Flags required',     'str',  'flags_required',    width=10, col=0),
        VDEField('Post date',          'str',  'post_date',         width=17, col=0),
        VDEField('Item use rate#',     'int',  'item_use_rate',     width=2,  col=0,
                 min_val=0, max_val=3),
        VDEField('Debit daily time',   'str',  None,                width=5,  col=0, implemented=False),
        VDEField('Disable MCI',        'bool', 'disable_mci',                col=0),
        VDEField('Disable More?',      'bool', 'disable_more',               col=0),
        VDEField('Enable SkyPix',      'bool', None,                         col=0, implemented=False),
        VDEField('Raw console startup','str',  None,                width=5,  col=0, implemented=False),

        # ── Right column (col=1) ─────────────────────────────────────────────
        # Spacers to align Purge date with Post date row (row 7 = Post date)
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('', 'sep', col=1),
        VDEField('Purge date',         'str',  'purge_date',        width=17, col=1),
        VDEField('Item disabled',      'bool', 'item_disabled',              col=1),
        VDEField('One user at a time', 'bool', None,                         col=1, implemented=False),
        VDEField('Disable word-wrap',  'bool', 'disable_wordwrap',           col=1),
        VDEField('Disable sysop MCI',  'bool', 'disable_sysop_mci',          col=1),
        VDEField('Delete when purged', 'bool', 'delete_when_purged',         col=1),
    ]


def make_news_item_data(item) -> dict:
    """Build data dict for a news item VDE form from a DB row or dict."""
    item = _row(item)
    return {
        'filename':          item.get('filename', '') or '',
        'title':             item.get('title', '') or '',
        'access_groups':     item.get('access_groups', '0-31') or '0-31',
        'flags_required':    item.get('flags_required', '') or '',
        'post_date':         item.get('post_date', '') or '',
        'item_use_rate':     item.get('item_use_rate', 0) or 0,
        'disable_mci':       item.get('disable_mci', 0) or 0,
        'disable_more':      item.get('disable_more', 0) or 0,
        'purge_date':        item.get('purge_date', '') or '',
        'item_disabled':     item.get('item_disabled', 0) or 0,
        'disable_wordwrap':  item.get('disable_wordwrap', 0) or 0,
        'disable_sysop_mci': item.get('disable_sysop_mci', 0) or 0,
        'delete_when_purged':item.get('delete_when_purged', 0) or 0,
    }
