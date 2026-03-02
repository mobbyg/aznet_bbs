"""
server/vde.py — In-BBS Sysop Management Panel (VDE)

Full-screen ANSI form editor for user and board management, modelled on
CNet PRO's VDE (Visual Data Editor).

COMMANDS  (all require access_group >= 31)
──────────
  EA  [handle]   — Edit Account   (full-screen VDE form)
  EB  [#|name]   — Edit Board     (full-screen VDE form)
  KU  [handle]   — Kill User      (quick deactivate, no form)
  KB  [#|name]   — Kill Board     (quick deactivate)
  UL  [pattern]  — User List

The EA form includes sub-screens:
  • Password change
  • Privilege flags (all 43 CNet flags, bitmask in priv_flags column)
  • Credits/balances  [ghosted — future]
  • Limits/ratios     [ghosted — future]
  • Preferences/term  [ghosted — future]
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as _dt

log = logging.getLogger('anet.vde')

_SYSOP_AG = 31


class VDESession:
    """Sysop command dispatcher.  One instance per invocation."""

    def __init__(self, session) -> None:
        self._s = session

    async def _send(self, data: bytes) -> None:
        await self._s.send(data)

    async def _sendl(self, line: str) -> None:
        await self._s.send_line(line.encode())

    async def _prompt(self, text: str) -> str | None:
        await self._s.send(text.encode())
        raw = await self._s.readline_with_timeout()
        return raw.strip() if raw is not None else None

    def _hr(self, width: int = 58) -> bytes:
        return b'  \x1b[1;32m' + b'\xc4' * width + b'\x1b[0m\r\n'

    def _fmt_date(self, iso: str | None) -> str:
        if not iso:
            return 'Never'
        try:
            return _dt.fromisoformat(iso).strftime('%d-%b-%y %H:%M')
        except (ValueError, TypeError):
            return str(iso)[:16]

    # ─────────────────────────────────────────────────────────────────────────
    # EA — Edit Account  (full-screen VDE form)
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_ea(self, handle_arg: str = '') -> None:
        from server import database as db
        from server.vde_engine import VDEForm
        from server.vde_screens import (
            make_ea_profile_fields, make_ea_profile_data,
            make_ea_priv_fields,   make_ea_priv_data,   pack_priv_data,
            make_ea_credits_fields, make_ea_credits_data,
            make_ea_prefs_fields,   make_ea_prefs_data,  unpack_prefs_data,
        )

        handle = handle_arg.strip()
        if not handle:
            handle = await self._prompt('\r\n  Edit account (handle): ')
            if not handle:
                return

        user = db.get_user_by_handle(handle)
        if not user:
            await self._sendl(f"\r\n  User '{handle}' not found.\r\n")
            return

        # ── Password sub-screen ───────────────────────────────────────────────
        async def sub_password(session, data):
            pw1 = await self._prompt('\r\n  New password (blank=cancel): ')
            if not pw1 or not pw1.strip():
                return None
            pw2 = await self._prompt('  Confirm password: ')
            if pw2 is None:
                return None
            if pw1.strip() != pw2.strip():
                await self._sendl('  Passwords do not match.\r\n')
                return None
            if len(pw1.strip()) < 4:
                await self._sendl('  Password must be at least 4 characters.\r\n')
                return None
            db.change_password(user['id'], pw1.strip())
            await self._sendl('  Password updated.\r\n')
            return None

        # ── Credits/Balances sub-screen ───────────────────────────────────────
        async def sub_credits(session, data):
            fresh = db.get_user_by_id(user['id'])
            if not fresh:
                return None
            cfields = make_ea_credits_fields()
            cdata   = make_ea_credits_data(fresh)
            title   = f'Credits/Balances: {fresh["handle"]}'
            form    = VDEForm(session, title, cfields, cdata, num_cols=2)
            saved   = await form.run()
            if saved:
                db.update_user_vde(user['id'], saved)
            return None

        # ── Privilege flags sub-screen ────────────────────────────────────────
        async def sub_privs(session, data):
            fresh = db.get_user_by_id(user['id'])
            if not fresh:
                return None
            priv_fields = make_ea_priv_fields()      # ← no arg (was a bug)
            priv_data   = make_ea_priv_data(fresh)
            title       = f'Privilege Flags: {fresh["handle"]}  (AG {fresh["access_group"]})'
            form        = VDEForm(session, title, priv_fields, priv_data, num_cols=3)
            saved = await form.run()
            if saved is None:
                return None
            merged    = dict(priv_data)
            merged.update(saved)
            new_flags = pack_priv_data(merged)
            with db.get_connection() as conn:
                conn.execute('UPDATE users SET priv_flags = ? WHERE id = ?',
                             (new_flags, user['id']))
                conn.commit()
            return None

        # ── Preferences/Terminal sub-screen ───────────────────────────────────
        async def sub_prefs(session, data):
            fresh = db.get_user_by_id(user['id'])
            if not fresh:
                return None
            pfields = make_ea_prefs_fields()
            pdata   = make_ea_prefs_data(fresh)
            title   = f'Preferences/Terminal: {fresh["handle"]}'
            form    = VDEForm(session, title, pfields, pdata, num_cols=2)
            saved   = await form.run()
            if saved:
                db.update_user_vde(user['id'], unpack_prefs_data(saved))
            return None

        sub_fns = {
            'password': sub_password,
            'credits':  sub_credits,
            'privs':    sub_privs,
            'prefs':    sub_prefs,
        }

        while True:
            user = db.get_user_by_id(user['id'])
            if not user:
                await self._sendl('\r\n  User no longer exists.\r\n')
                break

            title = (f'Edit User: {user["handle"]}  '
                     f'AG:{user["access_group"]}  '
                     f'Calls:{user["call_count"] or 0}')
            fields = make_ea_profile_fields(user, sub_fns)
            data   = make_ea_profile_data(user)

            form  = VDEForm(self._s, title, fields, data, num_cols=2)
            saved = await form.run()

            if saved is None:
                break

            if saved.get('__kill__'):
                if db.soft_delete_user(user['id']):
                    await self._sendl(f"\r\n  Account '{user['handle']}' deactivated.\r\n")
                break

            if saved:
                db.update_user_vde(user['id'], saved)
                await self._sendl('\r\n  Changes saved.\r\n')
                await asyncio.sleep(0.4)
            else:
                break

    # ─────────────────────────────────────────────────────────────────────────
    # EB — Edit Board  (full-screen VDE form)
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_eb(self, id_arg: str = '') -> None:
        from server import msgbase
        from server.vde_engine import VDEForm
        from server.vde_screens import make_eb_main_fields, make_eb_main_data

        board = await self._resolve_board(id_arg)
        if not board:
            return

        while True:
            board = msgbase.get_subboard(board['id'])
            if not board:
                await self._sendl('\r\n  Board no longer exists.\r\n')
                break

            title  = f'Edit Board #{board["id"]}: {board["name"]}'
            fields = make_eb_main_fields({})
            data   = make_eb_main_data(board)

            form  = VDEForm(self._s, title, fields, data)
            saved = await form.run()

            if saved is None:
                break

            if saved:
                if 'name' in saved:
                    msgbase.update_subboard(board['id'], name=str(saved['name'])[:40])
                if 'description' in saved:
                    msgbase.update_subboard(board['id'], description=str(saved['description'])[:80])
                if 'read_ag' in saved:
                    try: msgbase.update_subboard(board['id'], read_ag=int(saved['read_ag']))
                    except (ValueError, TypeError): pass
                if 'write_ag' in saved:
                    try: msgbase.update_subboard(board['id'], write_ag=int(saved['write_ag']))
                    except (ValueError, TypeError): pass
                await self._sendl('\r\n  Changes saved.\r\n')
                await asyncio.sleep(0.4)
            else:
                break

    # ─────────────────────────────────────────────────────────────────────────
    # EG — Edit Access Group  (full-screen VDE form + sub-screens)
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_eg(self, ag_arg: str = '') -> None:
        from server import database as db
        from server.vde_engine import VDEForm
        from server.vde_screens import (
            make_eg_fields,        make_eg_data,
            make_eg_priv_fields,   make_eg_priv_data,   pack_eg_priv_data,
            make_eg_limits_fields, make_eg_limits_data,
        )
        ag_arg = ag_arg.strip()
        if not ag_arg:
            ag_arg = await self._prompt('\r\n  Edit access group (0-31): ')
            if ag_arg is None:
                return
        try:
            ag_id = int(ag_arg.strip())
            if not (0 <= ag_id <= 31):
                raise ValueError
        except ValueError:
            await self._sendl(f'\r\n  Invalid access group number.\r\n')
            return

        group = db.get_or_create_access_group(ag_id)

        # ── EG Privilege flags sub-screen ─────────────────────────────────────
        async def sub_privs(session, data):
            g = db.get_or_create_access_group(ag_id)
            pfields = make_eg_priv_fields()
            pdata   = make_eg_priv_data(g)
            title   = f'Privileges: AG {ag_id} ({g.get("title", "")})'
            form    = VDEForm(session, title, pfields, pdata, num_cols=3)
            saved = await form.run()
            if saved is None:
                return None
            merged = dict(pdata)
            merged.update(saved)
            new_privs = pack_eg_priv_data(merged)
            db.update_access_group(ag_id, ag_privs=new_privs)
            return None

        # ── EG Limits/Ratios sub-screen ───────────────────────────────────────
        async def sub_limits(session, data):
            g = db.get_or_create_access_group(ag_id)
            lfields = make_eg_limits_fields()
            ldata   = make_eg_limits_data(g)
            title   = f'Limits/Ratios: AG {ag_id} ({g.get("title", "")})'
            form    = VDEForm(session, title, lfields, ldata, num_cols=2)
            saved = await form.run()
            if saved:
                db.update_access_group(ag_id, ag_limits=saved)
            return None

        sub_fns = {
            'privs':  sub_privs,
            'limits': sub_limits,
        }

        while True:
            group = db.get_or_create_access_group(ag_id)
            title  = f'Edit Access Group {ag_id}: {group.get("title", "")}'
            fields = make_eg_fields(group, sub_fns)
            data   = make_eg_data(group)

            form  = VDEForm(self._s, title, fields, data)
            saved = await form.run()

            if saved is None:
                break

            if saved:
                update_kwargs = {}
                if 'ag_title' in saved:
                    update_kwargs['title'] = str(saved['ag_title'])[:40]
                if 'days_until_exp' in saved:
                    try:
                        update_kwargs['days_until_exp'] = int(saved['days_until_exp'])
                    except (ValueError, TypeError):
                        pass
                if 'exp_to_access' in saved:
                    try:
                        update_kwargs['exp_to_access'] = max(0, min(31, int(saved['exp_to_access'])))
                    except (ValueError, TypeError):
                        pass
                if update_kwargs:
                    db.update_access_group(ag_id, **update_kwargs)
                await self._sendl('\r\n  Changes saved.\r\n')
                await asyncio.sleep(0.4)
            else:
                break

    # ─────────────────────────────────────────────────────────────────────────
    # UL — User List
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_ul(self, pattern: str = '') -> None:
        from server import database as db

        users = db.get_all_users(pattern.strip())
        if not users:
            await self._sendl(
                f'\r\n  No users found{" matching " + repr(pattern) if pattern else ""}.\r\n'
            )
            return

        await self._send(b'\r\n')
        await self._send(self._hr())
        await self._send(
            b'  \x1b[1;32m  #  Handle               AG  Calls  Last Call          Valid\x1b[0m\r\n'
        )
        await self._send(self._hr())

        for i, u in enumerate(users, 1):
            handle    = (u['handle'] or '')[:20].ljust(20)
            ag        = u['access_group']
            calls     = u['call_count'] or 0
            last_call = self._fmt_date(u['last_call'])
            validated = 'Y' if u['is_validated'] else 'N'
            color     = b'\x1b[1;37m' if ag >= _SYSOP_AG else b'\x1b[0m'
            row       = f'  {i:3d}  {handle}  {ag:2d}  {calls:5d}  {last_call:<17}  {validated}\r\n'
            await self._send(color + row.encode() + b'\x1b[0m')

        await self._send(self._hr())
        await self._sendl(f'  {len(users)} user(s){" (filtered)" if pattern else ""}\r\n')

    # ─────────────────────────────────────────────────────────────────────────
    # KU — Kill User
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_ku(self, handle_arg: str = '') -> None:
        from server import database as db

        handle = handle_arg.strip()
        if not handle:
            handle = await self._prompt('\r\n  Kill user (handle): ')
            if not handle:
                return

        user = db.get_user_by_handle(handle)
        if not user:
            await self._sendl(f"\r\n  User '{handle}' not found.\r\n")
            return

        if user['access_group'] >= _SYSOP_AG:
            await self._sendl('\r\n  Cannot kill a sysop account.\r\n')
            return

        confirm = await self._prompt(
            f"\r\n  Kill account '{user['handle']}' (AG {user['access_group']})?  (Y/N): "
        )
        if confirm and confirm.upper().startswith('Y'):
            if db.soft_delete_user(user['id']):
                await self._sendl(f"  Account '{user['handle']}' deactivated.\r\n")
            else:
                await self._sendl('  Error deactivating account.\r\n')
        else:
            await self._sendl('  [Cancelled]\r\n')

    # ─────────────────────────────────────────────────────────────────────────
    # KB — Kill Board
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_kb(self, id_arg: str = '') -> None:
        from server import msgbase

        board = await self._resolve_board(id_arg)
        if not board:
            return

        confirm = await self._prompt(
            f"\r\n  Kill board #{board['id']} '{board['name']}'?  (Y/N): "
        )
        if confirm and confirm.upper().startswith('Y'):
            if msgbase.deactivate_subboard(board['id']):
                await self._sendl(f"  Board '{board['name']}' deactivated.\r\n")
            else:
                await self._sendl('  Error deactivating board.\r\n')
        else:
            await self._sendl('  [Cancelled]\r\n')

    # ─────────────────────────────────────────────────────────────────────────
    # Shared: board resolver
    # ─────────────────────────────────────────────────────────────────────────

    async def _resolve_board(self, arg: str):
        from server import msgbase

        arg = arg.strip()

        if not arg:
            boards = msgbase.get_all_subboards()
            if not boards:
                await self._sendl('\r\n  No boards found.\r\n')
                return None
            await self._send(b'\r\n')
            await self._send(self._hr())
            for b in boards:
                await self._send(
                    f'  {b["id"]:3d}  {b["name"]:<30}  '
                    f'R:{b["read_ag"]} W:{b["write_ag"]}'
                    f'  ({b["post_count"]} posts)\r\n'.encode()
                )
            await self._send(self._hr())
            arg = await self._prompt('  Board # or name: ')
            if not arg:
                return None

        if arg.isdigit():
            board = msgbase.get_subboard(int(arg))
            if board:
                return board
            await self._sendl(f'  Board #{arg} not found.\r\n')
            return None

        boards = msgbase.get_all_subboards()
        matches = [b for b in boards if arg.upper() in b['name'].upper()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            await self._sendl(
                f'  Ambiguous: matches {", ".join(b["name"] for b in matches[:5])}.\r\n'
            )
            return None
        await self._sendl(f"  No board matching '{arg}'.\r\n")
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # News item AT — edit item attributes (VDE form)
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_news_at(self, list_idx: int, items: list):
        """
        AT # — open the VDE attributes screen for news item at list position #.
        items is the current visible list (1-based idx passed in).
        Returns the updated item row (re-fetched), or None on cancel.
        """
        from server.vde_engine import VDEForm
        from server.vde_screens import make_news_item_fields, make_news_item_data
        from server import news as news_mod

        if list_idx < 1 or list_idx > len(items):
            await self._sendl('\r\n  Item number out of range.\r\n')
            return None

        item    = dict(items[list_idx - 1])   # sqlite3.Row → plain dict
        item_id = item['id']

        title = f"News Item #{list_idx}: {item['title'][:40]}"
        fields = make_news_item_fields()
        data   = make_news_item_data(item)

        # Show Item type as info line (ghosted header, not editable)
        info = [f'Item type: {item.get("item_type", "Text") or "Text"}']
        form = VDEForm(self._s, title, fields, data,
                       info_lines=info, num_cols=2)
        saved = await form.run()

        if saved is None:
            return None   # user cancelled / no changes

        if saved:
            news_mod.update_item_vde(item_id, saved)
            await self._sendl('\r\n  Changes saved.\r\n')

        return news_mod.get_item_by_id(item_id)

    # ─────────────────────────────────────────────────────────────────────────
    # News item ED — edit content file with the line editor
    # ─────────────────────────────────────────────────────────────────────────

    async def cmd_news_ed(self, list_idx: int, items: list):
        """
        ED # — open the line editor to edit a news item's content file.
        Pre-populates the editor with the existing content.
        """
        from server.editor import LineEditor
        from server import news as news_mod

        if list_idx < 1 or list_idx > len(items):
            await self._sendl('\r\n  Item number out of range.\r\n')
            return

        item    = dict(items[list_idx - 1])   # sqlite3.Row → plain dict
        current = news_mod.read_content(item)

        await self._sendl(
            f'\r\n  -- Edit news item: {item["title"]} --\r\n'
        )
        # Pre-populate editor with existing lines (without quoting prefix)
        existing_lines = current.splitlines() if current.strip() else None
        editor = LineEditor(self._s, subject=item['title'],
                            quote_lines=existing_lines)
        new_body = await editor.run()
        if new_body is None:
            await self._sendl('  [Edit cancelled]\r\n')
            return

        # Write to existing file or create a new one
        filename = item['filename'] or news_mod._make_filename(item['id'])
        news_mod.write_content(filename, new_body)
        if not item['filename']:
            news_mod.update_item_vde(item['id'], {'filename': filename})
        await self._sendl('  Content saved.\r\n')
