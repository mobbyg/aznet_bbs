"""
server/telnet.py — Asyncio Telnet Server

ECHO STRATEGY
─────────────────────────────────────────────────────────────────────────────
Different telnet clients handle echo very differently:

  • Linux `telnet` command  — line-mode by default; client echoes its own
    input and sends a full line when Enter is pressed.

  • SyncTerm and most BBS clients — character-mode; they send one byte per
    keypress and expect THE SERVER to echo each character back.  If the
    server claims WILL ECHO but doesn't actually echo, the user sees nothing.

Our approach: server-side echo always on.
  1. We send IAC WILL ECHO at connection time → clients disable local echo.
  2. TelnetReader._process() echoes every printable character back and
     handles backspace visually (backspace-space-backspace).
  3. readline() drains the write buffer after each chunk so echo appears
     instantly, character by character.
  4. For password fields, session.py sets reader.echo = False which
     suppresses the echo server-side (client already handed us control).

LINE ENDING NORMALISATION
─────────────────────────────────────────────────────────────────────────────
RFC 854 says Enter must be sent as CR LF (\r\n) or CR NUL (\r\0).
Many clients send \r\0 or bare \r.  _process() normalises all of these
to \r\n so readline() always finds a consistent line terminator.
The ^M^M^M repeating symptom means \r\0 was not being consumed as a pair —
the \0 was falling through as a separate pass and triggering readline again.
"""

import asyncio
import logging

from config import Config
from server.terminal import BBSText
from server.session import BBSSession

log = logging.getLogger('anet.telnet')

# Telnet command bytes
IAC  = 0xFF
WILL = 0xFB
WONT = 0xFC
DO   = 0xFD
DONT = 0xFE
SB   = 0xFA   # Start sub-negotiation
SE   = 0xF0   # End sub-negotiation

# Telnet option codes
OPT_ECHO  = 1
OPT_SGA   = 3    # Suppress Go-Ahead
OPT_TTYPE = 24
OPT_NAWS  = 31   # Negotiate About Window Size


def _iac(*bytes_) -> bytes:
    return bytes([IAC, *bytes_])


# Sent immediately on connection.
# WILL ECHO  — tells client to stop its own local echo; we handle it.
# WILL SGA   — full-duplex mode (suppress go-ahead).
# DO   SGA   — ask client to also suppress go-ahead.
# DO   NAWS  — ask client to report its terminal dimensions.
_INITIAL_NEGOTIATION = (
    _iac(WILL, OPT_ECHO) +
    _iac(WILL, OPT_SGA)  +
    _iac(DO,   OPT_SGA)  +
    _iac(DO,   OPT_NAWS)
)


# --------------------------------------------------------------------------
# Telnet stream wrapper
# --------------------------------------------------------------------------

