"""
sysop/main.py — ANet BBS SysOp Control Panel

A complete redesign of the control panel, faithful to CNet Pro's layout
from Chapter 3 of the manual but adapted for PyQt6 on Linux.

Aesthetic: retro-computing / BBS era. Dark background, phosphor-green
accents, monospace data columns, flat square controls. Feels like it
belongs next to the original Amiga version.

Communication with the server: direct SQLite polling via QTimer.
The panel reads the `sessions` and `activity_log` tables every 1.5
seconds. This means the panel works whether or not the server is
currently running — it just shows an empty node list when nothing is
connected.
"""

import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from PyQt6 import QtWidgets, QtGui, QtCore

# Allow running from either project root or sysop/ directory
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from config import Config


# ─────────────────────────────────────────────────────────────────────────────
# Palette / style constants
# ─────────────────────────────────────────────────────────────────────────────

STYLE = """
/* ── Global ──────────────────────────────────────────────────────── */
QMainWindow, QWidget {
    background-color: #111111;
    color: #d0d0d0;
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 12px;
}

/* ── Menu bar ─────────────────────────────────────────────────────── */
QMenuBar {
    background-color: #1a1a1a;
    color: #d0d0d0;
    border-bottom: 1px solid #2a2a2a;
}
QMenuBar::item:selected {
    background-color: #00aa33;
    color: #000000;
}
QMenu {
    background-color: #1a1a1a;
    color: #d0d0d0;
    border: 1px solid #333333;
}
QMenu::item:selected {
    background-color: #00aa33;
    color: #000000;
}

/* ── Toolbar buttons ─────────────────────────────────────────────── */
QPushButton {
    background-color: #1e1e1e;
    color: #c0c0c0;
    border: 1px solid #333333;
    padding: 4px 10px;
    min-height: 22px;
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 11px;
}
QPushButton:hover {
    background-color: #252525;
    border-color: #00aa33;
    color: #00cc44;
}
QPushButton:pressed {
    background-color: #00aa33;
    color: #000000;
}

/* ── Toggle buttons (checkable) ──────────────────────────────────── */
QPushButton#toggle_btn {
    background-color: #1a1a1a;
    color: #555555;
    border: 1px solid #2a2a2a;
    padding: 5px 12px;
    font-size: 11px;
    font-weight: bold;
}
QPushButton#toggle_btn:hover {
    border-color: #444444;
    color: #888888;
}
QPushButton#toggle_btn:checked {
    background-color: #002200;
    color: #00cc44;
    border: 1px solid #00aa33;
}
QPushButton#toggle_btn:checked:hover {
    background-color: #003300;
}

/* ── SAM stats bar ───────────────────────────────────────────────── */
QFrame#stats_bar {
    background-color: #161616;
    border-top: 1px solid #2a2a2a;
    border-bottom: 1px solid #2a2a2a;
}
QLabel#stat_label {
    color: #888888;
    font-size: 11px;
    padding: 2px 6px;
}
QLabel#stat_value {
    color: #00cc44;
    font-size: 11px;
    font-weight: bold;
    padding: 2px 0px;
    min-width: 28px;
}
QLabel#bbs_name_label {
    color: #00cc44;
    font-size: 13px;
    font-weight: bold;
    padding: 2px 8px 2px 4px;
}

/* ── Column header bar ───────────────────────────────────────────── */
QFrame#col_header {
    background-color: #161616;
    border-bottom: 1px solid #333333;
}
QLabel#col_header_label {
    color: #00aa33;
    font-size: 11px;
    font-weight: bold;
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
}

/* ── Node status table ───────────────────────────────────────────── */
QTableWidget {
    background-color: #111111;
    color: #999999;
    gridline-color: transparent;
    border: none;
    selection-background-color: #002200;
    selection-color: #00cc44;
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 12px;
    outline: 0;
}
QTableWidget::item {
    padding: 2px 4px;
    border: none;
}
QTableWidget::item:selected {
    background-color: #002200;
    color: #00cc44;
}
QHeaderView {
    background-color: #161616;
}
QHeaderView::section {
    background-color: #161616;
    color: #00aa33;
    border: none;
    border-bottom: 1px solid #333333;
    padding: 3px 4px;
    font-size: 11px;
    font-weight: bold;
}
QScrollBar:vertical {
    background: #1a1a1a;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #333333;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background: #00aa33;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

/* ── Activity log ─────────────────────────────────────────────────── */
QPlainTextEdit#activity_log {
    background-color: #0d0d0d;
    color: #666666;
    border: none;
    border-top: 1px solid #2a2a2a;
    font-family: "DejaVu Sans Mono", "Courier New", monospace;
    font-size: 11px;
    padding: 4px;
}

/* ── Status bar ──────────────────────────────────────────────────── */
QStatusBar {
    background-color: #161616;
    color: #555555;
    border-top: 1px solid #2a2a2a;
    font-size: 11px;
}

/* ── Separator lines ─────────────────────────────────────────────── */
QFrame[frameShape="4"],   /* HLine */
QFrame[frameShape="5"] {  /* VLine */
    color: #2a2a2a;
}
"""

