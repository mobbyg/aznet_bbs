"""
run_panel.py — ANet BBS SysOp Control Panel Launcher

Usage:
    python3 run_panel.py

Launches the SysOp control panel GUI independently of the BBS server.
The panel reads the SQLite database directly, so it shows live data
whenever the server is running and shows an empty node list when it isn't.

You can run the panel and the server simultaneously in two terminals:
    Terminal 1: python3 run_server.py
    Terminal 2: python3 run_panel.py
"""

import sys
from sysop.main import main

if __name__ == "__main__":
    main()
