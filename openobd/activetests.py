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

import heapq
import math
import time
from datetime import datetime
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
#: Item data role (column 0) holding a row's identity, so a rebuild can
#: put the selection back on the same journal entries.
ROW_KEY_ROLE = Qt.UserRole + 1
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
    """A finite epoch, or None. json.loads accepts NaN / Infinity and huge
    integer literals; none of them can be ordered, so none is a timestamp."""
    try:
        value = float(at)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


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
        # Render caches (R1 / SC-10): the 3 s poll re-reads the journal,
        # but the table is rebuilt only when what it shows has changed.
        self._sel_cache: Optional[tuple] = None     # (inputs, snapshot, result)
        self._table_key: Optional[tuple] = None     # what the tree now shows
        self._shown: tuple[int, int] = (0, 0)       # (rows listed, matching)
        self._sources_body = ""                     # last good sources listing

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
            if self._state is None:      # nothing to keep: say so, not blank
                self._restore_last_good()
        else:
            prev = (self._state, self._roots)
            self._state, self._roots, self._read_error = state, roots, None
            try:
                self._fill_table()
                self._fill_sources()
            except Exception as exc:
                # A render failure must never leave a half-built table marked
                # current: put the last good read back and say it failed.
                self._state, self._roots = prev
                self._read_error = ("could not render the journal: "
                                    + (str(exc) or type(exc).__name__))
                self._restore_last_good()
            else:
                self._as_of = datetime.now()   # only once fully rendered
        self._show_sources()
        self._refresh_chrome()

    def _restore_last_good(self) -> None:
        self._sel_cache = None
        self._table_key = None
        try:
            self._fill_table()
            self._fill_sources()
        except Exception:
            self.tree.clear()
            self._table_key = None
            self._shown = (0, 0)
            self._sources_body = ""
            self._add_note_row("Table unavailable: the last good read could "
                               "not be rendered either", "empty")

    # -- journal table --------------------------------------------------------
    def _select(self) -> tuple[list[tuple], int]:
        """(newest TABLE_LIMIT matches, number matching). Show mode and the
        filter apply to EVERY entry before the limit, so an old match is
        never hidden behind newer non-matches. Bounded selection (heapq) over
        a generator: no full copy + sort on the UI thread. Entries without a
        usable timestamp sort last (ctljournal already downgrades the verdict
        for them). Cached per (inputs, journal content)."""
        st = self._state
        if st is None:
            return [], 0
        mode = self.show_mode()
        needle = self.filter.text().strip().lower()
        cutoff = time.time() - 86400 if mode == "24h" else None
        inputs = (mode, needle, cutoff)
        snapshot = [(str(r.path), r.entries) for r in st.reads]
        cache = self._sel_cache
        if cache is not None and cache[0] == inputs and cache[1] == snapshot:
            return cache[2]
        matched = 0

        def candidates():
            nonlocal matched
            for r in st.reads:
                journal = str(r.path)
                for e in r.entries:
                    event = str(e.get("event", ""))
                    if mode == "controls" and event_kind(event) != "danger":
                        continue
                    at = _epoch(e.get("at"))
                    if cutoff is not None and (at or 0.0) < cutoff:
                        continue
                    if needle:
                        hay = " ".join((event, str(e.get("module", "")),
                                        str(e.get("cpid", "")), _detail(e)))
                        if needle not in hay.lower():
                            continue
                    matched += 1
                    yield (at is not None, at or 0.0), e, journal

        top = heapq.nlargest(TABLE_LIMIT, candidates(), key=lambda t: t[0])
        result = (top, matched)
        self._sel_cache = (inputs, snapshot, result)
        return result

    @staticmethod
    def _rows(top: list[tuple]) -> list[tuple]:
        """Display rows: (when, event, module, cpid, detail, full, journal).
        The journal is the file the entry was read from, assigned here - an
        entry's own `_journal` key cannot forge its provenance."""
        rows = []
        last_day = None
        for _key, e, journal in top:
            at = _epoch(e.get("at"))
            dt = None
            if at is not None:
                try:
                    dt = datetime.fromtimestamp(at)
                except (OverflowError, OSError, ValueError):
                    dt = None
            if dt is None:
                when = full = str(e.get("at", ""))
            else:
                full = dt.strftime("%Y-%m-%d %H:%M:%S")
                when = (full if dt.date() != last_day
                        else dt.strftime("%H:%M:%S"))
                last_day = dt.date()
            rows.append((when, str(e.get("event", "")),
                         str(e.get("module", "")), str(e.get("cpid", "")),
                         _detail(e), full, journal))
        return rows

    def _empty_text(self, matched: int) -> Optional[str]:
        if matched:
            return None
        st = self._state
        if st is None:
            return ("No successful read yet" if self._read_error is not None
                    else "No journal entries")
        if not any(r.entries for r in st.reads):
            return "No journal entries"
        return "No matches for the current filter / Show mode"

    def _add_note_row(self, text: str, role: str) -> None:
        item = QTreeWidgetItem([text])
        item.setFlags(Qt.NoItemFlags)
        item.setForeground(0, theme.qcolor("text-muted"))
        item.setData(0, Qt.UserRole, role)
        self.tree.addTopLevelItem(item)
        item.setFirstColumnSpanned(True)

    def _fill_table(self) -> None:
        top, matched = self._select()
        rows = self._rows(top)
        empty = self._empty_text(matched)
        key = (self.show_mode(), self.filter.text().strip().lower(),
               tuple(rows), matched, empty)
        if key == self._table_key:
            return          # unchanged: keep items, selection and scroll
        bar = self.tree.verticalScrollBar()
        scroll = bar.value()
        selected = {it.data(0, ROW_KEY_ROLE)
                    for it in self.tree.selectedItems()}
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            if empty is not None:
                self._add_note_row(empty, "empty")
            for row in rows:
                when, event, module, cpid, detail, full, journal = row
                item = QTreeWidgetItem([when, event, module, cpid, detail])
                kind = event_kind(event)
                item.setForeground(COL_EVENT,
                                   theme.qcolor(_EVENT_TOKENS[kind]))
                item.setData(COL_EVENT, Qt.UserRole, kind)
                row_key = "\x1f".join(row[1:])
                item.setData(0, ROW_KEY_ROLE, row_key)
                item.setToolTip(COL_WHEN, plain_tooltip(full))
                item.setToolTip(COL_DETAIL, plain_tooltip(
                    f"{detail}\njournal: {journal}"))
                self.tree.addTopLevelItem(item)
                if row_key in selected:
                    item.setSelected(True)
            if matched > len(rows):
                self._add_note_row(
                    f"showing {len(rows)} of {matched} matching entries "
                    "(newest first); narrow the filter or Show mode to list "
                    "older ones", "info")
        finally:
            self.tree.setUpdatesEnabled(True)
        self.tree.doItemsLayout()
        bar.setValue(scroll)
        self._table_key = key
        self._shown = (len(rows), matched)

    def _apply_filter(self, *_a) -> None:
        """Display-only: changes which entries are listed, never what was
        read or the verdict."""
        self._fill_table()

    def visible_rows(self) -> list[list[str]]:
        rows = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.isHidden() or item.data(0, Qt.UserRole) in ("empty",
                                                                 "info"):
                continue
            rows.append([item.text(c) for c in range(len(COLUMNS))])
        return rows

    def shown_counts(self) -> tuple[int, int]:
        """(entries listed, entries matching Show mode + filter)."""
        return self._shown

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
        self._sources_body = "\n".join(lines) or "No data roots searched."

    def _show_sources(self) -> None:
        body = self._sources_body
        if self._read_error is not None:
            head = (f"last good read {self._as_of.strftime('%H:%M:%S')} "
                    "(not current)" if self._as_of is not None
                    else "no successful read yet")
            body = f"{head}\n{body}" if body else head
        if self.sources_text.text() != body:
            self.sources_text.setText(body)

    # -- report / search --------------------------------------------------------
    def report(self) -> str:
        out = ["OpenOBD Active Tests - control journal report (read-only)"]
        if self._read_error is not None:
            out.append(f"Read failed: {self._read_error}")
        good = (self._as_of.strftime("%Y-%m-%d %H:%M:%S") if self._as_of
                else "never")
        out.append(f"Last successful read: {good}")
        st = self._state
        if self._read_error is not None:
            if st is not None and self._as_of is not None:
                label = VERDICT_CHIPS.get(st.verdict, (st.verdict,))[0]
                out.append(f"Verdict: read failed (last good: {label} at "
                           f"{self._as_of.strftime('%H:%M:%S')})")
            else:
                out.append("Verdict: read failed (no successful read yet)")
        elif st is not None:
            label = VERDICT_CHIPS.get(st.verdict, (st.verdict,))[0]
            out.append(f"Verdict: {label}")
        if st is not None:
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
        out.append(f"Entries shown ({len(rows)} of {self._shown[1]}, "
                   "newest first):")
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