# Colors for node row text, set programmatically
COLOR_IDLE    = QtGui.QColor("#555555")
COLOR_LOGIN   = QtGui.QColor("#888833")
COLOR_ONLINE  = QtGui.QColor("#00cc44")
COLOR_DEFAULT = QtGui.QColor("#888888")


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers (read-only from the panel's perspective)
# ─────────────────────────────────────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection | None:
    """Open a read-only connection to the BBS database, or return None."""
    if not Config.DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{Config.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_sessions() -> list[sqlite3.Row]:
    conn = _db_connect()
    if conn is None:
        return []
    with conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY node_id"
        ).fetchall()
    conn.close()
    return rows


def fetch_stats() -> dict:
    """Return SAM-style stats: mail, feedback, new_users, calls_logged."""
    conn = _db_connect()
    stats = {"mail": 0, "feedback": 0, "new_users": 0, "calls": 0, "members": 0}
    if conn is None:
        return stats
    with conn:
        r = conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE is_deleted=0"
        ).fetchone()
        stats["members"] = r["c"] if r else 0

        r = conn.execute(
            "SELECT SUM(call_count) as c FROM users"
        ).fetchone()
        stats["calls"] = r["c"] or 0 if r else 0
    conn.close()
    return stats


def fetch_recent_activity(limit: int = 80) -> list[sqlite3.Row]:
    conn = _db_connect()
    if conn is None:
        return []
    # Table may not exist yet if server hasn't written any activity
    try:
        with conn:
            rows = conn.execute(
                """SELECT * FROM activity_log
                   ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return list(reversed(rows))   # oldest first for the log display


def fetch_system_flags() -> dict:
    """Read toggle flags from system_config table."""
    conn = _db_connect()
    flags = {
        "doors_closed":    False,
        "files_closed":    False,
        "msgs_closed":     False,
        "no_new_users":    False,
        "sysop_in":        False,
    }
    if conn is None:
        return flags
    try:
        with conn:
            rows = conn.execute(
                "SELECT key, value FROM system_config"
            ).fetchall()
            for row in rows:
                if row["key"] in flags:
                    flags[row["key"]] = (row["value"] == "1")
    except sqlite3.OperationalError:
        pass
    conn.close()
    return flags


def set_system_flag(key: str, value: bool) -> None:
    """Write a toggle flag to system_config. Uses read-write connection."""
    if not Config.DB_PATH.exists():
        return
    conn = sqlite3.connect(Config.DB_PATH)
    try:
        with conn:
            conn.execute(
                """INSERT INTO system_config (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, "1" if value else "0"),
            )
            conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────

