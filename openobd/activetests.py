"""
activetests — the Active Tests tab: a READ-ONLY audit of truck-mcp's control
journal under the shared workspace chrome (breadcrumb + chips, one 36 px
toolbar, inline banners) the Tuning, Diagnostics and Live Data tabs use.

There is deliberately NO arm / fire / activate / release path here. OpenOBD
holds no control lease, owns no console, writes no journal and sends no frame:
arm and fire exist only in the truck-mcp CLI behind a console nonce. This page
reads ctljournal.control_state() and reports one honest verdict:

  clean        every located journal read fully, no activation unreleased
  outstanding  an activation has no matching release - the truck may be
               holding an actuator; recovery is truck-mcp's, never OpenOBD's
  unknown      no journal located, or damage means the answer cannot be
               trusted (a warning, never the green all-clear)
  read failed  the read itself raised; the last good table stays on screen
               but is labelled as the last good read, never as current

Cues from HP Tuners VCM Scanner / GM GDS2 / Autel special-function screens: a
status strip that always says how fresh the verdict is, a filterable audit
log, and an unmistakable safe / unsafe colour.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHeaderView, QLabel, QLineEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import ctljournal, theme
from .appsettings import app_settings
from .tuningui import (
    Breadcrumb, CollapsiblePanel, empty_state_label, make_toolbar,
    plain_tooltip, repolish,
)

CONTROL_POLL_MS = 3000
TABLE_LIMIT = 100           # newest journal entries listed
SHOW_KEY = "activetests/show"
SOURCES_COLLAPSED_KEY = "activetests/sources_collapsed"
#: Show combo: (settings value, label).
SHOW_MODES = (("all", "All entries"),
              ("controls", "Activate + refused only"),
              ("24h", "Last 24 h"))
COLUMNS = ("When", "Event", "Module", "CPID", "Detail")
COL_WHEN, COL_EVENT, COL_MODULE, COL_CPID, COL_DETAIL = range(5)
#: Keys shown in their own column (or internal), not repeated in Detail.
_OWN_COLUMN_KEYS = ("at", "event", "module", "cpid", "_journal")

EXPLAINER = ("No executable vehicle controls are known to OpenOBD. This page "
             "is a read-only audit of truck-mcp's control journal.")
RECOVERY_SENTENCE = ("OpenOBD will not send the release; run truck-mcp "
                     "recovery, which replays the exact release bytes from "
                     "the journal.")

VERDICT_CHIPS = {
    "clean": ("clean", "ok"),
    "outstanding": ("CONTROL LEFT ACTIVE", "danger"),
    "unknown": ("journal unknown", "warning"),
}


def event_kind(event: str) -> str:
    """Colour class of a journal event: danger | ok | neutral."""
    if event == "activate" or event.startswith("fire_refused"):
        return "danger"
    if event in ("released", "return_to_normal"):
        return "ok"
    return "neutral"


_EVENT_TOKENS = {"danger": "danger-text", "ok": "ok-text", "neutral": "text"}


def _epoch(at) -> Optional[float]:
    try:
        return float(at)
    except (TypeError, ValueError):
        return None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _detail(entry: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in entry.items()
                     if k not in _OWN_COLUMN_KEYS)


class ActiveTestsPage(QWidget):
    """DISPLAY-ONLY vehicle-control state. Commands nothing; enables no control.

    Public API (unchanged since it lived in livedata.py): refresh(),
    shutdown(), and polling only while the page is shown."""

    def __init__(self, settings=None):
        super().__init__()
        self.setObjectName("activeTestsPage")
        self.setStyleSheet(theme.workspace_qss())
        self.settings = settings if settings is not None else app_settings()
        self._active = False
        self._state: Optional[ctljournal.ControlState] = None
        self._as_of: Optional[datetime] = None      # last SUCCESSFUL read
        self._read_error: Optional[str] = None      # why the last read failed
        self._roots: list[Path] = []                # searched at last good read

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.crumb = Breadcrumb(elastic_chips=True)
        self.crumb.set_path(["Active Tests", "Control journal"])
        root.addWidget(self.crumb)
        root.addWidget(self._build_toolbar())

        body = QVBoxLayout()
        m = theme.SPACING["sm"]
        body.setContentsMargins(m, m, m, m)
        body.setSpacing(m)
        self.banner = QLabel()
        self.banner.setObjectName("activeBanner")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.PlainText)    # journal / exception text
        self.banner.setProperty("banner", "danger")
        self.banner.hide()
        # Journal paths are long unbreakable words: word wrap cannot fold
        # them, so without this the banner would pin the page (and through
        # QTabWidget the window) to the longest path. Below that width the
        # path tail clips; every path is also in Copy report.
        self.banner.setMinimumWidth(1)
        body.addWidget(self.banner)
        self.explainer = empty_state_label(EXPLAINER)
        body.addWidget(self.explainer)

        self.tree = QTreeWidget()
        self.tree.setObjectName("journalTree")
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        for col in (COL_WHEN, COL_EVENT, COL_MODULE, COL_CPID):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        body.addWidget(self.tree, 1)

        self.sources_text = QLabel()
        self.sources_text.setTextFormat(Qt.PlainText)   # filesystem paths
        self.sources_text.setWordWrap(True)
        self.sources_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sources_text.setMinimumWidth(1)      # see the banner above
        collapsed = str(self.settings.value(SOURCES_COLLAPSED_KEY,
                                            "true")).lower() == "true"
        self.sources = CollapsiblePanel("Journal sources", self.sources_text,
                                        expanded=not collapsed,
                                        body_width=None, align=Qt.AlignLeft)
        self.sources.toggled.connect(
            lambda on: self.settings.setValue(
                SOURCES_COLLAPSED_KEY, "false" if on else "true"))
        body.addWidget(self.sources)
        root.addLayout(body, 1)

        # F5 re-reads while this page is showing (enabled in showEvent), so it
        # never collides with the Dashboard's, Diagnostics' or Live Data's F5.
        self._f5 = QShortcut(QKeySequence(Qt.Key_F5), self)
        self._f5.setContext(Qt.WindowShortcut)
        self._f5.setEnabled(False)
        self._f5.activated.connect(self.refresh)

        self._poll = QTimer(self)
        self._poll.timeout.connect(self.refresh)
        self.refresh()

    # -- chrome ---------------------------------------------------------------
    def _build_toolbar(self):
        tb = make_toolbar("activeToolbar")
        self.toolbar = tb
        self.act_refresh = self._action(
            "refresh", "Refresh",
            "Re-read every truck-mcp control journal now  (F5)", self.refresh)
        tb.addAction(self.act_refresh)
        btn = tb.widgetForAction(self.act_refresh)
        if btn is not None:
            btn.setProperty("primary", True)
        self.act_copy_report = self._action(
            "copy", "Copy report",
            "Copy the verdict, any outstanding control and the listed "
            "journal entries as text", self.copy_report)
        tb.addAction(self.act_copy_report)
        tb.addSeparator()
        self.filter = QLineEdit()
        self.filter.setObjectName("journalFilter")
        self.filter.setPlaceholderText("Filter journal  (Ctrl+F)")
        self.filter.setToolTip("Show only entries whose event, module or "
                               "detail contains this text (display only). "
                               "Esc clears.")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(110)
        self.filter.setMaximumWidth(200)
        self.filter.textChanged.connect(self._apply_filter)
        self.filter.installEventFilter(self)
        tb.addWidget(self.filter)
        tb.addWidget(QLabel("Show"))
        self.show_combo = QComboBox()
        for value, label in SHOW_MODES:
            self.show_combo.addItem(label, value)
        saved = str(self.settings.value(SHOW_KEY, "all"))
        idx = self.show_combo.findData(saved)
        self.show_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.show_combo.currentIndexChanged.connect(self._on_show_changed)
        tb.addWidget(self.show_combo)
        return tb

    def _action(self, glyph: str, text: str, tip: str, slot) -> QAction:
        a = QAction(theme.glyph_icon(glyph), text, self)
        a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    def show_mode(self) -> str:
        return str(self.show_combo.currentData())

    def _on_show_changed(self, *_a) -> None:
        self.settings.setValue(SHOW_KEY, self.show_mode())
        self._fill_table()

    def verdict(self) -> str:
        """What the chip says: clean | outstanding | unknown | read failed."""
        if self._read_error is not None:
            return "read failed"
        return self._state.verdict if self._state is not None else "unknown"

    def _chips(self) -> list[tuple[str, str]]:
        chips = []
        if self._read_error is not None:
            chips.append(("read failed", "danger"))
        if self._state is not None:
            text, kind = VERDICT_CHIPS.get(self._state.verdict,
                                           VERDICT_CHIPS["unknown"])
            # A failed read keeps an unsafe last-good verdict on screen (it
            # is the more cautious reading); a "clean" one is not repeated.
            if self._read_error is None or self._state.verdict != "clean":
                chips.append((text, kind))
            chips.append((_plural(len(self._state.reads), "journal"),
                          "neutral"))
            chips.append((_plural(self._state.total_entries, "record"),
                          "neutral"))
        if self._active:
            chips.append(("Polling", "muted"))
        return chips

    def _note(self) -> str:
        good = (self._as_of.strftime("%H:%M:%S") if self._as_of is not None
                else None)
        if self._read_error is not None:
            tail = (f"last good read {good}" if good
                    else "no successful read yet")
            return f"Read failed: {self._read_error} · {tail}"
        roots = ", ".join(str(r) for r in self._roots) or "no data roots"
        return f"as of {good} · searched {roots}"

    def _banner_text(self) -> tuple[Optional[str], str]:
        parts: list[str] = []
        kind = "danger"
        if self._read_error is not None:
            good = (self._as_of.strftime("%H:%M:%S") if self._as_of
                    else None)
            parts.append(
                f"Could not read the control journal: {self._read_error}. "
                + (f"The table below is the last good read ({good}), not "
                   "current." if good else "No journal has been read yet."))
        st = self._state
        if st is not None and st.verdict == "outstanding":
            lines = [f"module {o.get('module')}  CPID {o.get('cpid')}  "
                     f"release bytes {o.get('release', '?')}\n"
                     f"    journal {o.get('_journal', '?')}"
                     for o in st.outstanding]
            parts.append(
                "CONTROL LEFT ACTIVE. A control session ended without a "
                "recorded release; the truck may be holding an actuator:\n"
                + "\n".join(lines) + "\n" + RECOVERY_SENTENCE)
        elif st is not None and st.verdict == "unknown" and not parts:
            kind = "warning"
            parts.append("CONTROL STATE UNKNOWN. " + st.detail)
        return ("\n\n".join(parts) if parts else None), kind

    def _refresh_chrome(self) -> None:
        self.crumb.set_chips(self._chips())
        self.crumb.set_note(self._note())
        text, kind = self._banner_text()
        if not text:
            self.banner.hide()
        else:
            self.banner.setText(text)
            if self.banner.property("banner") != kind:
                self.banner.setProperty("banner", kind)
                repolish(self.banner)
            self.banner.show()

    # -- lifecycle: only poll while the tab is actually visible --------------
    def showEvent(self, event):   # noqa: N802
        super().showEvent(event)
        self._f5.setEnabled(True)
        if not self._active:
            self._active = True
            self.refresh()
            self._poll.start(CONTROL_POLL_MS)

    def hideEvent(self, event):   # noqa: N802
        super().hideEvent(event)
        self._f5.setEnabled(False)
        self._active = False
        self._poll.stop()
        self._refresh_chrome()

    def shutdown(self):
        self._active = False
        self._f5.setEnabled(False)
        self._poll.stop()

    def closeEvent(self, event):   # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    # -- the read -------------------------------------------------------------
    def refresh(self):
        try:
            roots = list(ctljournal.control_root_candidates())
            state = ctljournal.control_state()
        except Exception as exc:   # the safety page must never fail to render
            self._read_error = str(exc) or type(exc).__name__
        else:
            self._state = state
            self._roots = roots
            self._read_error = None
            self._as_of = datetime.now()
            self._fill_table()
            self._fill_sources()
        self._refresh_chrome()

    # -- journal table --------------------------------------------------------
    def _entries(self) -> list[dict]:
        """Newest first, Show mode applied, capped at TABLE_LIMIT. Entries
        without a usable timestamp sort last (ctljournal already downgrades
        the verdict for them)."""
        if self._state is None:
            return []
        flat = []
        for r in self._state.reads:
            for e in r.entries:
                d = dict(e)
                d.setdefault("_journal", str(r.path))
                flat.append(d)
        flat.sort(key=lambda e: (_epoch(e.get("at")) is not None,
                                 _epoch(e.get("at")) or 0.0), reverse=True)
        mode = self.show_mode()
        if mode == "controls":
            flat = [e for e in flat
                    if event_kind(str(e.get("event", ""))) == "danger"]
        elif mode == "24h":
            cutoff = (datetime.now() - timedelta(hours=24)).timestamp()
            flat = [e for e in flat
                    if (_epoch(e.get("at")) or 0.0) >= cutoff]
        return flat[:TABLE_LIMIT]

    def _fill_table(self) -> None:
        self.tree.clear()
        entries = self._entries()
        if not entries:
            item = QTreeWidgetItem(["No journal entries"])
            item.setFlags(Qt.NoItemFlags)
            item.setForeground(0, theme.qcolor("text-muted"))
            item.setData(0, Qt.UserRole, "empty")
            self.tree.addTopLevelItem(item)
            item.setFirstColumnSpanned(True)
            return
        last_day = None
        for e in entries:
            at = _epoch(e.get("at"))
            if at is not None:
                try:
                    dt = datetime.fromtimestamp(at)
                except (OverflowError, OSError, ValueError):
                    dt = None
            else:
                dt = None
            if dt is None:
                when, full = str(e.get("at", "")), str(e.get("at", ""))
            else:
                full = dt.strftime("%Y-%m-%d %H:%M:%S")
                when = (full if dt.date() != last_day
                        else dt.strftime("%H:%M:%S"))
                last_day = dt.date()
            event = str(e.get("event", ""))
            detail = _detail(e)
            item = QTreeWidgetItem([
                when, event, str(e.get("module", "")),
                str(e.get("cpid", "")), detail])
            kind = event_kind(event)
            item.setForeground(COL_EVENT, theme.qcolor(_EVENT_TOKENS[kind]))
            item.setData(COL_EVENT, Qt.UserRole, kind)
            item.setToolTip(COL_WHEN, plain_tooltip(full))
            item.setToolTip(COL_DETAIL, plain_tooltip(
                f"{detail}\njournal: {e.get('_journal', '?')}"))
            self.tree.addTopLevelItem(item)
        self._apply_filter()

    def _apply_filter(self, *_a) -> None:
        """Display-only: hides rows, never changes what was read."""
        needle = self.filter.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) == "empty":
                continue
            hay = " ".join(item.text(c) for c in
                           (COL_EVENT, COL_MODULE, COL_CPID, COL_DETAIL))
            item.setHidden(bool(needle) and needle not in hay.lower())

    def visible_rows(self) -> list[list[str]]:
        rows = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.isHidden() or item.data(0, Qt.UserRole) == "empty":
                continue
            rows.append([item.text(c) for c in range(len(COLUMNS))])
        return rows

    def _fill_sources(self) -> None:
        reads = {str(r.path): r for r in (self._state.reads
                                          if self._state else [])}
        lines = []
        for root in self._roots:
            path = root / "control" / ctljournal.JOURNAL_NAME
            r = reads.get(str(path))
            if r is None:
                lines.append(f"no journal   {root}")
            elif r.error:
                lines.append(f"unreadable   {path}  ({r.error})")
            else:
                damaged = (f", {r.unreadable_lines} damaged line(s)"
                           if r.unreadable_lines else "")
                lines.append(f"journal      {path}  "
                             f"({_plural(len(r.entries), 'record')}{damaged})")
        self.sources_text.setText("\n".join(lines) or "No data roots searched.")

    # -- report / search --------------------------------------------------------
    def report(self) -> str:
        out = ["OpenOBD Active Tests - control journal report (read-only)"]
        if self._read_error is not None:
            out.append(f"Read failed: {self._read_error}")
        good = (self._as_of.strftime("%Y-%m-%d %H:%M:%S") if self._as_of
                else "never")
        out.append(f"Last successful read: {good}")
        st = self._state
        if st is not None:
            out.append(f"Verdict: {VERDICT_CHIPS.get(st.verdict, (st.verdict,))[0]}")
            if st.detail:
                out.append(f"Detail: {st.detail}")
            out.append(f"Journals: {len(st.reads)}, records: "
                       f"{st.total_entries}")
            if st.outstanding:
                out.append("Outstanding controls:")
                for o in st.outstanding:
                    out.append(
                        f"  module {o.get('module')}  CPID {o.get('cpid')}  "
                        f"release bytes {o.get('release', '?')}  "
                        f"journal {o.get('_journal', '?')}")
                out.append(RECOVERY_SENTENCE)
        out.append("Journal sources:")
        out.extend("  " + ln for ln in self.sources_text.text().splitlines())
        rows = self.visible_rows()
        out.append(f"Entries shown ({len(rows)}, newest first):")
        out.extend("  " + "  ".join(r) for r in rows)
        return "\n".join(out)

    def copy_report(self) -> None:
        QApplication.clipboard().setText(self.report())

    def focus_filter(self) -> None:
        self.filter.setFocus(Qt.ShortcutFocusReason)
        self.filter.selectAll()

    def eventFilter(self, obj, event):  # noqa: N802
        if (obj is self.filter and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Escape):
            self.filter.clear()
            return True
        return super().eventFilter(obj, event)
