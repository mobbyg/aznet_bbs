"""
server/session.py — Per-Caller Session State Machine

This is the heart of the BBS.  Every time a user connects, a new BBSSession
object is created and its run() coroutine is launched as an asyncio Task.

ASYNCIO PRIMER (READ THIS):
─────────────────────────────────────────────────────────────────────────────
asyncio runs everything in a single thread using an "event loop".  Instead of
blocking the whole program when we wait for a user to type, we use `await` to
pause THIS task and let other tasks run.

  await writer.drain()      → "pause here until the network buffer clears"
  await self.readline()     → "pause here until the user presses Enter"
  await asyncio.sleep(1)    → "pause here for 1 second"

None of these block other callers.  While one session is waiting for input,
the event loop handles all the other sessions.

A session has STATES.  It starts at LOGIN and moves forward:
  LOGIN_ENTER → LOGIN_PASSWORD → MAIN_MENU → (future states)

We implement this as a simple dispatch loop rather than a complex state
machine framework — Python async/await makes sequential code readable enough
that we can just write it as a coroutine that flows from top to bottom.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
from datetime import datetime

from config import Config
from server.terminal import BBSText, BBSMenu
from server import database as db
from server.database import write_activity
from server.boards import BoardArea
from server.systext import SystextFile
from server import news as news_mod
from server import mail as mail_mod
from server.vde import VDESession

log = logging.getLogger('anet.session')


class BBSSession:
    """
    Represents one connected caller.

    Args:
        node_id : integer node slot assigned to this connection (0-based)
        reader  : asyncio.StreamReader — incoming bytes from the client
        writer  : asyncio.StreamWriter — outgoing bytes to the client
        bbstext : shared BBSText instance loaded from bbstext.txt
        peer    : string like "192.168.1.5:54321" for logging
    """

    def __init__(
        self,
        node_id: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        bbstext: BBSText,
        bbsmenu: BBSMenu,
        systext: SystextFile,
        peer: str,
    ):
        self.node_id   = node_id
        self.reader    = reader
        self.writer    = writer
        self.bbstext   = bbstext
        self.bbsmenu   = bbsmenu
        self.systext   = systext
        self.peer      = peer

        # User state (populated after login)
        self.user_id      : int | None = None
        self.handle       : str        = ''
        self.access_group : int        = 0
        self.location     : str        = peer.split(':')[0]   # default to IP
        self.term_type    : str        = 'IBM'                # set in _run_terminal_detection
        self._connected_at: str        = datetime.utcnow().isoformat()
        self._time_warned : bool       = False

        # Terminal preferences (loaded from DB after login; defaults until then)
        self.ansi_level   : str  = 'Simple'
        self.needs_lf     : bool = False
        self.screen_width : int  = 80
        self.screen_height: int  = 24
        self.ansi_color   : bool = True
        self.ansi_tabs    : bool = False

    # -----------------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main coroutine for this session.  Called once per connection.
        Handles the full lifecycle: login → menu → disconnect.

        Any unhandled exception is caught here so one bad session never
        crashes the server.
        """
        log.info("Node %d — connection from %s", self.node_id, self.peer)
        write_activity(f"Connection from {self.peer}", self.node_id)
        db.register_node_waiting(self.node_id)

        try:
            await self._run_login()
        except asyncio.TimeoutError:
            await self.send_line("\r\nIdle timeout. Goodbye.\r\n")
        except (ConnectionResetError, BrokenPipeError):
            log.info("Node %d — connection dropped by client", self.node_id)
        except Exception as exc:
            log.exception("Node %d — unhandled error: %s", self.node_id, exc)
        finally:
            db.clear_node(self.node_id)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            log.info("Node %d — session ended", self.node_id)
            write_activity(f"Session ended  (user: {self.handle or 'none'})", self.node_id)

    # -----------------------------------------------------------------------
    # Login sequence
    # Mirrors the CNet login flow from Chapter 5 of the manual.
    # bbstext.txt line numbers are referenced by name for clarity.
    # -----------------------------------------------------------------------

    async def _run_login(self) -> None:
        """
        Full login sequence per the CNet manual Chapter 5:

          1. Rec 14  — port banner
          2. Rec 21  — terminal type prompt  [A]MIGA/[C]BM/[I]BM/[S]ky/[NONE]
          3. Rec 22-25 — confirmation of choice
          4. Rec 17  — attribute reset + spacer newlines
          5. sys.start — opening screen (rendered in chosen term type)
          6. Rec 18  — "Press RETURN to enter system"  → wait for ENTER
          7. Handle prompt loop (rec 28 + rec 29)
          8. Rec 42  — "Use previous term settings?" for returning users
                       who pressed ENTER at step 2 (deferred until we know who they are)
        """

        # ── 1. Banner / port announcement (bbstext rec 14) ────────────────
        await self.send(self.bbstext.render(14, {0: self.node_id, 1: Config.BBS_NAME}))
        await self.send_line()

        # ── 2 & 3. Terminal type detection ────────────────────────────────
        pressed_enter = await self._run_terminal_detection()
        # pressed_enter = True if user just hit ENTER (no explicit type chosen).
        # In that case we defer the "use previous?" check until after login.

        # ── 4. Attribute reset + spacer (bbstext rec 17) ──────────────────
        # "\x19f1\x19n8" — clear attributes, 8 blank lines before sys.start
        await self.send(self.bbstext.render(17))

        # ── 5. sys.start — opening screen ─────────────────────────────────
        await self.send(self.systext.render('sys.start'))

        # ── 6. Press RETURN prompt (bbstext rec 18) ────────────────────────
        await self.send(self.bbstext.render(18))
        await self.readline_with_timeout(Config.LOGIN_TIMEOUT)  # wait for any key

        # ── 7. Handle prompt loop ──────────────────────────────────────────
        for attempt in range(Config.MAX_LOGIN_TRIES):
            # Rec 28: "\r\nEnter NEW if you have no account.\r\n"
            if attempt == 0:
                await self.send(self.bbstext.render(28))

            # Rec 29: "Enter your handle.\r\n: "
            await self.send(self.bbstext.render(29))
            handle = await self.readline_with_timeout(Config.LOGIN_TIMEOUT)
            handle = handle.strip()

            if not handle:
                continue

            # ── New user branch ────────────────────────────────────────────
            if handle.upper() == 'NEW':
                await self._run_new_user()
                return

            # ── Existing user: password ────────────────────────────────────
            # Rec 36: "\r\nEnter your password.\r\n: "
            await self.send(self.bbstext.render(36))
            await self.send_noecho_on()
            password = await self.readline_with_timeout(Config.LOGIN_TIMEOUT)
            await self.send_noecho_off()
            await self.send_line()   # newline after hidden password entry
            password = password.strip()

            # Rec 37: "\r\nVerifying..."
            await self.send(self.bbstext.render(37))
            await self.send_line()

            user = db.authenticate_user(handle, password)

            if user is None:
                # Rec 38: "Incorrect password."
                await self.send(self.bbstext.render(38))
                log.warning("Node %d — failed login for handle '%s'", self.node_id, handle)
                continue

            # ── Successful login ───────────────────────────────────────────
            self.user_id      = user['id']
            self.handle       = user['handle']
            self.access_group = user['access_group']

            # Load persisted terminal preferences
            self.ansi_level   = user['ansi_level']   if 'ansi_level'   in user.keys() else 'Simple'
            self.needs_lf     = bool(user['needs_lf']     if 'needs_lf'     in user.keys() else False)
            self.screen_width  = user['screen_width']  if 'screen_width'  in user.keys() else 80
            self.screen_height = user['screen_height'] if 'screen_height' in user.keys() else 24
            self.ansi_color   = bool(user['ansi_color']  if 'ansi_color'  in user.keys() else True)
            self.ansi_tabs    = bool(user['ansi_tabs']   if 'ansi_tabs'   in user.keys() else False)

            # Save last_call / call_count BEFORE updating so sys.welcome
            # shows the previous call info, not the current one.
            prev_last_call  = user['last_call']  or 'first time'
            prev_call_count = user['call_count'] or 0

            # ── 8. Rec 42: "Use previous term settings?" ─────────────────
            # Only offered to returning users who pressed ENTER at the terminal
            # prompt (deferred until we could identify them).
            saved_term = (user['term_type'] or '').upper()
            if pressed_enter and saved_term and saved_term != 'NONE':
                await self.send(self.bbstext.render(42))
                answer = await self.readline_with_timeout(Config.LOGIN_TIMEOUT)
                answer = answer.strip().upper()
                if answer in ('', 'Y', 'YES'):
                    # Use their saved type — show the confirmation record
                    self.term_type = saved_term
                    confirm_rec = {'IBM': 22, 'AMIGA': 23, 'SKY': 24, 'CBM': 25}.get(saved_term)
                    if confirm_rec:
                        await self.send(self.bbstext.render(confirm_rec))
                        await self.send_line()
                # If N/NO → keep self.term_type as 'NONE' (ASCII, already set)

            db.update_last_call(self.user_id)
            db.update_term_type(self.user_id, self.term_type)
            db.update_node_online(
                node_id      = self.node_id,
                user_id      = self.user_id,
                handle       = self.handle,
                location     = self.location,
                access_group = self.access_group,
            )

            log.info("Node %d — '%s' logged in (AG %d, term=%s)",
                     self.node_id, self.handle, self.access_group, self.term_type)
            write_activity(
                f"Login: {self.handle} (AG {self.access_group}, {self.term_type}) "
                f"from {self.location}",
                self.node_id,
            )

            # ── sys.welcome — personalised greeting ────────────────────────
            vars_ = SystextFile.make_variables(
                handle     = self.handle,
                bbs_name   = Config.BBS_NAME,
                last_call  = prev_last_call,
                call_count = prev_call_count,
            )
            await self.send(self.systext.render('sys.welcome', vars_))

            # New mail notification
            await self._check_new_mail()

            # Auto-display news items posted since last call
            await self._show_new_news()

            await self._run_main_menu()
            return

        # ── Too many failed attempts ───────────────────────────────────────
        # Line 42: "\r\n@ Connection closed\r\n"
        await self.send(self.bbstext.render(41))

    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # Terminal type detection
    # -----------------------------------------------------------------------

    async def _run_terminal_detection(self) -> bool:
        """
        Show the CNet terminal type prompt (bbstext rec 21) and set self.term_type.

        Per the manual Chapter 5: "The first prompt to appear is one asking the
        terminal type.  Knowing this right away allows CNet to display the opening
        screen (sys.start) according to the user's choice of terminal type."

        Returns:
            True  — user pressed ENTER without choosing a type (deferred;
                    caller should offer rec 42 "use previous?" after login)
            False — user explicitly chose a type

        ANSI note:  We always send ANSI escape codes regardless of the choice.
        The type is recorded per-user and respected by higher-level features
        (e.g. graphics character set, C/G vs IBM extended chars).
        The confirmation records:
            A / AMIGA  → rec 23  "Amiga/ANSI enabled!"
            C / CBM    → rec 25  "Commodore C/G enabled!"
            I / IBM    → rec 22  "IBM/ANSI enabled!"
            S / SKY    → rec 24  "SkyPix enabled!"
            NONE/blank → no confirmation  (ASCII / dumb terminal)
        """
        # Rec 21: "Terminal [A]MIGA, [C]BM, [I]BM, [S]ky, [NONE]: "
        await self.send(self.bbstext.render(21))
        raw = await self.readline_with_timeout(Config.LOGIN_TIMEOUT)
        choice = raw.strip().upper() if raw else ''

        # Map input to canonical term type and confirmation record number
        if not choice or choice == 'NONE':
            self.term_type = 'NONE'
            return not bool(choice)   # True if blank (deferred), False if 'NONE'

        mapping = {
            'A': ('AMIGA', 23),
            'AMIGA': ('AMIGA', 23),
            'C': ('CBM', 25),
            'CBM': ('CBM', 25),
            'I': ('IBM', 22),
            'IBM': ('IBM', 22),
            'S': ('SKY', 24),
            'SKY': ('SKY', 24),
        }

        if choice in mapping:
            self.term_type, confirm_rec = mapping[choice]
            await self.send(self.bbstext.render(confirm_rec))
            await self.send_line()
        else:
            # Unrecognised → default to IBM/ANSI (most common)
            self.term_type = 'IBM'
            await self.send(self.bbstext.render(22))
            await self.send_line()

        log.debug("Node %d — terminal type: %s", self.node_id, self.term_type)
        return False   # explicit choice was made; no deferred "use previous?" needed

    # -----------------------------------------------------------------------
    # New user registration
    # -----------------------------------------------------------------------

    async def _run_new_user(self) -> None:
        """
        Full CNet-authentic new user registration flow.

        Sequence per CNet manual Chapter 5:
          1. nu0     — "Welcome to the system!"
          2. nu      — "First, we need to ask you about your terminal."
          3. Terminal questions (ansi, lf, width, length, color, tabs)
          4. nu2     — "You will now be asked several personal data items."
          5. Personal data (handle, password, real name, city, country,
                            phone, DOB, gender)
          6. nu3     — preferences intro  (more? mode collected)
          7. nq      — finger question intro + nq0..nq4
          8. nu4     — "Filing your account information..."
          9. Create account
         10. sys.nuser
        """

        T = Config.LOGIN_TIMEOUT

        # ══ 1 & 2. Welcome + terminal intro ══════════════════════════════
        await self.send(self.systext.render('nu0'))   # "Welcome to the system!"
        await self.send(self.systext.render('nu'))    # "First, terminal questions..."

        # ══ 3. Terminal questions ═════════════════════════════════════════

        # ANSI level
        await self.send(self.systext.render('ansi'))
        await self.send(self.bbstext.render(1250))    # "Enter level of ANSI (N/S/F)..."
        ansi_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        ansi_level = {'N': 'None', 'S': 'Simple', 'F': 'Full'}.get(ansi_raw[:1], 'Simple')

        # Line feeds
        await self.send(self.systext.render('lf'))
        await self.send(self.bbstext.render(1249))    # "Does your terminal require linefeeds [Y/n]?"
        lf_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        needs_lf = (lf_raw not in ('N', 'NO'))

        # Screen width
        await self.send(self.systext.render('width'))
        await self.send(self.bbstext.render(1240))    # "Enter the number of characters per line..."
        try:
            screen_w = int((await self.readline_with_timeout(T) or '80').strip())
            screen_w = max(40, min(255, screen_w))
        except ValueError:
            screen_w = 80

        # Screen height
        await self.send(self.systext.render('length'))
        await self.send(self.bbstext.render(1242))    # "Enter the number of lines..."
        try:
            screen_h = int((await self.readline_with_timeout(T) or '24').strip())
            screen_h = max(10, min(99, screen_h))
        except ValueError:
            screen_h = 24

        # ANSI color
        await self.send(self.systext.render('color'))
        await self.send(self.bbstext.render(1252))    # "Does your terminal support ANSI Color [Y/n]?"
        color_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        ansi_color = (color_raw not in ('N', 'NO'))

        # ANSI tabs
        await self.send(self.systext.render('tabs'))
        await self.send(self.bbstext.render(1251))    # "Does your terminal support ANSI Tabs [Y/n]?"
        tabs_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        ansi_tabs = (tabs_raw not in ('N', 'NO'))

        log.debug("Node %d — term prefs: ANSI=%s LF=%s W=%d H=%d Color=%s Tabs=%s",
                  self.node_id, ansi_level, needs_lf, screen_w, screen_h, ansi_color, ansi_tabs)

        # ══ 4. Personal data intro ════════════════════════════════════════
        await self.send(self.systext.render('nu2'))

        # ── Handle ────────────────────────────────────────────────────────
        handle = ''
        while True:
            await self.send(self.systext.render('handle'))
            await self.send(self.bbstext.render(1137))   # "Enter the handle you wish to use."
            h = (await self.readline_with_timeout(T) or '').strip()
            if len(h) < 2:
                await self.send(self.bbstext.render(1148))   # "Please try again."
                continue
            if db.get_user_by_handle(h) is not None:
                await self.send(self.bbstext.render(1135, {0: h}))  # "Somebody already using '%s'!"
                continue
            # Confirm: "Is %s correct [Y/n]?"
            await self.send(self.bbstext.render(1151, {0: h}))
            conf = (await self.readline_with_timeout(T) or '').strip().upper()
            if conf in ('', 'Y', 'YES'):
                handle = h
                break

        # ── Password ──────────────────────────────────────────────────────
        password = ''
        while True:
            await self.send(self.systext.render('password'))
            await self.send(self.bbstext.render(1146))   # "Enter the password you would like to use."
            await self.send_noecho_on()
            pw1 = (await self.readline_with_timeout(T) or '').strip()
            await self.send_noecho_off()
            await self.send_line()
            if len(pw1) < 4:
                await self.send(self.bbstext.render(1148))   # "Please try again."
                continue
            # Confirm
            await self.send(self.bbstext.render(312))    # "Password: "
            await self.send_noecho_on()
            pw2 = (await self.readline_with_timeout(T) or '').strip()
            await self.send_noecho_off()
            await self.send_line()
            if pw1 != pw2:
                await self.send(self.bbstext.render(1148))   # "Please try again."
                continue
            password = pw1
            break

        # ── Real name (first + last) ───────────────────────────────────────
        while True:
            await self.send(self.bbstext.render(1147))   # "Please enter your real FIRST name."
            first = (await self.readline_with_timeout(T) or '').strip()
            await self.send(self.bbstext.render(1149))   # "Please enter your real LAST name."
            last  = (await self.readline_with_timeout(T) or '').strip()
            real_name = f"{first} {last}".strip()
            if not real_name:
                await self.send(self.bbstext.render(1148))
                continue
            await self.send(self.bbstext.render(1151, {0: real_name}))   # "Is %s correct [Y/n]?"
            conf = (await self.readline_with_timeout(T) or '').strip().upper()
            if conf in ('', 'Y', 'YES'):
                break

        # ── City / State ───────────────────────────────────────────────────
        while True:
            await self.send(self.bbstext.render(1153))   # "Please enter the name of your City."
            city  = (await self.readline_with_timeout(T) or '').strip()
            await self.send(self.bbstext.render(1155))   # "Please enter 2 letter State/Province abbr."
            state = (await self.readline_with_timeout(T) or '').strip()
            city_state = f"{city}, {state}".strip(', ') if state else city
            await self.send(self.bbstext.render(1157, {0: city_state}))  # "Is %s correct [Y/n]?"
            conf = (await self.readline_with_timeout(T) or '').strip().upper()
            if conf in ('', 'Y', 'YES'):
                break

        # ── Country ────────────────────────────────────────────────────────
        await self.send(self.systext.render('country'))
        await self.send(self.bbstext.render(1136))   # "Enter abbreviation for your country (??? for help)."
        country = (await self.readline_with_timeout(T) or '').strip().upper() or 'USA'
        location = f"{city_state}, {country}" if city_state else country

        # ── Voice phone ────────────────────────────────────────────────────
        await self.send(self.systext.render('phone'))
        await self.send(self.bbstext.render(1160))   # "Enter the three digit AREA CODE."
        area = (await self.readline_with_timeout(T) or '').strip()
        await self.send(self.bbstext.render(1159))   # "Enter the LOCAL part of your phone number."
        local = (await self.readline_with_timeout(T) or '').strip()
        phone = f"{area}-{local}" if area and local else (area or local or '')

        # ── Date of birth ──────────────────────────────────────────────────
        await self.send(self.systext.render('dob'))
        await self.send(self.bbstext.render(1163))   # "Enter the year during which you were born."
        yr  = (await self.readline_with_timeout(T) or '').strip()
        await self.send(self.bbstext.render(1164))   # "Enter the number of the month..."
        mo  = (await self.readline_with_timeout(T) or '').strip()
        await self.send(self.bbstext.render(1165))   # "Enter the day of the month..."
        day = (await self.readline_with_timeout(T) or '').strip()
        dob = f"{yr}-{mo.zfill(2)}-{day.zfill(2)}" if yr and mo and day else ''

        # ── Gender ─────────────────────────────────────────────────────────
        await self.send(self.bbstext.render(1140))   # "What is your gender?"
        await self.send(self.bbstext.render(1244, {0: 1, 1: 'Female'}))
        await self.send(self.bbstext.render(1244, {0: 2, 1: 'Male'}))
        await self.send(b": ")
        gender_raw = (await self.readline_with_timeout(T) or '').strip()
        gender = 'F' if gender_raw.startswith('1') or gender_raw.upper().startswith('F') else 'M'

        # ── Comp type ──────────────────────────────────────────────────────
        await self.send(self.systext.render('comp'))
        await self.send(self.bbstext.render(1248))   # "Enter the # of a computer type."
        _comp = (await self.readline_with_timeout(T) or '').strip()

        # ══ 5. Preferences ════════════════════════════════════════════════
        await self.send(self.systext.render('nu3'))

        # More? mode
        await self.send(self.systext.render('more'))
        await self.send(self.bbstext.render(1247))   # "Do you want the 'More?' mode enabled [Y/n]?"
        more_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        more_mode = (more_raw not in ('N', 'NO'))

        # ══ 6. Finger questions ═══════════════════════════════════════════
        await self.send(self.systext.render('nq'))
        finger = {}
        for qfile in ('nq0', 'nq1', 'nq2', 'nq3', 'nq4'):
            await self.send(self.systext.render(qfile))
            answer = (await self.readline_with_timeout(T) or '').strip()
            finger[qfile] = answer

        # ══ 7. Filing ════════════════════════════════════════════════════
        await self.send(self.systext.render('nu4'))   # "Filing your account information..."

        # ── Create account ─────────────────────────────────────────────────
        try:
            user_id = db.create_user(
                handle    = handle,
                password  = password,
                real_name = real_name,
                location  = location,
            )
        except ValueError as exc:
            await self.send_line(f"\r\nError: {exc}\r\n")
            return

        db.update_term_type(user_id, self.term_type)

        # Save personal profile fields collected during registration
        db.update_user_profile(
            user_id = user_id,
            phone   = phone,
            dob     = dob,
            gender  = gender,
        )

        # Save finger-file answers (nq0..nq4 → question_num 0..4)
        db.save_finger_answers(user_id, {
            int(qfile[2]): answer
            for qfile, answer in finger.items()
        })

        self.user_id      = user_id
        self.handle       = handle
        self.access_group = Config.DEFAULT_NEW_USER_AG
        self.location     = location

        db.update_node_online(
            node_id      = self.node_id,
            user_id      = user_id,
            handle       = handle,
            location     = location,
            access_group = self.access_group,
        )

        # Send nmail welcome letter as a mail item from SysOp
        nmail_vars = SystextFile.make_variables(
            handle   = handle,
            bbs_name = Config.BBS_NAME,
        )
        nmail_body = self.systext.render('nmail', nmail_vars)
        # Strip ANSI codes to get plain text for the mail body
        import re as _re
        nmail_text = _re.sub(b'\x1b\\[[^m]*m', b'', nmail_body).decode('latin-1', errors='replace')
        nmail_text = nmail_text.replace('\r\n', '\n').replace('\r', '\n').strip()
        sysop_row  = db.get_user_by_handle(Config.SYSOP_HANDLE)
        sysop_id   = sysop_row['id'] if sysop_row else 1
        mail_mod.send_mail(
            from_id     = sysop_id,
            from_handle = Config.SYSOP_HANDLE,
            to_id       = user_id,
            to_handle   = handle,
            subject     = f"Welcome to {Config.BBS_NAME}!",
            body        = nmail_text,
        )

        # rec 1123: "New user process completed."
        await self.send(self.bbstext.render(1123))

        # sys.nuser — shown to every new user
        vars_ = SystextFile.make_variables(
            handle   = handle,
            bbs_name = Config.BBS_NAME,
        )
        await self.send(self.systext.render('sys.nuser', vars_))

        log.info("Node %d — new user '%s' registered (from %s)", self.node_id, handle, location)
        write_activity(
            f"NEW USER: {handle} ({real_name}) from {location}  "
            f"ph:{phone or '?'} dob:{dob or '?'} gender:{gender}",
            self.node_id,
        )
        await self._run_main_menu()

    # -----------------------------------------------------------------------
    # Main Menu
    # -----------------------------------------------------------------------

    async def _run_main_menu(self) -> None:
        """
        Main command level — the top-level BBS prompt.

        Command resolution order:
          1. Try to resolve the full typed input via bbsmenu context 2
             (handles multi-word aliases like "EDIT TERMINAL" → "ET").
          2. If no full match, split on the first space and resolve just the
             verb (handles "KILLNEWS 3" where args follow the command).
          3. Fall back to the raw verb if bbsmenu has no entry at all
             (covers our custom sysop commands not in the original bbsmenu).
        """
        while True:
            # ── Session time limit check ───────────────────────────────────
            if Config.MAX_SESSION_MINUTES > 0:
                elapsed_mins = (datetime.utcnow() -
                                datetime.fromisoformat(self._connected_at)
                               ).total_seconds() / 60
                remaining = Config.MAX_SESSION_MINUTES - elapsed_mins
                if remaining <= 0:
                    # rec 81: "Time limit exceeded."
                    await self.send(self.bbstext.render(81))
                    await self._run_logoff()
                    return
                if remaining <= Config.TIME_WARN_MINUTES and not getattr(self, '_time_warned', False):
                    # rec 153: "Note: you have less than %d minute(s) left!"
                    await self.send(self.bbstext.render(153, {0: int(remaining) + 1}))
                    self._time_warned = True

            # Rec 67: "\r\n(Scan,?=help) CNet: "
            await self.send(self.bbstext.render(67))
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None:
                await self.send_line(b"\r\n[Session timeout]\r\n")
                await self._run_logoff()
                return

            raw = raw.strip()
            if not raw:
                continue

            raw_upper = raw.upper()

            # ── Alias resolution via bbsmenu ──────────────────────────────
            # Try full input first ("EDIT TERMINAL" → "ET"), then verb-only
            # ("KILLNEWS 3" → verb="KILLNEWS", args="3").
            canonical = self.bbsmenu.resolve(raw_upper)
            args      = ''
            if canonical is None:
                parts     = raw_upper.split(None, 1)
                canonical = self.bbsmenu.resolve(parts[0]) or parts[0]
                args      = parts[1] if len(parts) > 1 else ''

            # ── Dispatch ──────────────────────────────────────────────────

            if canonical in ('OFF', 'Q', 'QUIT', 'BYE', 'G', 'GOODBYE', 'LOGOFF', 'RELOGON'):
                await self._run_logoff()
                return

            elif canonical == '?' or canonical == 'HELP':
                await self._send_help()

            elif canonical == 'ET':
                await self._run_et()

            elif canonical == 'EP':
                await self._cmd_ep()

            elif canonical == 'PW':
                await self._cmd_pw()

            elif canonical in ('ST', 'STATUS', 'ACCOUNT'):
                await self._cmd_status()

            elif canonical == 'UPTIME':
                await self._cmd_uptime()

            elif canonical == 'HIDE':
                await self._cmd_hide()

            elif canonical == 'TIME':
                await self._cmd_time()

            elif canonical == 'INFO':
                await self._cmd_info()

            elif canonical in ('FINGER', 'FI'):
                await self._cmd_finger(args)

            elif canonical == 'EF':
                await self._cmd_ef()

            elif canonical == 'FIND':
                await self._cmd_find(args)

            elif canonical == 'NEWS':
                await self._cmd_news()

            elif canonical == 'FEEDBACK':
                await self._cmd_mail_write(to_handle=Config.SYSOP_HANDLE)

            elif canonical == 'NU':
                # Re-read the new user welcome message
                vars_ = SystextFile.make_variables(
                    handle   = self.handle,
                    bbs_name = Config.BBS_NAME,
                )
                await self.send(self.systext.render('sys.nuser', vars_))

            elif canonical in ('POSTNEWS', 'PN'):
                if self.access_group >= 31:
                    await self._cmd_postnews()
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical in ('KILLNEWS', 'KN'):
                if self.access_group >= 31:
                    # Pass reconstructed command+args so handler can parse the number
                    await self._cmd_killnews(f"{canonical} {args}".strip())
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical == 'WHO':
                await self._cmd_who()

            elif canonical in ('B', 'BASE', 'BOARDS'):
                area = BoardArea(self)
                await area.run()

            elif canonical in ('NS', 'NSCAN'):
                await self._cmd_nscan()

            elif canonical in ('MAIL', 'M', 'MS'):
                await self._cmd_mail_area()

            elif canonical == 'MR':
                await self._cmd_mail_read()

            elif canonical == 'NEWBOARD':
                if self.access_group >= 31:
                    await self._cmd_newboard()
                else:
                    await self.send(self.bbstext.render(76))

            # ── VDE sysop commands ─────────────────────────────────────────
            elif canonical in ('EA', 'EU'):
                if self.access_group >= 31:
                    await VDESession(self).cmd_ea(args)
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical in ('KU', 'KILLUSER'):
                if self.access_group >= 31:
                    await VDESession(self).cmd_ku(args)
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical == 'UL':
                if self.access_group >= 31:
                    await VDESession(self).cmd_ul(args)
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical in ('EB', 'EDITBOARD'):
                if self.access_group >= 31:
                    await VDESession(self).cmd_eb(args)
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical in ('KB', 'KILLBOARD'):
                if self.access_group >= 31:
                    await VDESession(self).cmd_kb(args)
                else:
                    await self.send(self.bbstext.render(76))

            elif canonical in ('EG', 'EDITGROUP'):
                if self.access_group >= 31:
                    await VDESession(self).cmd_eg(args)
                else:
                    await self.send(self.bbstext.render(76))

            else:
                # rec 310: "\r\nUnknown command "%s"...\r\n"
                await self.send(self.bbstext.render(310, {0: raw_upper}))

    async def _run_logoff(self) -> None:
        """
        Logoff sequence: render sys.end, log the signoff, then return
        so the caller can close the connection cleanly.
        """
        # sys.end — "Thanks for calling <BBS Name>!  Logging call..."
        vars_ = SystextFile.make_variables(
            handle   = self.handle,
            bbs_name = Config.BBS_NAME,
        )
        await self.send(self.systext.render('sys.end', vars_))

        # bbstext rec 98 — "@ Logoff complete"
        await self.send(self.bbstext.render(98))
        await self.send_line()

        if self.handle:
            log.info("Node %d — '%s' logged off", self.node_id, self.handle)
            write_activity(f"Logoff: {self.handle}", self.node_id)

    async def _send_help(self) -> None:
        """
        Display the main menu help screen from the 'main' systext file.
        The 'main' file is paginated — CNet embeds pagination markers that
        split it into screenfuls with a "Want to see more [Yes]?" prompt.
        Each page is sent in turn; the user can press N to stop early.

        Falls back to a plain text summary if the file is missing.
        """
        vars_ = SystextFile.make_variables(
            handle     = self.handle,
            bbs_name   = Config.BBS_NAME,
            subboard_name = '',
        )
        pages = self.systext.render_pages('main', vars_)

        if not any(pages):
            # Fallback if systext/main is missing
            help_text = (
                "\r\n"
                "  B / BASE  — Message boards\r\n"
                "  WHO       — Who is online\r\n"
                "  Q / OFF   — Logoff\r\n"
                "  ?         — This help\r\n"
                "\r\n"
            )
            await self.send_line(help_text)
            return

        for idx, page in enumerate(pages):
            await self.send(page)
            # After every page except the last, wait for input.
            # The "Want to see more [Yes]?" prompt text is already in the
            # page content (rendered from the systext file).
            if idx < len(pages) - 1:
                answer = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
                if answer is None:
                    return
                if answer.strip().upper() in ('N', 'NO'):
                    return

    async def _run_et(self) -> None:
        """
        ET — Edit Terminal preferences.

        Shows a summary of current terminal settings (bbstext rec 1215) then
        re-runs the same terminal questions used during new user registration
        (ansi, lf, width, height, color, tabs) and saves the updated
        term_type.

        Note: ANSI level, LF flag, width, height, color and tabs are
        collected here but DB columns for them don't exist yet — only
        term_type is persisted for now.  Full ET prefs storage is a next
        step.
        """
        T = Config.SESSION_TIMEOUT

        # ── Show current settings header (rec 1215) ───────────────────────
        # "Here are the terminal settings you've selected:\n"
        await self.send(self.bbstext.render(1215))
        await self.send(self.bbstext.render(1216, {0: self.term_type}))
        await self.send(self.bbstext.render(1246, {0: self.ansi_level}))
        await self.send(self.bbstext.render(1229, {0: "Yes" if self.needs_lf else "No"}))
        await self.send(self.bbstext.render(1230, {0: self.screen_width}))
        await self.send(self.bbstext.render(1231, {0: self.screen_height}))
        await self.send(self.bbstext.render(1232, {0: "Yes" if self.ansi_tabs else "No"}))
        await self.send(self.bbstext.render(1233, {0: "Yes" if self.ansi_color else "No"}))

        # ── Re-run terminal questions ─────────────────────────────────────
        await self.send(self.systext.render('ansi'))
        await self.send(self.bbstext.render(1250))   # "Enter level of ANSI..."
        ansi_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        ansi_level = {'N': 'None', 'S': 'Simple', 'F': 'Full'}.get(ansi_raw[:1], 'Simple')

        await self.send(self.systext.render('lf'))
        await self.send(self.bbstext.render(1249))   # "Does your terminal require linefeeds?"
        lf_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        needs_lf = (lf_raw not in ('N', 'NO'))

        await self.send(self.systext.render('width'))
        await self.send(self.bbstext.render(1240))   # "Enter the number of characters per line..."
        try:
            screen_w = int((await self.readline_with_timeout(T) or '80').strip())
            screen_w = max(40, min(255, screen_w))
        except ValueError:
            screen_w = 80

        await self.send(self.systext.render('length'))
        await self.send(self.bbstext.render(1242))   # "Enter the number of lines..."
        try:
            screen_h = int((await self.readline_with_timeout(T) or '24').strip())
            screen_h = max(10, min(99, screen_h))
        except ValueError:
            screen_h = 24

        await self.send(self.systext.render('color'))
        await self.send(self.bbstext.render(1252))   # "Does your terminal support ANSI Color?"
        color_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        ansi_color = (color_raw not in ('N', 'NO'))

        await self.send(self.systext.render('tabs'))
        await self.send(self.bbstext.render(1251))   # "Does your terminal support ANSI Tabs?"
        tabs_raw = (await self.readline_with_timeout(T) or '').strip().upper()
        ansi_tabs = (tabs_raw not in ('N', 'NO'))

        # ── Show final summary ────────────────────────────────────────────
        await self.send(self.bbstext.render(1215))   # "Here are the terminal settings..."
        await self.send(self.bbstext.render(1216, {0: self.term_type}))
        await self.send(self.bbstext.render(1246, {0: ansi_level}))
        await self.send(self.bbstext.render(1229, {0: 'Yes' if needs_lf else 'No'}))
        await self.send(self.bbstext.render(1230, {0: screen_w}))
        await self.send(self.bbstext.render(1231, {0: screen_h}))
        await self.send(self.bbstext.render(1232, {0: 'Yes' if ansi_tabs else 'No'}))
        await self.send(self.bbstext.render(1233, {0: 'Yes' if ansi_color else 'No'}))

        # ── Save ─────────────────────────────────────────────────────────
        if self.user_id:
            db.update_term_type(self.user_id, self.term_type)
            db.update_term_prefs(
                self.user_id,
                ansi_level   = ansi_level,
                needs_lf     = needs_lf,
                screen_width  = screen_w,
                screen_height = screen_h,
                ansi_color   = ansi_color,
                ansi_tabs    = ansi_tabs,
            )
            # Mirror to session object so other code can read them immediately
            self.ansi_level   = ansi_level
            self.needs_lf     = needs_lf
            self.screen_width  = screen_w
            self.screen_height = screen_h
            self.ansi_color   = ansi_color
            self.ansi_tabs    = ansi_tabs

        log.debug("Node %d — ET updated: ANSI=%s LF=%s W=%d H=%d Color=%s Tabs=%s",
                  self.node_id, ansi_level, needs_lf, screen_w, screen_h, ansi_color, ansi_tabs)

    async def _cmd_pw(self) -> None:
        """PW — change password (quick shortcut, no full EP screen needed)."""
        T = Config.SESSION_TIMEOUT
        await self.send_line(b"\r\n  -- Change Password --\r\n")
        await self.send(b"  New password: ")
        pw1 = await self.readline_with_timeout(T)
        if pw1 is None:
            return
        await self.send(b"  Confirm password: ")
        pw2 = await self.readline_with_timeout(T)
        if pw2 is None:
            return
        if pw1.strip() != pw2.strip():
            await self.send(self.bbstext.render(312))   # "Passwords do not match."
        elif len(pw1.strip()) < 4:
            await self.send_line(b"\r\n  Password must be at least 4 characters.\r\n")
        else:
            db.change_password(self.user_id, pw1.strip())
            await self.send_line(b"\r\n  Password updated.\r\n")

    async def _cmd_status(self) -> None:
        """ST / STATUS — display the caller's current account summary."""
        from datetime import datetime as _dt

        user = db.get_user_by_id(self.user_id)
        if not user:
            await self.send(self.bbstext.render(1108))   # "Unable to find your account!"
            return

        # Connected time this session
        try:
            connected = _dt.fromisoformat(self._connected_at)
            elapsed   = _dt.utcnow() - connected
            mins      = int(elapsed.total_seconds() // 60)
            secs      = int(elapsed.total_seconds() % 60)
            time_str  = f"{mins}m {secs}s"
        except Exception:
            time_str = "?"

        last_call = user['last_call'] or 'Never'
        try:
            lc = _dt.fromisoformat(last_call)
            last_call = lc.strftime('%d-%b-%y %H:%M')
        except (ValueError, TypeError):
            pass

        await self.send_line(b"\r\n")
        await self.send_line(
            b"  \x1b[1;32m" + b"\xc4" * 50 + b"\x1b[0m\r\n"
        )
        await self.send_line(
            f"  \x1b[1;37mHandle    :\x1b[0m  {self.handle}\r\n".encode()
        )
        await self.send_line(
            f"  \x1b[1;37mReal Name :\x1b[0m  {user['real_name'] or '(not set)'}\r\n".encode()
        )
        await self.send_line(
            f"  \x1b[1;37mAccess    :\x1b[0m  Group {self.access_group}\r\n".encode()
        )
        await self.send_line(
            f"  \x1b[1;37mCalls     :\x1b[0m  {user['call_count']}\r\n".encode()
        )
        await self.send_line(
            f"  \x1b[1;37mLast Call :\x1b[0m  {last_call}\r\n".encode()
        )
        await self.send_line(
            f"  \x1b[1;37mTime On   :\x1b[0m  {time_str}\r\n".encode()
        )
        await self.send_line(
            f"  \x1b[1;37mTerminal  :\x1b[0m  {self.term_type}\r\n".encode()
        )
        await self.send_line(
            b"  \x1b[1;32m" + b"\xc4" * 50 + b"\x1b[0m\r\n"
        )

    async def _cmd_uptime(self) -> None:
        """UPTIME — show how long the server has been running."""
        import server as _srv
        from datetime import datetime as _dt, timezone as _tz

        now     = _dt.now(_tz.utc)
        delta   = now - _srv.SERVER_START
        days    = delta.days
        hours   = delta.seconds // 3600
        mins    = (delta.seconds % 3600) // 60
        started = _srv.SERVER_START.strftime('%d-%b-%y %H:%M UTC')

        await self.send(
            f"\r\n  Server up: {days}d {hours}h {mins}m  (since {started})\r\n".encode()
        )

    async def _cmd_hide(self) -> None:
        """
        HIDE — toggle visibility in the WHO listing.

        When hidden:
          - You do not appear in WHO for other users
          - Sysops (AG 31) always see everyone via WHO
          - A * marker appears next to your node entry when viewing your
            own status so you know you're hidden
        """
        now_hidden = db.toggle_node_hidden(self.node_id)
        if now_hidden:
            await self.send_line(
                b"\r\n  \x1b[1;33mYou are now HIDDEN from the WHO listing.\x1b[0m\r\n"
            )
        else:
            await self.send_line(
                b"\r\n  \x1b[1;32mYou are now VISIBLE in the WHO listing.\x1b[0m\r\n"
            )

    async def _cmd_time(self) -> None:
        """
        TIME — show current date/time and session elapsed time.

        Uses bbstext recs 1070-1088 for day/month names and rec 1069 for
        the format string: "%s %2d-%s-%d %2d:%02d%c"
        Produces e.g. "Mon 12-Feb-26 02:30p"
        """
        from datetime import datetime as _dt, timezone as _tz

        bb  = self.bbstext.render
        now = _dt.now(_tz.utc)

        # Day and month names from bbstext (Sun=1071, Mon=1072 … Sat=1070)
        # recs 1070=Sat 1071=Sun 1072=Mon 1073=Tue 1074=Wed 1075=Thu 1076=Fri
        _day_map   = {5: 1070, 6: 1071, 0: 1072, 1: 1073, 2: 1074, 3: 1075, 4: 1076}
        _month_map = {1:1077,2:1078,3:1079,4:1080,5:1081,6:1082,
                      7:1083,8:1084,9:1085,10:1086,11:1087,12:1088}

        day_name   = bb(_day_map[now.weekday()]).decode('latin-1').strip()
        month_name = bb(_month_map[now.month]).decode('latin-1').strip()
        hour_12    = now.hour % 12 or 12
        ampm       = 'a' if now.hour < 12 else 'p'

        # rec 1069: "%s %2d-%s-%d %2d:%02d%c"
        time_str = bb(1069, {
            0: day_name,
            1: now.day,
            2: month_name,
            3: now.year % 100,
            4: hour_12,
            5: now.minute,
            6: ampm,
        })

        # Elapsed session time
        try:
            connected = datetime.fromisoformat(self._connected_at)
            elapsed   = datetime.utcnow() - connected
            e_mins    = int(elapsed.total_seconds() // 60)
            e_secs    = int(elapsed.total_seconds() % 60)
            elapsed_str = f"{e_mins}m {e_secs}s"
        except Exception:
            elapsed_str = "?"

        await self.send(b"\r\n  Time  : ")
        await self.send(time_str + b"\r\n")
        await self.send(f"  On for: {elapsed_str}\r\n".encode())

    async def _cmd_info(self) -> None:
        """INFO — display system information."""
        import sqlite3 as _sql
        from server import database as _db
        from server import msgbase as _mb

        # Live stats from DB
        try:
            with _db.get_connection() as conn:
                user_count  = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE is_deleted=0"
                ).fetchone()[0]
            board_count = len(_mb.get_all_subboards())
        except Exception:
            user_count = board_count = 0

        bb = self.bbstext.render
        hr = b"  \x1b[1;32m" + b"\xc4" * 54 + b"\x1b[0m\r\n"

        await self.send(b"\r\n")
        await self.send(hr)
        await self.send(
            f"  \x1b[1;37mSystem  :\x1b[0m  {Config.BBS_NAME}\r\n".encode()
        )
        await self.send(
            f"  \x1b[1;37mSysOp   :\x1b[0m  {Config.SYSOP_HANDLE}\r\n".encode()
        )
        await self.send(
            f"  \x1b[1;37mSoftware:\x1b[0m  ANet BBS (CNet/5 compatible)\r\n".encode()
        )
        await self.send(
            f"  \x1b[1;37mUsers   :\x1b[0m  {user_count}\r\n".encode()
        )
        await self.send(
            f"  \x1b[1;37mBoards  :\x1b[0m  {board_count}\r\n".encode()
        )
        await self.send(hr)

    async def _cmd_finger(self, handle_arg: str = '') -> None:
        """
        FINGER [handle] — display a user's public profile and finger-file answers.

        Display records:
          171  — "Finger: Enter a user handle.:"  (prompt if no arg)
          149  — "Unable to locate user '%s'."
          115  — "Handle   : %s"
          116  — "Real name: %s (%c)"   (%c = privacy flag, space=public)
          118  — "City/St  : %s"
          120  — "Voice Ph#: %s"
          121  — "Access   : %s"
          122  — "Birthday : %s (%d years)"
          123  — "FirstCall: %s"
          124  — "LastCall : %s"
          125  — "Computer : %s"
          150  — "%d.  %s"              (numbered finger Q&A lines)
          176  — "Detailed [N/y]?"
        """
        from datetime import datetime as _dt, date as _date
        from server import database as _db

        handle = handle_arg.strip()
        if not handle:
            # rec 171: "Finger: Enter a user handle.:"
            await self.send(self.bbstext.render(171))
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None or not raw.strip():
                return
            handle = raw.strip()

        user = _db.get_user_by_handle(handle)
        if not user:
            # rec 149: "Unable to locate user '%s'."
            await self.send(self.bbstext.render(149, {0: handle}))
            return

        bb  = self.bbstext.render

        def _fmt_date(iso):
            if not iso: return 'Never'
            try:   return _dt.fromisoformat(iso).strftime('%d-%b-%y %H:%M')
            except: return str(iso)[:16]

        # Calculate age from DOB
        age_str = ''
        dob_str = user['dob'] or ''
        if dob_str:
            try:
                dob  = _dt.strptime(dob_str[:10], '%Y-%m-%d').date()
                today = _date.today()
                age  = today.year - dob.year - (
                    (today.month, today.day) < (dob.month, dob.day)
                )
                age_str = dob_str[:10]
                age_val = age
            except Exception:
                age_str = dob_str
                age_val = 0
        else:
            age_val = 0

        ag_label = f"Group {user['access_group']}"
        computer = user['term_type'] or ''

        await self.send(b"\r\n")
        await self.send(b"  \x1b[1;32m" + b"\xc4" * 54 + b"\x1b[0m\r\n")
        # rec 115: "Handle   : %s"
        await self.send(b"  " + bb(115, {0: user['handle']}))
        # rec 116: "Real name: %s (%c)"
        await self.send(b"  " + bb(116, {0: user['real_name'] or '(private)', 1: ' '}))
        # rec 118: "City/St  : %s"
        if user['location']:
            await self.send(b"  " + bb(118, {0: user['location']}))
        # rec 120: "Voice Ph#: %s"
        if user['phone']:
            await self.send(b"  " + bb(120, {0: user['phone']}))
        # rec 121: "Access   : %s"
        await self.send(b"  " + bb(121, {0: ag_label}))
        # rec 122: "Birthday : %s (%d years)"
        if age_str:
            await self.send(b"  " + bb(122, {0: age_str, 1: age_val}))
        # rec 123: "FirstCall: %s"
        await self.send(b"  " + bb(123, {0: _fmt_date(user['created_at'])}))
        # rec 124: "LastCall : %s"
        await self.send(b"  " + bb(124, {0: _fmt_date(user['last_call'])}))
        # rec 125: "Computer : %s"
        if computer:
            await self.send(b"  " + bb(125, {0: computer}))
        await self.send(b"  \x1b[1;32m" + b"\xc4" * 54 + b"\x1b[0m\r\n")

        # Finger file Q&A — rec 176: "Detailed [N/y]?"
        await self.send(bb(176))
        raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
        if raw and raw.strip().upper() in ('Y', 'YES'):
            answers = _db.get_finger_answers(user['id'])
            q_names = {
                0: 'Occupation',
                1: 'Equipment',
                2: 'Interests',
                3: 'Found BBS',
                4: 'Runs BBS?',
            }
            if not answers:
                await self.send_line(b"  (no finger file on record)\r\n")
            else:
                await self.send(b"\r\n")
                for qnum in sorted(answers):
                    label  = q_names.get(qnum, f'Q{qnum}')
                    answer = answers[qnum] or '(no answer)'
                    # rec 150: "%d.  %s"
                    await self.send(
                        b"  " + bb(150, {0: qnum + 1, 1: f"{label}: {answer}"})
                    )

    async def _cmd_ef(self) -> None:
        """
        EF — Edit Finger file.  Lets user update their public Q&A answers.

        systext/nq  — intro paragraph
        systext/nq0-nq4 — one question per file
        Saves via db.save_finger_answers().
        """
        from server import database as _db

        T     = Config.SESSION_TIMEOUT
        vars_ = SystextFile.make_variables(handle=self.handle, bbs_name=Config.BBS_NAME)

        # Intro
        await self.send(self.systext.render('nq', vars_))

        current = _db.get_finger_answers(self.user_id)
        new_answers: dict[int, str] = {}

        q_files = ['nq0', 'nq1', 'nq2', 'nq3', 'nq4']
        for i, qfile in enumerate(q_files):
            # Show the question text
            await self.send(self.systext.render(qfile, vars_))
            existing = current.get(i, '')
            if existing:
                await self.send_line(
                    f"  [Current: {existing[:60]}{'...' if len(existing)>60 else ''}]\r\n".encode()
                )
            await self.send(b"  Answer (ENTER to keep): ")
            raw = await self.readline_with_timeout(T)
            if raw is None:
                return
            answer = raw.strip()
            new_answers[i] = answer if answer else existing

        _db.save_finger_answers(self.user_id, new_answers)
        await self.send_line(b"\r\n  Finger file updated.\r\n")

    async def _cmd_find(self, pattern_arg: str = '') -> None:
        """
        FIND [pattern] — public user search.

        Shows handle, real name, and last-call date.
        Does not expose AG, email, or notes (those are sysop-only via UL).
        """
        from server import database as _db
        from datetime import datetime as _dt

        pattern = pattern_arg.strip()
        if not pattern:
            await self.send(b"\r\n  Search handles (blank=all): ")
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None:
                return
            pattern = raw.strip()

        users = _db.get_all_users(pattern)
        if not users:
            await self.send_line(
                f"\r\n  No users found{' matching ' + repr(pattern) if pattern else ''}.\r\n".encode()
            )
            return

        await self.send(b"\r\n")
        await self.send(b"  \x1b[1;32m" + b"\xc4" * 54 + b"\x1b[0m\r\n")
        await self.send(
            b"  \x1b[1;32m  Handle               Real Name            Last Call\x1b[0m\r\n"
        )
        await self.send(b"  \x1b[1;32m" + b"\xc4" * 54 + b"\x1b[0m\r\n")

        for u in users:
            handle    = (u['handle'] or '')[:20].ljust(20)
            real_name = (u['real_name'] or '')[:20].ljust(20)
            try:
                lc = _dt.fromisoformat(u['last_call']).strftime('%d-%b-%y') if u['last_call'] else 'Never'
            except Exception:
                lc = 'Never'
            await self.send(
                f"  {handle}  {real_name}  {lc}\r\n".encode()
            )

        await self.send(b"  \x1b[1;32m" + b"\xc4" * 54 + b"\x1b[0m\r\n")
        await self.send(
            f"  {len(users)} user(s){' (filtered)' if pattern else ''}\r\n\r\n".encode()
        )

    async def _cmd_ep(self) -> None:
        """
        EP — Edit Personal information.

        Shows the authentic CNet personal-info screen (bbstext 1094-1112)
        and lets the user change editable fields one at a time.

        Fields we support:
          2) Real name
          7) Voice phone number
          9) Date of birth
         10) Gender
         12) Password

        (Address, zip, country, data#, organisation are present in the CNet
        display but not stored in our schema — shown as blanks for now.)
        """
        T = Config.SESSION_TIMEOUT

        while True:
            # Reload user from DB each loop so we always show fresh data
            user = db.get_user_by_id(self.user_id)
            if not user:
                await self.send(self.bbstext.render(1108))   # "Unable to find your account!"
                return

            # rec 1094: "\r\nPersonal information (*=Private):\r\n\r\n"
            await self.send(self.bbstext.render(1094))
            # rec 1095: " 1) Handle  :  %s"
            await self.send(self.bbstext.render(1095, {0: user['handle']}) + b"\r\n")
            # rec 1096: " 2) Name    : %c%s"   (%c is privacy flag — space = public)
            await self.send(self.bbstext.render(1096, {0: ' ', 1: user['real_name'] or ''}) + b"\r\n")
            # rec 1101: " 7) Voice#  : %c%s"
            await self.send(self.bbstext.render(1101, {0: ' ', 1: user['phone'] or ''}) + b"\r\n")
            # rec 1110: " 8) Data#   : %c%s"  (we don't store data#; show blank)
            await self.send(self.bbstext.render(1110, {0: ' ', 1: ''}) + b"\r\n")
            # rec 1103: " 9) BirthDay: %c%s"
            await self.send(self.bbstext.render(1103, {0: ' ', 1: user['dob'] or ''}) + b"\r\n")
            # rec 1104: "10) Gender  :  %s"
            gender_display = {'M': 'Male', 'F': 'Female'}.get(user['gender'] or '', user['gender'] or '')
            await self.send(self.bbstext.render(1104, {0: gender_display}) + b"\r\n")
            # rec 1106: "12) Password: *%s"
            await self.send(self.bbstext.render(1106, {0: '(hidden)'}) + b"\r\n")

            # rec 1107: "\r\nEnter the # of the item to change, or press ENTER to continue.\r\n: "
            await self.send(self.bbstext.render(1107))
            raw = await self.readline_with_timeout(T)
            if raw is None:
                return
            choice = raw.strip()

            if not choice:
                return   # ENTER = done

            if choice == '2':
                await self.send(b"  New real name: ")
                val = await self.readline_with_timeout(T)
                if val is not None:
                    db.update_user_profile(self.user_id, real_name=val.strip()[:60],
                                           phone=user['phone'] or '',
                                           dob=user['dob'] or '',
                                           gender=user['gender'] or '')

            elif choice == '7':
                await self.send(b"  New voice phone: ")
                val = await self.readline_with_timeout(T)
                if val is not None:
                    db.update_user_profile(self.user_id, phone=val.strip()[:30],
                                           dob=user['dob'] or '',
                                           gender=user['gender'] or '',
                                           real_name=user['real_name'] or '')

            elif choice == '9':
                await self.send(b"  New date of birth (YYYY-MM-DD): ")
                val = await self.readline_with_timeout(T)
                if val is not None:
                    db.update_user_profile(self.user_id, dob=val.strip()[:10],
                                           phone=user['phone'] or '',
                                           gender=user['gender'] or '',
                                           real_name=user['real_name'] or '')

            elif choice == '10':
                await self.send(b"  Gender (M/F): ")
                val = await self.readline_with_timeout(T)
                if val is not None:
                    db.update_user_profile(self.user_id, gender=val.strip()[:1],
                                           phone=user['phone'] or '',
                                           dob=user['dob'] or '',
                                           real_name=user['real_name'] or '')

            elif choice == '12':
                await self.send(b"  New password: ")
                raw_pw1 = await self.readline_with_timeout(T)
                if raw_pw1 is None:
                    continue
                await self.send(b"  Confirm password: ")
                raw_pw2 = await self.readline_with_timeout(T)
                if raw_pw2 is None:
                    continue
                if raw_pw1.strip() != raw_pw2.strip():
                    await self.send(self.bbstext.render(312))   # "Passwords do not match."
                elif len(raw_pw1.strip()) < 4:
                    await self.send_line(b"\r\n  Password must be at least 4 characters.\r\n")
                else:
                    db.change_password(self.user_id, raw_pw1.strip())
                    await self.send_line(b"\r\n  Password updated.\r\n")

            else:
                await self.send_line(
                    b"\r\n  Valid choices: 2 (name)  7 (phone)  9 (birthday)"
                    b"  10 (gender)  12 (password)\r\n"
                )

    # ─────────────────────────────────────────────────────────────────────────
    # News helpers
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # News helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _news_list_line(self, idx: int, item) -> bytes:
        """Format a single news list line matching CNet style:
           ' 3  Some bulletin title'
        """
        return f"{idx:>2}  {item['title']}\r\n".encode()

    async def _news_print_item(self, item) -> None:
        """
        Display a news item.  Reads content from file (falls back to DB body).
        Uses bbstext rec 664 (title line) and rec 665 (date/byline).
        """
        from datetime import datetime as _dt
        try:
            dt = _dt.fromisoformat(item['posted_at'])
            date_str = dt.strftime('%d-%b-%y %H:%M')
        except (ValueError, TypeError):
            date_str = str(item['posted_at'])[:16]

        # rec 664: "\r\nNews Bulletin: %.50s"
        await self.send(self.bbstext.render(664, {0: item['title']}))
        # rec 665: "\r\n(%.50s)\r\n"  — date + poster
        byline = f"{date_str}  {item['posted_by_handle'] or ''}"
        await self.send(self.bbstext.render(665, {0: byline}))
        # rec 668: "   Date     : %s"  (post date if set)
        post_date = item['post_date'] or ''
        if post_date:
            await self.send(self.bbstext.render(668, {0: post_date}))
            await self.send(b"\r\n")
        await self.send(b"\r\n")

        body = news_mod.read_content(item).strip()
        if body:
            for line in body.splitlines():
                await self.send_line(line.encode('latin-1', errors='replace'))
        await self.send(b"\r\n")

    async def _news_show_list(self, items: list) -> None:
        """Print the numbered news list — matches CNet screenshot format."""
        await self.send(b"\r\n")
        await self.send(b"\x1b[1;36m## Description\x1b[0m\r\n")
        await self.send(b"\x1b[0;36m== " + b"=" * 57 + b"\x1b[0m\r\n")
        for idx, item in enumerate(items, start=1):
            await self.send(self._news_list_line(idx, item))
        await self.send(b"\r\n")

    async def _show_new_news(self) -> None:
        """
        Called at logon — auto-display items newer than last_news_read.
        Uses bbstext rec 674 "No new news." if nothing new.
        Silently swallows errors so a missing column never breaks login.
        """
        try:
            is_sysop  = self.access_group >= 31
            last_read = news_mod.get_last_news_read(self.user_id)
            new_items = news_mod.get_new_since(last_read, sysop=is_sysop)
            if not new_items:
                return

            entry_text = self.systext.render('sys.entry')
            if entry_text:
                await self.send(entry_text)

            count = len(new_items)
            await self.send(
                f"\r\n\x1b[1;33m{count} new news item"
                f"{'s' if count != 1 else ''} since your last call:"
                f"\x1b[0m\r\n".encode()
            )
            await self._news_show_list(new_items)
            news_mod.update_last_news_read(self.user_id)
        except Exception:
            pass  # Never let news errors break the login flow

    # ─────────────────────────────────────────────────────────────────────────
    # News commands
    # ─────────────────────────────────────────────────────────────────────────

    async def _cmd_news(self) -> None:
        """
        N — Interactive News area.

        Commands available at the News> prompt:
          #        — read item #
          S/SCAN   — re-list items
          Q/QUIT   — exit
          ?        — help
          (sysop)  P / POST    — post a new item
          (sysop)  AT#         — open VDE attributes for item #
          (sysop)  ED#         — edit content of item #
          (sysop)  K#          — kill (remove) item #
        """
        entry_text = self.systext.render('sys.entry')
        if entry_text:
            await self.send(entry_text)

        is_sysop = self.access_group >= 31
        items    = news_mod.get_all_items(sysop=is_sysop)

        if not items:
            # bbstext rec 663: "\r\nNo news.\r\n"
            await self.send(self.bbstext.render(663))
            news_mod.update_last_news_read(self.user_id)
            return

        await self._news_show_list(items)

        while True:
            # bbstext rec 666: "\r\nEnter item#, Scan, Quit, ?=Menu\r\n"
            await self.send(self.bbstext.render(666))
            if is_sysop:
                await self.send(
                    b"  \x1b[0;33m(Sysop: Post, AT#=Attribs, "
                    b"ED#=Edit, K#=Kill)\x1b[0m\r\n"
                )

            prompt = f"\x1b[1;36m({len(items)}) BBS System News> \x1b[0m"
            await self.send(prompt.encode())

            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None:
                break
            cmd = raw.strip().upper()

            if not cmd or cmd in ('Q', 'QUIT'):
                break

            elif cmd in ('S', 'SCAN'):
                items = news_mod.get_all_items(sysop=is_sysop)
                await self._news_show_list(items)

            elif cmd == '?':
                await self.send(
                    b"\r\n  Enter a number to read that item.\r\n"
                    b"  S = Scan (re-list)   Q = Quit\r\n"
                )
                if is_sysop:
                    await self.send(
                        b"  P = Post new item\r\n"
                        b"  AT# = Edit item attributes (VDE)\r\n"
                        b"  ED# = Edit item content\r\n"
                        b"  K#  = Kill (remove) item\r\n"
                    )
                await self.send(b"\r\n")

            elif cmd in ('P', 'POST') and is_sysop:
                await self._cmd_postnews()
                items = news_mod.get_all_items(sysop=True)
                await self._news_show_list(items)

            elif cmd.startswith('AT') and cmd[2:].isdigit() and is_sysop:
                idx = int(cmd[2:])
                if 1 <= idx <= len(items):
                    await VDESession(self).cmd_news_at(idx, items)
                    items = news_mod.get_all_items(sysop=True)
                    await self._news_show_list(items)
                else:
                    # bbstext rec 1786: "\r\nItem number out of range.\r\n"
                    await self.send(self.bbstext.render(1786))

            elif cmd.startswith('ED') and cmd[2:].isdigit() and is_sysop:
                idx = int(cmd[2:])
                if 1 <= idx <= len(items):
                    await VDESession(self).cmd_news_ed(idx, items)
                    items = news_mod.get_all_items(sysop=True)
                else:
                    await self.send(self.bbstext.render(1786))

            elif cmd.startswith('K') and cmd[1:].isdigit() and is_sysop:
                idx = int(cmd[1:])
                if 1 <= idx <= len(items):
                    item_id = items[idx - 1]['id']
                    if news_mod.kill_item(item_id):
                        await self.send(
                            f"  Item {idx} killed.\r\n".encode())
                        items = news_mod.get_all_items(sysop=True)
                        await self._news_show_list(items)
                    else:
                        await self.send(b"  Could not kill item.\r\n")
                else:
                    await self.send(self.bbstext.render(1786))

            elif cmd.isdigit():
                idx = int(cmd)
                if 1 <= idx <= len(items):
                    await self._news_print_item(items[idx - 1])
                else:
                    await self.send(self.bbstext.render(1786))

            else:
                await self.send(b"  ?\r\n")

        news_mod.update_last_news_read(self.user_id)

    async def _cmd_postnews(self) -> None:
        """
        PN / POSTNEWS (sysop-only) — write and post a new news item.
        Prompts for a description, then opens the line editor for the body.
        Body is written to data/news/<timestamp>.news on disk.
        """
        from server.editor import LineEditor

        await self.send_line(b"\r\n  -- Post News Item --\r\n")
        await self.send(b"  Description: ")
        raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
        if raw is None or not raw.strip():
            await self.send_line(b"  [Cancelled]\r\n")
            return

        title  = raw.strip()[:70]
        editor = LineEditor(self, subject=title)
        body   = await editor.run()
        if body is None:
            await self.send_line(b"  [Item discarded]\r\n")
            return

        item_id = news_mod.post_item(
            title            = title,
            body             = body,
            posted_by_id     = self.user_id,
            posted_by_handle = self.handle,
        )
        await self.send_line(
            f"\r\n  News item #{item_id} posted.\r\n".encode())

    async def _cmd_killnews(self, command: str) -> None:
        """
        KN / KILLNEWS # (sysop-only) — remove a news item.
        With a number kills directly; without, lists then prompts.
        """
        parts   = command.split()
        num_str = parts[1] if len(parts) > 1 else ''

        if not num_str:
            items = news_mod.get_all_items(sysop=True)
            if not items:
                await self.send(self.bbstext.render(663))
                return
            await self.send_line(b"\r\n  Active news items:\r\n")
            for idx, item in enumerate(items, start=1):
                await self.send(self._news_list_line(idx, item))
            await self.send(b"\r\n  Kill item #: ")
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None or not raw.strip():
                await self.send_line(b"  [Cancelled]\r\n")
                return
            num_str = raw.strip()
            if num_str.isdigit():
                i = int(num_str)
                if 1 <= i <= len(items):
                    num_str = str(items[i - 1]['id'])

        if not num_str.isdigit():
            await self.send_line(b"  Usage: KN #\r\n")
            return

        item_id = int(num_str)
        if news_mod.kill_item(item_id):
            await self.send_line(
                f"\r\n  News item #{item_id} killed.\r\n".encode())
        else:
            await self.send_line(
                f"\r\n  Item #{item_id} not found or already inactive.\r\n"
                .encode()
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Mail
    # ─────────────────────────────────────────────────────────────────────────

    async def _check_new_mail(self) -> None:
        """
        After login, notify user of waiting mail.

        rec 225 — "There are %d new mail item(s) in %s.  Read now [N/y]?"
        rec 226 — "There are %d old mail item(s) in %s.  Use MR to read."
        If user says Y to rec 225, drop straight into _cmd_mail_read().
        """
        if not self.user_id:
            return
        unread = mail_mod.get_unread_count(self.user_id)
        inbox  = mail_mod.get_inbox(self.user_id)
        total  = len(inbox)

        if unread > 0:
            # rec 225: unread mail prompt
            await self.send(self.bbstext.render(225, {0: unread, 1: 'INBOX'}))
            ans = (await self.readline_with_timeout(Config.SESSION_TIMEOUT) or '').strip().upper()
            if ans in ('Y', 'YES', ''):
                await self._cmd_mail_read()
        elif total > 0:
            # rec 226: old (read) mail reminder
            await self.send(self.bbstext.render(226, {0: total, 1: 'INBOX'}))

    async def _cmd_mail_area(self) -> None:
        """
        MAIL command — enter the mail area.
        Shows the mail area prompt and dispatches sub-commands.

        Commands:
          R / READ  — read inbox (same as MR)
          W / WRITE / SEND — compose new mail
          S / SCAN  — list mail headers without reading
          Q / QUIT  — back to main prompt
          ?         — show systext/mail help
        """
        while True:
            # Show unread count as context
            unread = mail_mod.get_unread_count(self.user_id)
            total  = len(mail_mod.get_inbox(self.user_id))

            if unread > 0:
                await self.send(
                    self.bbstext.render(225, {0: unread, 1: 'INBOX'})
                )
                # Suppress the [N/y]? — we'll show our own prompt right after
                # by just not reading input here; the prompt text is cosmetic.
                # Fall through to the command prompt.
                await self.send(b"\r\n")
            elif total > 0:
                await self.send(self.bbstext.render(226, {0: total, 1: 'INBOX'}))

            await self.send(b"\r\nMail: Read, Write, Scan, Quit, ?=Help: ")
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None:
                return
            cmd = raw.strip().upper()

            if not cmd or cmd in ('Q', 'QUIT'):
                return

            elif cmd == '?':
                await self.send(self.systext.render('mail'))

            elif cmd in ('R', 'READ', 'MR'):
                await self._cmd_mail_read()

            elif cmd in ('W', 'WRITE', 'SEND'):
                await self._cmd_mail_write()

            elif cmd in ('S', 'SCAN'):
                inbox = mail_mod.get_inbox(self.user_id)
                if not inbox:
                    await self.send(self.bbstext.render(200))  # "No mail in inbox."
                else:
                    await self._print_mail_list(inbox)

            else:
                await self.send_line(
                    b"\r\n  Commands: Read  Write  Scan  Quit  ?\r\n"
                )

    async def _cmd_mail_read(self) -> None:
        """
        MR command — read inbox sequentially.

        Starts at the first unread message (falling back to message #1).
        After each message shows rec 218 prompt:
          ENTER / P = next message
          A         = again (re-read current)
          R / REPLY = reply to sender
          K / KILL  = delete this message
          S / SCAN  = show the full list, then return to reading
          F / FWD   = (future: forward)
          Q / QUIT  = leave mail read
          ?         = systext/mail-read help
        """
        inbox = mail_mod.get_inbox(self.user_id)
        if not inbox:
            await self.send(self.bbstext.render(200))   # "No mail in inbox."
            return

        # Start at first unread, or 0 if all read
        start_idx = next(
            (i for i, m in enumerate(inbox) if m['read_at'] is None), 0
        )
        idx = start_idx

        while 0 <= idx < len(inbox):
            msg = inbox[idx]
            await self._print_mail_message(msg, idx + 1, len(inbox))
            mail_mod.mark_read(msg['id'])

            # rec 218: "Mail-Read/INBOX: ?,Quit,Scan,Reply,Rescan [ENTER=next]:"
            await self.send(self.bbstext.render(218, {0: 'INBOX'}))
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None:
                return
            cmd = raw.strip().upper()

            if cmd in ('', 'P', 'NEXT'):
                idx += 1

            elif cmd in ('A', 'AGAIN'):
                pass   # re-read, don't advance

            elif cmd in ('R', 'REPLY'):
                await self._cmd_mail_reply(msg)
                inbox = mail_mod.get_inbox(self.user_id)   # reload in case count changed
                idx  += 1

            elif cmd in ('K', 'KILL', 'D', 'DELETE'):
                if mail_mod.kill_mail(msg['id'], self.user_id):
                    await self.send_line(b"\r\n  Message deleted.\r\n")
                inbox = mail_mod.get_inbox(self.user_id)
                # Don't advance idx — next message is now at same slot

            elif cmd in ('S', 'SCAN'):
                await self._print_mail_list(inbox)

            elif cmd == '?':
                await self.send(self.systext.render('mail-read'))

            elif cmd in ('Q', 'QUIT'):
                return

            else:
                await self.send(self.bbstext.render(218, {0: 'INBOX'}))

        await self.send_line(b"\r\n  [End of inbox]\r\n")

    async def _cmd_mail_write(self, to_handle: str = '') -> None:
        """
        Compose and send a mail message.

        Prompts for To:, Subject:, then opens the LineEditor for body.
        Validates that the recipient exists in the user table.
        """
        await self.send_line(b"\r\n  -- Compose Mail --\r\n")

        # To:
        if not to_handle:
            await self.send(b"     To: ")
            raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
            if raw is None or not raw.strip():
                await self.send_line(b"  [Cancelled]\r\n")
                return
            to_handle = raw.strip()

        # Validate recipient
        recipient = db.get_user_by_handle(to_handle)
        if not recipient:
            await self.send(self.bbstext.render(204, {0: to_handle}))  # "Invalid address or user %s"
            return

        # Subject:
        await self.send(b"Subject: ")
        raw = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
        if raw is None or not raw.strip():
            await self.send_line(b"  [Cancelled]\r\n")
            return
        subject = raw.strip()[:75]

        # Body
        from server.editor import LineEditor
        editor = LineEditor(self, subject=subject)
        body   = await editor.run()
        if body is None:
            await self.send_line(b"  [Message discarded]\r\n")
            return

        mail_id = mail_mod.send_mail(
            from_id     = self.user_id,
            from_handle = self.handle,
            to_id       = recipient['id'],
            to_handle   = recipient['handle'],
            subject     = subject,
            body        = body,
        )
        await self.send_line(
            f"\r\n  Mail sent to {recipient['handle']} (#{mail_id}).\r\n".encode()
        )

    async def _cmd_mail_reply(self, original_msg) -> None:
        """Reply to a mail message — pre-fills Re: subject, offers to quote."""
        orig_subj = original_msg['subject'] or ''
        # rec 709: "Re: %-.75s"  or rec 708: "Re:" if subject is already "Re: ..."
        if orig_subj.startswith('Re: '):
            subject = orig_subj[:75]
        else:
            subject = f"Re: {orig_subj}"[:75]

        await self.send_line(b"\r\n  -- Reply --\r\n")
        await self.send(b"  Quote original message? (Y/N): ")
        raw    = await self.readline_with_timeout(Config.SESSION_TIMEOUT)
        quote_lines = None
        if raw and raw.strip().upper() in ('Y', 'YES'):
            body_lines  = (original_msg['body'] or '').splitlines()
            quote_lines = body_lines[:6]

        from server.editor import LineEditor
        editor = LineEditor(self, subject=subject, quote_lines=quote_lines)
        body   = await editor.run()
        if body is None:
            await self.send_line(b"  [Reply discarded]\r\n")
            return

        # Look up original sender
        recipient = db.get_user_by_handle(original_msg['from_handle'])
        if not recipient:
            await self.send_line(
                f"  Error: original sender '{original_msg['from_handle']}' not found.\r\n".encode()
            )
            return

        mail_id = mail_mod.send_mail(
            from_id     = self.user_id,
            from_handle = self.handle,
            to_id       = recipient['id'],
            to_handle   = recipient['handle'],
            subject     = subject,
            body        = body,
            reply_to_id = original_msg['id'],
        )
        await self.send_line(
            f"\r\n  Reply sent to {recipient['handle']} (#{mail_id}).\r\n".encode()
        )

    # ── Mail display helpers ──────────────────────────────────────────────────

    async def _print_mail_message(self, msg, idx: int, total: int) -> None:
        """Print a single mail message with CNet-style header."""
        from datetime import datetime as _dt
        try:
            dt = _dt.fromisoformat(msg['sent_at'])
            date_str = dt.strftime('%d-%b-%y %H:%M')
        except (ValueError, TypeError):
            date_str = str(msg['sent_at'])[:16]

        await self.send(b"\r\n  \x1b[1;32m" + b"\xc4" * 68 + b"\x1b[0m\r\n")
        # rec 253: "    Item: %d (of %d)"
        await self.send(b"  " + self.bbstext.render(253, {0: idx, 1: total}) + b"\r\n")
        # rec 246: "   From: %s"
        await self.send(b"  " + self.bbstext.render(246, {0: msg['from_handle']}) + b"\r\n")
        await self.send(f"        To: {msg['to_handle']}\r\n".encode())
        # rec 249: "    Date: %s"
        await self.send(b"  " + self.bbstext.render(249, {0: date_str}) + b"\r\n")
        # rec 250: "Subject: %s"
        await self.send(b"  " + self.bbstext.render(250, {0: msg['subject']}) + b"\r\n")
        await self.send(b"  \x1b[1;32m" + b"\xc4" * 68 + b"\x1b[0m\r\n\r\n")

        body = (msg['body'] or '').strip()
        for line in body.splitlines():
            await self.send_line(f"  {line}".encode())
        await self.send(b"\r\n")

    async def _print_mail_list(self, inbox: list) -> None:
        """Print mail inbox as a numbered list (Scan view)."""
        await self.send(b"\r\n")
        await self.send(
            b"\x1b[1;32m  #   St  From                 Subject                       Date\x1b[0m\r\n"
        )
        await self.send(b"  " + b"\xc4" * 68 + b"\r\n")
        for i, msg in enumerate(inbox, 1):
            status   = 'N ' if msg['read_at'] is None else '  '
            from_h   = (msg['from_handle'] or '?')[:20].ljust(20)
            subject  = (msg['subject'] or '(no subject)')[:29].ljust(30)
            try:
                from datetime import datetime as _dt
                dt   = _dt.fromisoformat(msg['sent_at'])
                date = dt.strftime('%d-%b-%y')
            except (ValueError, TypeError):
                date = '?'
            color = b'\x1b[1;33m' if msg['read_at'] is None else b'\x1b[0m'
            row   = f"  {i:<3} {status}  {from_h} {subject} {date}\r\n"
            await self.send(color + row.encode() + b'\x1b[0m')
        await self.send(b"  " + b"\xc4" * 68 + b"\r\n")
        await self.send(
            f"  {len(inbox)} message(s)   N = new\r\n".encode()
        )

    async def _cmd_nscan(self) -> None:
        """
        NS — scan all boards for new messages since last visit.

        Display flow:
          1. rec 809  "New scan" status header
          2. rec 812/813  summary table header
          3. Per accessible board: board number, name, new-post/response counts
          4. rec 829  "N subboard(s) report new messages"  or  rec 828  "no new"
          5. rec 830  "Browse#-# Cancel#-# List#-# Read#-# Yank [?=menu]:"
          6. Dispatch: R=read  L=list  B=browse  C=cancel(reset dates)  ?=help  Q=quit

        Range syntax (optional on R/B/C): "R 2" or "R 1-4"
        No range = all boards in the new-boards list.
        """
        from server import msgbase
        from server.boards import BoardArea

        T  = Config.SESSION_TIMEOUT
        bb = self.bbstext.render

        # ── rec 809: "New scan" status ────────────────────────────────────
        await self.send(bb(809) + b"\r\n")

        boards = msgbase.get_subboards_for_user(self.access_group)

        # Build scan data — one entry per board with new activity
        _EPOCH = "1970-01-01T00:00:00"

        scan_results = []   # list of dicts: board, last_visit, new_posts, new_resps
        for board in boards:
            last_visit  = msgbase.get_last_visit(self.user_id, board['id']) or _EPOCH
            new_p, new_r = msgbase.get_new_counts(board['id'], last_visit)
            scan_results.append({
                'board':      board,
                'last_visit': last_visit,
                'new_posts':  new_p,
                'new_resps':  new_r,
                'has_new':    (new_p + new_r) > 0,
            })

        new_boards = [s for s in scan_results if s['has_new']]

        async def show_table():
            # rec 812: "### Subboard    New posts  New resps  To you"
            await self.send(bb(812))
            # rec 813: separator
            await self.send(bb(813))

            if not new_boards:
                await self.send_line(b"      (none)\r\n")
                return

            for i, s in enumerate(new_boards, 1):
                name     = (s['board']['name'] or '')[:35]
                new_p    = s['new_posts']
                new_r    = s['new_resps']
                # rec 814 format: "%3d %-37s" with post/resp counts appended
                row = f"{i:3d} {name:<37}"
                # posts: rec 818 (singular) or rec 819 (plural)
                if new_p == 1:
                    post_str = bb(818, {0: new_p}).decode('latin-1', 'replace').strip()
                else:
                    post_str = bb(819, {0: new_p}).decode('latin-1', 'replace').strip()
                # responses: rec 820/821
                if new_r == 1:
                    resp_str = bb(820, {0: new_r}).decode('latin-1', 'replace').strip()
                else:
                    resp_str = bb(821, {0: new_r}).decode('latin-1', 'replace').strip()

                import re as _re
                post_clean = _re.sub(r'\x1b\[[^m]*m', '', post_str).strip()
                resp_clean = _re.sub(r'\x1b\[[^m]*m', '', resp_str).strip()

                await self.send_line(
                    f"\x1b[1;37m{row}\x1b[0m "
                    f"\x1b[1;33m{post_clean:<12}\x1b[0m "
                    f"\x1b[1;33m{resp_clean}\x1b[0m\r\n"
                    .encode()
                )

        await show_table()

        if not new_boards:
            # rec 828: "There have been no new posts or responses."
            await self.send(bb(828))
            return

        # rec 829: "%d subboard(s) report new messages since you last visited them."
        await self.send(bb(829, {0: len(new_boards)}))

        def parse_range(arg: str) -> list[int]:
            """Parse "2" or "1-4" into a list of 1-based indices."""
            arg = arg.strip()
            if not arg:
                return list(range(1, len(new_boards) + 1))
            if '-' in arg:
                parts = arg.split('-', 1)
                try:
                    lo = max(1, int(parts[0]))
                    hi = min(len(new_boards), int(parts[1]))
                    return list(range(lo, hi + 1))
                except ValueError:
                    return list(range(1, len(new_boards) + 1))
            try:
                n = int(arg)
                if 1 <= n <= len(new_boards):
                    return [n]
            except ValueError:
                pass
            return list(range(1, len(new_boards) + 1))

        # ── Secondary prompt loop ─────────────────────────────────────────
        while True:
            # rec 830: "Browse#-# Cancel#-# List#-# Read#-# Yank [?=menu]:"
            await self.send(bb(830))
            raw = await self.readline_with_timeout(T)
            if raw is None:
                return

            parts    = raw.strip().upper().split(None, 1)
            sub_cmd  = parts[0] if parts else ''
            sub_arg  = parts[1] if len(parts) > 1 else ''

            if not sub_cmd or sub_cmd in ('Q', 'QUIT', 'XIT'):
                return

            elif sub_cmd == '?':
                vars_ = SystextFile.make_variables(handle=self.handle)
                await self.send(self.systext.render('nscan', vars_))

            elif sub_cmd == 'L':
                await show_table()
                await self.send(bb(829, {0: len(new_boards)}))

            elif sub_cmd in ('R', 'READ'):
                indices = parse_range(sub_arg)
                area    = BoardArea(self)
                for idx in indices:
                    s = new_boards[idx - 1]
                    board = s['board']
                    if board['read_ag'] > self.access_group:
                        continue
                    name = (board['name'] or '').strip()
                    await self.send(
                        b"\r\n\x1b[1;37m*Subboard \x1b[36m("
                        + name.encode() + b")\x1b[0m\r\n"
                    )
                    msgbase.record_visit(self.user_id, board['id'])
                    threads = msgbase.get_thread_list_since(
                        board['id'], s['last_visit']
                    )
                    for t in threads:
                        if not await area._read_thread(t, board):
                            return   # user typed Q

            elif sub_cmd in ('B', 'BROWSE'):
                indices = parse_range(sub_arg)
                area    = BoardArea(self)
                for idx in indices:
                    s     = new_boards[idx - 1]
                    board = s['board']
                    if board['read_ag'] > self.access_group:
                        continue
                    await area._thread_list(board)

            elif sub_cmd in ('C', 'CANCEL'):
                indices = parse_range(sub_arg)
                for idx in indices:
                    s = new_boards[idx - 1]
                    msgbase.record_visit(self.user_id, s['board']['id'])
                n = len(indices)
                # rec 810: "New-dates reset for %d subboard(s)."
                await self.send(bb(810, {0: n}))
                return   # exit NS after cancel, same as CNet

            else:
                await self.send(bb(830))

    async def _cmd_who(self) -> None:
        """
        WHO — show who is online.

        Display format uses authentic CNet bbstext records:
          rec 898 — column header row
          rec 899 — separator line
          rec 900 — "%2d %1.1s"           (port, status char)
          rec 901 — " %-20s %6s %3d"      (handle, logon-time, speed)
          rec 907 — " %-24.24s %-16.16s"  (from, where/status)
          rec 902 — "(no one)"            (empty node)
        """
        from datetime import datetime as _dt

        sessions = db.get_active_sessions(include_hidden=(self.access_group >= 31))

        # Filter to active sessions only (skip long-dead disconnected nodes)
        active = [s for s in sessions if s['status'] != 'disconnected']

        await self.send(self.bbstext.render(898))   # header
        await self.send(self.bbstext.render(899))   # separator

        if not active:
            await self.send(self.bbstext.render(902))   # "(no one)"
            await self.send(b"\r\n")
            return

        for s in active:
            handle = s['handle'] or '(connecting)'

            # Status char: space=online, L=logging in, I=idle/waiting
            status_map = {
                'online':      ' ',
                'logging_in':  'L',
                'waiting':     'I',
            }
            status_char = status_map.get(s['status'], '?')

            # Logon time formatted as HH:MM
            try:
                connected = _dt.fromisoformat(s['connected_at'])
                logon_str = connected.strftime('%H:%M')
            except (ValueError, TypeError):
                logon_str = '--:--'

            speed = s['speed'] or 0
            location = (s['location'] or '(unknown)')[:24]
            where = s['status'][:16]

            # rec 900: "%2d %1.1s"
            await self.send(self.bbstext.render(900, {0: s['node_id'], 1: status_char}))
            # rec 901: " %-20s %6s %3d"
            await self.send(self.bbstext.render(901, {0: handle, 1: logon_str, 2: speed}))
            # rec 907: " %-24.24s %-16.16s\n"
            await self.send(self.bbstext.render(907, {0: location, 1: where}))

    async def _cmd_newboard(self) -> None:
        """SysOp command: create a new message subboard interactively."""
        from server import msgbase
        await self.send_line(b"\r\n  -- Create New Board --\r\n")

        await self.send(b"  Board name: ")
        raw = await self.readline_with_timeout()
        if not raw or not raw.strip():
            await self.send_line(b"  [Cancelled]\r\n")
            return
        name = raw.strip()[:40]

        await self.send(b"  Description (optional): ")
        raw = await self.readline_with_timeout()
        description = raw.strip()[:80] if raw else ""

        await self.send(b"  Minimum AG to READ  (0-31, Enter=0): ")
        raw = await self.readline_with_timeout()
        try:
            read_ag = max(0, min(31, int(raw.strip()))) if raw and raw.strip() else 0
        except ValueError:
            read_ag = 0

        await self.send(b"  Minimum AG to WRITE (0-31, Enter=5): ")
        raw = await self.readline_with_timeout()
        try:
            write_ag = max(0, min(31, int(raw.strip()))) if raw and raw.strip() else 5
        except ValueError:
            write_ag = 5

        # Confirm
        await self.send_line(
            f"\r\n  Name        : {name}\r\n"
            f"  Description : {description}\r\n"
            f"  Read AG     : {read_ag}\r\n"
            f"  Write AG    : {write_ag}\r\n"
        )
        await self.send(b"  Create this board? (Y/N): ")
        confirm = await self.readline_with_timeout()
        if not confirm or confirm.strip().upper() not in ("Y", "YES"):
            await self.send_line(b"  [Cancelled]\r\n")
            return

        board_id = msgbase.create_subboard(name, description, read_ag, write_ag,
                                           self.handle)
        await self.send_line(
            f"  Board '{name}' created (#{board_id}).\r\n".encode()
        )

    # -----------------------------------------------------------------------
    # I/O helpers
    # -----------------------------------------------------------------------

    async def send(self, data: bytes | str) -> None:
        """
        Write bytes (or a string) to the client and flush the buffer.

        `await writer.drain()` is the asyncio way of saying "wait until all
        buffered data has actually been sent over the network before continuing".
        Without drain(), we could queue up megabytes in RAM and overwhelm a
        slow connection.
        """
        if isinstance(data, str):
            data = data.encode('latin-1', errors='replace')
        self.writer.write(data)
        await self.writer.drain()

    async def send_line(self, text: str | bytes = '') -> None:
        """Send text followed by \\r\\n."""
        if isinstance(text, str):
            text = text.encode('latin-1', errors='replace')
        await self.send(text + b'\r\n' if not text.endswith(b'\r\n') else text)

    async def readline_with_timeout(self, timeout: float = None) -> str | None:
        """
        Read a line of input from the client with a timeout.

        Returns the decoded line (str), or None on timeout or connection close.

        asyncio.wait_for() wraps any awaitable with a deadline.  If the user
        doesn't press Enter within `timeout` seconds, TimeoutError is raised —
        we catch it and return None so callers can handle it gracefully rather
        than propagating an exception.
        """
        if timeout is None:
            timeout = Config.SESSION_TIMEOUT
        try:
            raw = await asyncio.wait_for(
                self.reader.readline(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None
        if not raw:
            return None
        return raw.decode('latin-1', errors='replace').rstrip('\r\n')

    # -- Echo control (Telnet IAC sequences) ---------------------------------
    # These tell the client terminal to stop echoing keys the user types,
    # so passwords appear blank.  The TelnetProtocol layer handles the full
    # negotiation; here we just send the relevant IAC sequences directly.

    async def send_noecho_on(self) -> None:
        """Stop echoing — used for password fields.
        The client handed us echo control at connection time (IAC WILL ECHO
        in the initial negotiation), so we just flip the flag on the reader."""
        self.reader.echo = False

    async def send_noecho_off(self) -> None:
        """Resume echoing after a password field."""
        self.reader.echo = True