class ANetControlPanel(QtWidgets.QMainWindow):

    # Column definitions: (key, header_text, width_px)
    COLUMNS = [
        ("node_id",      "P#",     40),
        ("handle",       "User",   180),
        ("access_group", "AG",     38),
        ("connected_at", "Logon",  70),
        ("speed",        "CPS",    52),
        ("location",     "From",   220),
        ("status",       "Status", 0),    # 0 = stretch
    ]

    def __init__(self):
        super().__init__()
        self._last_activity_id = 0    # track last log row we've appended
        self._setup_window()
        self._build_menu()
        self._build_central()
        self._build_statusbar()
        self._start_timer()
        self._refresh()               # initial populate

    # ── Window setup ──────────────────────────────────────────────────────────

    def _setup_window(self):
        self.setWindowTitle(f"ANet Control Panel")
        self.resize(1100, 520)
        self.setMinimumSize(800, 400)
        self.setStyleSheet(STYLE)
        # No icon path dependency — clears any stale icon
        self.setWindowIcon(QtGui.QIcon())

    # ── Menu bar ──────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        # File menu
        file_menu = mb.addMenu("&File")
        file_menu.addAction("Write Setup").triggered.connect(self._write_setup)
        file_menu.addSeparator()
        act_quit = file_menu.addAction("&Quit")
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)

        # Port menu (affects selected node)
        port_menu = mb.addMenu("&Port")
        port_menu.addAction("Send OLM...").triggered.connect(self._send_olm)
        port_menu.addAction("Hangup / Clear Line").triggered.connect(self._hangup)
        port_menu.addSeparator()
        port_menu.addAction("User Info").triggered.connect(self._show_user_info)

        # System menu
        sys_menu = mb.addMenu("&System")
        sys_menu.addAction("System Info").triggered.connect(self._show_sys_info)
        sys_menu.addAction("Configuration Editor").triggered.connect(self._open_config)

    # ── Central widget ────────────────────────────────────────────────────────

    def _build_central(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_stats_bar())
        root.addWidget(self._build_node_table())
        root.addWidget(self._build_activity_log())
        root.addWidget(self._build_toggle_bar())

    def _build_toolbar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(32)
        bar.setStyleSheet("background-color: #1a1a1a; border-bottom: 1px solid #2a2a2a;")
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)

        buttons = [
            ("SysInfo",  self._show_sys_info),
            ("UserInfo", self._show_user_info),
            ("Config",   self._open_config),
            ("Mail",     self._open_mail),
            ("Files",    self._open_files),
            ("Yanks",    self._open_yanks),
            ("News",     self._open_news),
            ("Edit",     self._open_edit),
        ]
        for label, slot in buttons:
            btn = QtWidgets.QPushButton(label)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

        layout.addStretch()

        quit_btn = QtWidgets.QPushButton("Quit")
        quit_btn.clicked.connect(self.close)
        layout.addWidget(quit_btn)

        return bar

    def _build_stats_bar(self) -> QtWidgets.QFrame:
        """The SAM statistics bar showing system-wide at-a-glance info."""
        bar = QtWidgets.QFrame()
        bar.setObjectName("stats_bar")
        bar.setFixedHeight(26)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)

        # BBS name
        self._lbl_bbs_name = QtWidgets.QLabel(Config.BBS_NAME)
        self._lbl_bbs_name.setObjectName("bbs_name_label")
        layout.addWidget(self._lbl_bbs_name)

        # Divider
        layout.addWidget(self._vline())

        # SAM stat pairs: (label_text, attr_name, default)
        sam_stats = [
            ("Mail:",      "_stat_mail",    "0"),
            ("Feedback:",  "_stat_feedback","0"),
            ("Members:",   "_stat_members", "0"),
            ("Calls:",     "_stat_calls",   "0"),
        ]
        for lbl_text, attr, default in sam_stats:
            lbl = QtWidgets.QLabel(lbl_text)
            lbl.setObjectName("stat_label")
            layout.addWidget(lbl)

            val = QtWidgets.QLabel(default)
            val.setObjectName("stat_value")
            val.setMinimumWidth(36)
            layout.addWidget(val)
            setattr(self, attr, val)

            layout.addWidget(self._vline())

        # Server status indicator (rightmost)
        self._lbl_server_status = QtWidgets.QLabel("● SERVER DOWN")
        self._lbl_server_status.setObjectName("stat_label")
        self._lbl_server_status.setStyleSheet("color: #553333; padding: 2px 6px;")
        layout.addWidget(self._lbl_server_status)

        layout.addStretch()

        # Clock — updated by timer
        self._lbl_clock = QtWidgets.QLabel("--:--:--")
        self._lbl_clock.setObjectName("stat_value")
        self._lbl_clock.setStyleSheet("color: #555555; min-width: 72px;")
        layout.addWidget(self._lbl_clock)

        return bar

    def _build_node_table(self) -> QtWidgets.QTableWidget:
        """The main node status table — one row per configured node."""
        tbl = QtWidgets.QTableWidget()
        self._node_table = tbl

        cols = [c[1] for c in self.COLUMNS]
        tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.setRowCount(Config.MAX_NODES)

        # Column widths
        hh = tbl.horizontalHeader()
        for i, (_, _, w) in enumerate(self.COLUMNS):
            if w == 0:
                hh.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.Stretch)
            else:
                hh.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.Fixed)
                tbl.setColumnWidth(i, w)

        tbl.verticalHeader().setVisible(False)
        tbl.verticalHeader().setDefaultSectionSize(18)
        tbl.setShowGrid(False)
        tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setAlternatingRowColors(False)
        tbl.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

        # Pre-populate idle rows
        self._populate_idle_rows()

        # Stretch to fill but cap height so activity log always shows
        tbl.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Minimum
        )
        max_rows_height = 18 * Config.MAX_NODES + tbl.horizontalHeader().height() + 4
        tbl.setMaximumHeight(max_rows_height)

        return tbl

    def _populate_idle_rows(self):
        """Fill all rows with idle placeholder data."""
        tbl = self._node_table
        for i in range(Config.MAX_NODES):
            values = [str(i), "", "", "", "", "", "Idle"]
            for j, val in enumerate(values):
                item = QtWidgets.QTableWidgetItem(val)
                item.setForeground(COLOR_IDLE)
                tbl.setItem(i, j, item)

    def _build_activity_log(self) -> QtWidgets.QPlainTextEdit:
        """Scrolling activity feed — server events append to DB, we display them."""
        log = QtWidgets.QPlainTextEdit()
        log.setObjectName("activity_log")
        log.setReadOnly(True)
        log.setMaximumBlockCount(500)    # cap memory usage
        log.setPlaceholderText("  Activity log — waiting for server...")
        log.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        log.setMinimumHeight(80)
        self._activity_log = log
        return log

    def _build_toggle_bar(self) -> QtWidgets.QWidget:
        """Bottom row of toggle buttons matching CNet's control panel flags."""
        bar = QtWidgets.QWidget()
        bar.setFixedHeight(36)
        bar.setStyleSheet("background-color: #161616; border-top: 1px solid #2a2a2a;")
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(3)

        # (label_text, db_key, attr_name)
        toggles = [
            ("Doors Open",    "doors_closed",    "_toggle_doors"),
            ("Files Open",    "files_closed",    "_toggle_files"),
            ("Messages Open", "msgs_closed",     "_toggle_msgs"),
            ("New Users On",  "no_new_users",    "_toggle_newusers"),
            ("SysOp Out",     "sysop_in",        "_toggle_sysop"),
        ]

        for label, db_key, attr in toggles:
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("toggle_btn")
            btn.setCheckable(True)
            btn.setMinimumWidth(100)
            # Closure trick: bind db_key and btn at definition time
            btn.toggled.connect(self._make_toggle_handler(btn, db_key, label))
            layout.addWidget(btn)
            setattr(self, attr, btn)

        layout.addStretch()

        # Node count indicator
        self._lbl_nodes = QtWidgets.QLabel("0 / {} nodes active".format(Config.MAX_NODES))
        self._lbl_nodes.setStyleSheet("color: #444444; font-size: 11px; padding-right: 8px;")
        layout.addWidget(self._lbl_nodes)

        return bar

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_statusbar(self):
        """Initialise the Qt status bar at the bottom of the main window."""
        self.statusBar().showMessage(f"ANet Control Panel — {Config.BBS_NAME}")

    @staticmethod
    def _vline() -> QtWidgets.QFrame:
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        line.setStyleSheet("color: #2a2a2a; margin: 4px 6px;")
        return line

    def _make_toggle_handler(self, btn, db_key: str, base_label: str):
        """
        Returns a slot that:
          • updates the button label to show current state
          • writes the flag to the DB so the server can react
        The button represents a restriction — checked = restriction is ACTIVE.
        We invert the label so it always shows the current system state.
        """
        # Label pairs: (when_unchecked, when_checked)
        label_map = {
            "doors_closed":  ("Doors Open",     "Doors CLOSED"),
            "files_closed":  ("Files Open",     "Files CLOSED"),
            "msgs_closed":   ("Messages Open",  "Messages CLOSED"),
            "no_new_users":  ("New Users ON",   "No New Users"),
            "sysop_in":      ("SysOp Out",      "SysOp IN"),
        }
        off_lbl, on_lbl = label_map.get(db_key, (base_label, base_label))

        def handler(checked: bool):
            btn.setText(on_lbl if checked else off_lbl)
            set_system_flag(db_key, checked)

        return handler

    # ── Timer and refresh ─────────────────────────────────────────────────────

    def _start_timer(self):
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1500)      # 1.5 seconds
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def _refresh(self):
        """Called every 1.5 seconds — update all dynamic elements."""
        self._refresh_clock()
        self._refresh_stats()
        self._refresh_nodes()
        self._refresh_activity()
        self._refresh_toggle_states()

    def _refresh_clock(self):
        now = datetime.now().strftime("%H:%M:%S")
        self._lbl_clock.setText(now)

    def _refresh_stats(self):
        db_alive = Config.DB_PATH.exists()
        if db_alive:
            self._lbl_server_status.setText("● DB OK")
            self._lbl_server_status.setStyleSheet("color: #336633; padding: 2px 6px;")
        else:
            self._lbl_server_status.setText("● NO DB")
            self._lbl_server_status.setStyleSheet("color: #553333; padding: 2px 6px;")

        stats = fetch_stats()
        self._stat_mail.setText(str(stats.get("mail", 0)))
        self._stat_feedback.setText(str(stats.get("feedback", 0)))
        self._stat_members.setText(str(stats.get("members", 0)))
        self._stat_calls.setText(str(stats.get("calls", 0)))

    def _refresh_nodes(self):
        sessions = fetch_sessions()
        # Build a map of node_id → session row for fast lookup
        sess_map = {s["node_id"]: s for s in sessions}

        active_count = 0
        tbl = self._node_table

        for row_idx in range(Config.MAX_NODES):
            sess = sess_map.get(row_idx)

            if sess is None:
                # No DB record at all — fully idle
                values = [str(row_idx), "", "", "", "", "", "Idle"]
                color = COLOR_IDLE
            else:
                status = sess["status"]
                handle = sess["handle"] or "(connecting)"
                logon  = sess["connected_at"][11:16] if sess["connected_at"] else ""
                speed  = str(sess["speed"]) if sess["speed"] else ""
                loc    = sess["location"] or ""
                ag     = str(sess["access_group"]) if sess["access_group"] else ""

                values = [str(row_idx), handle, ag, logon, speed, loc, status]

                if status == "online":
                    color = COLOR_ONLINE
                    active_count += 1
                elif status in ("waiting", "logging_in"):
                    color = COLOR_LOGIN
                else:
                    color = COLOR_IDLE

            for col_idx, val in enumerate(values):
                item = tbl.item(row_idx, col_idx)
                if item is None:
                    item = QtWidgets.QTableWidgetItem(val)
                    tbl.setItem(row_idx, col_idx, item)
                else:
                    item.setText(val)
                item.setForeground(color)

        # Update active count label
        self._lbl_nodes.setText(f"{active_count} / {Config.MAX_NODES} nodes active")

        # Update server status based on any active sessions
        if sessions:
            self._lbl_server_status.setText("● SERVER UP")
            self._lbl_server_status.setStyleSheet("color: #00aa33; padding: 2px 6px;")

    def _refresh_activity(self):
        rows = fetch_recent_activity(limit=100)
        if not rows:
            return

        # Only append rows we haven't seen yet
        new_rows = [r for r in rows if r["id"] > self._last_activity_id]
        if not new_rows:
            return

        log = self._activity_log
        for r in new_rows:
            ts   = r["timestamp"][11:19] if r["timestamp"] else "--:--:--"
            node = f"[{r['node_id']}]" if r["node_id"] is not None else "   "
            msg  = r["message"] or ""
            line = f"  {ts}  {node:<4}  {msg}"
            log.appendPlainText(line)

        self._last_activity_id = new_rows[-1]["id"]
        # Auto-scroll to bottom
        sb = log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_toggle_states(self):
        """Sync toggle button state from DB (allows external changes to show)."""
        flags = fetch_system_flags()
        # Map db_key → (button_widget, inverted?)
        mapping = [
            ("doors_closed",  self._toggle_doors,    False),
            ("files_closed",  self._toggle_files,    False),
            ("msgs_closed",   self._toggle_msgs,     False),
            ("no_new_users",  self._toggle_newusers, False),
            ("sysop_in",      self._toggle_sysop,    False),
        ]
        for key, btn, _ in mapping:
            # Block signals so we don't trigger the DB write again
            btn.blockSignals(True)
            btn.setChecked(flags.get(key, False))
            btn.blockSignals(False)

    # ── Action stubs (for future milestones) ─────────────────────────────────

    def _write_setup(self):
        QtWidgets.QMessageBox.information(self, "Write Setup",
            "Setup saved.\n(Full implementation coming in Milestone 3)")

    def _send_olm(self):
        sel = self._node_table.currentRow()
        if sel < 0:
            QtWidgets.QMessageBox.warning(self, "Send OLM", "Select a node first.")
            return
        msg, ok = QtWidgets.QInputDialog.getText(
            self, "Send OLM", f"Message to send to node {sel}:"
        )
        if ok and msg:
            # TODO Milestone 3: write OLM record to DB; server picks it up
            self._activity_log.appendPlainText(
                f"  {datetime.now().strftime('%H:%M:%S')}  OLM→{sel}  {msg}"
            )

    def _hangup(self):
        sel = self._node_table.currentRow()
        if sel < 0:
            QtWidgets.QMessageBox.warning(self, "Hangup", "Select a node first.")
            return
        reply = QtWidgets.QMessageBox.question(
            self, "Hangup", f"Disconnect node {sel}?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            # TODO Milestone 3: write hangup command to DB; server acts on it
            self._activity_log.appendPlainText(
                f"  {datetime.now().strftime('%H:%M:%S')}  [SysOp] Hangup requested for node {sel}"
            )

    def _show_user_info(self):
        sel = self._node_table.currentRow()
        if sel < 0:
            QtWidgets.QMessageBox.information(self, "UserInfo", "Select a node first.")
            return
        handle_item = self._node_table.item(sel, 1)
        handle = handle_item.text() if handle_item else ""
        if not handle:
            QtWidgets.QMessageBox.information(self, "UserInfo", "No user on this node.")
            return
        QtWidgets.QMessageBox.information(
            self, f"UserInfo — {handle}",
            f"Node: {sel}\nHandle: {handle}\n\nFull user detail window coming in Milestone 3."
        )

    def _show_sys_info(self):
        db_exists = "Yes" if Config.DB_PATH.exists() else "No (server not run yet)"
        stats = fetch_stats()
        info = (
            f"BBS Name:    {Config.BBS_NAME}\n"
            f"Max Nodes:   {Config.MAX_NODES}\n"
            f"Telnet Port: {Config.TELNET_PORT}\n"
            f"DB Present:  {db_exists}\n"
            f"Members:     {stats.get('members', 0)}\n"
            f"Total Calls: {stats.get('calls', 0)}\n"
        )
        QtWidgets.QMessageBox.information(self, "System Info", info)

    def _open_config(self):
        QtWidgets.QMessageBox.information(self, "Configuration",
            "Configuration editor — coming in Milestone 3.")

    def _open_mail(self):
        QtWidgets.QMessageBox.information(self, "Mail",
            "Mail reader — coming after Milestone 3.")

    def _open_files(self):
        QtWidgets.QMessageBox.information(self, "Files",
            "File browser — coming after Milestone 3.")

    def _open_yanks(self):
        QtWidgets.QMessageBox.information(self, "Yanks",
            "Yank buffer — coming after Milestone 3.")

    def _open_news(self):
        QtWidgets.QMessageBox.information(self, "News",
            "News manager — coming after Milestone 3.")

    def _open_edit(self):
        QtWidgets.QMessageBox.information(self, "Edit",
            "Text editor — coming after Milestone 3.")

    # ── Close handler ─────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._timer.stop()
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ANet Control Panel")

    window = ANetControlPanel()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
