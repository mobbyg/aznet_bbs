"""
run_server.py — ANet BBS Entry Point

Usage:
    python3 run_server.py

This script:
  1. Sets up logging so you can see what's happening.
  2. Initialises the SQLite database (creates it on first run).
  3. Starts the Telnet server.
  4. Runs the asyncio event loop until you press Ctrl+C.

HOW asyncio.run() WORKS:
─────────────────────────────────────────────────────────────────────────────
asyncio.run(main()) does three things:
  1. Creates a brand new event loop.
  2. Runs main() as the first coroutine.
  3. Closes the loop and cleans up when main() returns.

Inside main(), we start the server and then call serve_forever(), which
suspends main() until the server is stopped.  While main() is suspended,
the event loop is free to handle all incoming connections.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import sys

from config import Config
from server import database as db
from server.telnet import TelnetServer


def setup_logging() -> None:
    """Configure logging to console with timestamps."""
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    logging.basicConfig(
        level   = logging.INFO,
        format  = fmt,
        datefmt = "%Y-%m-%d %H:%M:%S",
        stream  = sys.stdout,
    )


async def main() -> None:
    setup_logging()
    log = logging.getLogger('anet')

    log.info("=" * 60)
    log.info("  %s  — starting up", Config.BBS_NAME)
    log.info("=" * 60)

    # ── Database ──────────────────────────────────────────────────────────
    # init_db() is safe to call every time — it only creates tables and the
    # sysop account if they don't already exist.
    db.init_db()

    # Initialise message base tables (subboards, messages, board_visits).
    # Done separately to avoid a circular import: msgbase imports database,
    # so database cannot import msgbase.
    from server.msgbase import init_message_tables
    init_message_tables()

    # ── Telnet server ─────────────────────────────────────────────────────
    server = TelnetServer()
    await server.start()

    log.info("Ready.  Connect with:  telnet localhost %d", Config.TELNET_PORT)
    log.info("Press Ctrl+C to shut down.\n")

    try:
        await server.serve_forever()
    except KeyboardInterrupt:
        log.info("\nShutdown requested (Ctrl+C).")
    finally:
        await server.stop()
        log.info("%s stopped.  Goodbye.", Config.BBS_NAME)


if __name__ == "__main__":
    asyncio.run(main())
