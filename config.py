"""
config.py — ANet BBS Central Configuration
Edit this file to customize your BBS. All settings live here so nothing
is hardcoded across the codebase.
"""

from pathlib import Path

# Absolute path to the directory this file lives in
BASE_DIR = Path(__file__).parent


class Config:
    # -------------------------------------------------------------------------
    # BBS Identity
    # -------------------------------------------------------------------------
    BBS_NAME        = "ANet BBS"
    SYSOP_HANDLE    = "SysOp"       # Handle for the sysop account (created on first run)

    # -------------------------------------------------------------------------
    # Network — Telnet
    # -------------------------------------------------------------------------
    TELNET_HOST     = "0.0.0.0"     # Listen on all interfaces; use "127.0.0.1" for local-only
    TELNET_PORT     = 2323          # Change to 23 for production (requires root or authbind)

    # -------------------------------------------------------------------------
    # Nodes (simultaneous callers)
    # -------------------------------------------------------------------------
    MAX_NODES       = 15            # Maximum callers connected at once; change freely

    # -------------------------------------------------------------------------
    # Paths
    # -------------------------------------------------------------------------
    DATA_DIR        = BASE_DIR / "data"
    DB_PATH         = DATA_DIR / "anet.db"
    BBSTEXT_PATH    = DATA_DIR / "bbstext.txt"

    # -------------------------------------------------------------------------
    # Session timeouts (seconds)
    # -------------------------------------------------------------------------
    LOGIN_TIMEOUT   = 120           # Disconnect if login isn't completed within this time
    SESSION_TIMEOUT = 3600          # Disconnect idle logged-in sessions after this time
    MAX_LOGIN_TRIES = 3             # Failed password attempts before forced disconnect

    # -------------------------------------------------------------------------
    # Access Groups
    # -------------------------------------------------------------------------
    # Access groups mirror CNet's 0–31 system.
    # 0  = lowest (new/unvalidated users)
    # 31 = highest (sysop)
    DEFAULT_NEW_USER_AG = 5
    SYSOP_AG            = 31