class TelnetReader:
    """
    Wraps asyncio.StreamReader to strip IAC, normalise line endings,
    and echo characters back to the client as they are typed.

    Public attribute:
        echo : bool  — when True, characters are echoed to the client.
                       session.py sets this False for password fields.
    """

    _BS_SEQ = bytes([0x08, 0x20, 0x08])   # backspace, space, backspace

    def __init__(self, raw_reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._raw    = raw_reader
        self._writer = writer
        self._buf    = bytearray()
        self.echo    = True

    async def readline(self) -> bytes:
        """
        Read until \\r\\n, return the line including the terminator.
        Echoes characters back as each chunk arrives.
        """
        while True:
            idx = self._buf.find(b'\r\n')
            if idx != -1:
                line = bytes(self._buf[:idx + 2])
                del self._buf[:idx + 2]
                return line

            chunk = await self._raw.read(256)
            if not chunk:
                return b''
            self._process(chunk)

            # Flush echoed bytes immediately so user sees them while typing
            try:
                await self._writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return b''

    async def read(self, n: int = 1) -> bytes:
        """Read up to n bytes of clean (IAC-stripped) data."""
        while len(self._buf) < n:
            chunk = await self._raw.read(256)
            if not chunk:
                return bytes(self._buf)
            self._process(chunk)
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    def _process(self, chunk: bytes) -> None:
        """
        Walk raw bytes:
          • Parse/respond to IAC sequences (never reach clean buffer).
          • Normalise \r\0, \r\n, bare \r  →  \r\n in clean buffer.
          • Echo printable chars; handle backspace visually.
        """
        i = 0
        n = len(chunk)

        while i < n:
            b = chunk[i]

            # ── IAC sequence ─────────────────────────────────────────────
            if b == IAC:
                if i + 1 >= n:
                    break
                verb = chunk[i + 1]

                if verb in (WILL, WONT, DO, DONT):
                    if i + 2 >= n:
                        break
                    self._handle_negotiation(verb, chunk[i + 2])
                    i += 3

                elif verb == SB:
                    # Find IAC SE terminator
                    end = i + 2
                    while end < n - 1:
                        if chunk[end] == IAC and chunk[end + 1] == SE:
                            break
                        end += 1
                    self._handle_subneg(chunk[i + 2: end])
                    i = end + 2

                elif verb == IAC:
                    # Escaped 0xFF literal
                    self._buf.append(IAC)
                    if self.echo:
                        self._writer.write(bytes([IAC]))
                    i += 2

                else:
                    i += 2   # unknown 2-byte command, skip

            # ── Carriage return — normalise all Enter variants ────────────
            elif b == 0x0D:
                # Consume the byte that follows \r so we handle it as a unit
                if i + 1 < n:
                    next_b = chunk[i + 1]
                    if next_b in (0x00, 0x0A):
                        i += 2   # skip \r\0 or \r\n pair
                    else:
                        i += 1   # bare \r
                else:
                    i += 1       # \r at very end of chunk

                self._buf += b'\r\n'
                if self.echo:
                    self._writer.write(b'\r\n')

            # ── Backspace / Delete ────────────────────────────────────────
            elif b in (0x08, 0x7F):
                if self._buf and self._buf[-1] not in (0x0A, 0x0D):
                    self._buf.pop()
                    if self.echo:
                        self._writer.write(self._BS_SEQ)
                i += 1

            # ── Null / telnet padding — discard ───────────────────────────
            elif b == 0x00:
                i += 1

            # ── Printable character ───────────────────────────────────────
            else:
                self._buf.append(b)
                if self.echo:
                    self._writer.write(bytes([b]))
                i += 1

    def _handle_negotiation(self, verb: int, option: int) -> None:
        if verb == DO and option == OPT_ECHO:
            # Client confirming it wants us to echo — we already said WILL,
            # no reply needed.
            pass

        elif verb == DONT and option == OPT_ECHO:
            # Client asking us to stop echoing — acknowledge
            self._writer.write(_iac(WONT, OPT_ECHO))

        elif verb == WILL and option == OPT_NAWS:
            self._writer.write(_iac(DO, OPT_NAWS))

        elif verb == WILL and option == OPT_SGA:
            pass

        elif verb == WONT:
            self._writer.write(_iac(DONT, option))

        elif verb == WILL:
            self._writer.write(_iac(DONT, option))

        elif verb == DO:
            if option not in (OPT_ECHO, OPT_SGA):
                self._writer.write(_iac(WONT, option))

    def _handle_subneg(self, data: bytes) -> None:
        if not data:
            return
        if data[0] == OPT_NAWS and len(data) >= 5:
            cols = (data[1] << 8) | data[2]
            rows = (data[3] << 8) | data[4]
            log.debug("NAWS: %d cols x %d rows", cols, rows)


# --------------------------------------------------------------------------
# Node slot manager
# --------------------------------------------------------------------------

class NodeManager:
    def __init__(self, max_nodes: int):
        self._max   = max_nodes
        self._slots: set[int] = set()

    def acquire(self) -> int | None:
        for i in range(self._max):
            if i not in self._slots:
                self._slots.add(i)
                return i
        return None

    def release(self, node_id: int) -> None:
        self._slots.discard(node_id)

    @property
    def active_count(self) -> int:
        return len(self._slots)


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class TelnetServer:
    def __init__(self):
        self._nodes   = NodeManager(Config.MAX_NODES)
        self._bbstext = BBSText(Config.BBSTEXT_PATH)
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            client_connected_cb=self._handle_connection,
            host=Config.TELNET_HOST,
            port=Config.TELNET_PORT,
        )
        addr = self._server.sockets[0].getsockname()
        log.info("ANet Telnet server listening on %s:%d", *addr)
        log.info("Max nodes: %d", Config.MAX_NODES)

    async def serve_forever(self) -> None:
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Telnet server stopped.")

    async def _handle_connection(
        self,
        raw_reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer     = writer.get_extra_info('peername')
        peer_str = f"{peer[0]}:{peer[1]}" if peer else "unknown"

        node_id = self._nodes.acquire()
        if node_id is None:
            log.warning("All nodes full — rejecting %s", peer_str)
            writer.write(b"\r\nSorry, all lines are busy.  Please try again later.\r\n")
            await writer.drain()
            writer.close()
            return

        try:
            writer.write(_INITIAL_NEGOTIATION)
            await writer.drain()
            await asyncio.sleep(0.1)   # let client process negotiation

            clean_reader = TelnetReader(raw_reader, writer)
            session = BBSSession(
                node_id=node_id,
                reader=clean_reader,   # type: ignore[arg-type]
                writer=writer,
                bbstext=self._bbstext,
                peer=peer_str,
            )

            log.info("Node %d assigned to %s  (%d/%d nodes active)",
                     node_id, peer_str,
                     self._nodes.active_count, Config.MAX_NODES)

            await session.run()

        finally:
            self._nodes.release(node_id)
            log.info("Node %d released", node_id)
