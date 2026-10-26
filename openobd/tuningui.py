"""
tuningui — the Tuning workspace's building blocks.

  Navigator        left pane: search, collapsible categories with counts,
                   changed-vs-stock dots, a persisted "Recent" group
  Breadcrumb       header above each sub-tab: "Category › Table", chips, note
  ElidedLabel      single-line label that elides instead of widening its
                   parent (keeps the Tuning page under 1000 px)
  CollapsiblePanel the editor's right-hand "Log overlay" panel (and the
                   Diagnostics module card's "Scan notes")
  ScalarsTree      grouped, filterable, editable scalar parameters
  ChannelTree      Log Analysis channels grouped Engine / Fuel / Spark / ...
  CursorReadout    each plotted channel's value at the chart cursor
  ChipDelegate     a cell drawn as a chip (scalars Δ, Diagnostics status)
  make_toolbar     the 36 px workspace toolbar (Tuning and Diagnostics)

Every colour, height and spacing comes from openobd.theme; the widgets set
objectNames / the dynamic "chip" property and the Tuning root carries
theme.tuning_qss(). MainWindow (app.py) wires these to the calibration, the
undo stack and the log; nothing here mutates a Calibration on its own.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from PySide6.QtCore import (
    QEvent, QRectF, QSize, QSortFilterProxyModel, Qt, QTimer,
    Signal,
)
from PySide6.QtGui import (
    QFont, QPainter, QPen, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QSizePolicy, QStyle, QStyledItemDelegate, QTableView, QTableWidget, QTableWidgetItem,
    QToolBar, QToolButton, QTreeView, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from . import theme
from .model import clean_unit

#: QSettings key holding the navigator's recently opened tables (JSON list:
#: an INI-backed QSettings returns a one-element list as a bare string).
RECENT_KEY = "tuning/recent"
RECENT_MAX = 8
RECENT_GROUP = "Recent"

SEP = " › "


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_chip(label: QLabel, kind: str) -> None:
    """kind: neutral | danger | warning | accent | muted | ok
    (theme.chip_qss)."""
    if label.property("chip") != kind:
        label.setProperty("chip", kind)
        repolish(label)


def make_toolbar(object_name: str = "tuningToolbar") -> QToolBar:
    """The 36 px toolbar every workspace sub-tab uses (theme.toolbar_qss).
    Actions that do not fit go to the toolbar's overflow menu, so the
    toolbar never sets the page's minimum width."""
    tb = QToolBar()
    tb.setObjectName(object_name)
    tb.setMovable(False)
    tb.setFloatable(False)
    tb.setIconSize(QSize(theme.ICON_SIZE, theme.ICON_SIZE))
    tb.setFixedHeight(theme.TOOLBAR_HEIGHT)
    tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    return tb


def table_category(table) -> str:
    return (table.category or "").split("/")[0] or "Uncategorized"


def table_label(table) -> str:
    """The navigator's short name for a table."""
    return table.name.replace("WOT Shift Speed — ", "")


def fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.4g}" if abs(v) < 1e5 else f"{v:.6g}"


def format_selection_stats(stats: dict, unit: str = "") -> str:
    """The editor's selection readout: 'n cells · min · max · mean · Δ vs
    stock n changed' (editops.selection_stats input); 'no stock baseline'
    in place of the Δ count when changed is None (nothing to compare to)."""
    n = stats.get("n", 0)
    if not n:
        return "No selection"
    u = clean_unit(unit)
    sfx = f" {u}" if u else ""
    return (f"{n} cell{'s' if n != 1 else ''}  ·  min {fmt_num(stats['min'])}"
            f"{sfx}  ·  max {fmt_num(stats['max'])}{sfx}  ·  mean "
            f"{fmt_num(stats['mean'])}{sfx}  ·  "
            + ("no stock baseline" if stats.get("changed") is None
               else f"Δ vs stock {stats['changed']} changed"))


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
class ElidedLabel(QLabel):
    """One line of text that elides (…) to the width it is given instead of
    demanding its full text width as a minimum. The full text is always in
    the tooltip and in full_text()."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt override)
        self._full = text or ""
        super().setText(self._full)
        self.setToolTip(self._full)

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(16, super().minimumSizeHint().height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self._full, Qt.ElideRight,
                                             rect.width())
        self.style().drawItemText(p, rect, int(self.alignment()),
                                  self.palette(), self.isEnabled(), text,
                                  self.foregroundRole())
        p.end()


# --------------------------------------------------------------------------- #
# Breadcrumb header
# --------------------------------------------------------------------------- #
class Breadcrumb(QFrame):
    """'Category › Table' in bold, chips on the right, the note muted below.
    The same fixed height on every Tuning / Diagnostics sub-tab.

    elastic_chips: the chip row may shrink below its natural width (chips
    clip on a very narrow window) instead of setting the page's minimum
    width. Diagnostics uses it: its chips spell out "not examined" per kind
    and must never be shortened to fit."""

    def __init__(self, parent: Optional[QWidget] = None,
                 elastic_chips: bool = False):
        super().__init__(parent)
        self.setObjectName("breadcrumb")
        self.setFixedHeight(theme.BREADCRUMB_HEIGHT)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACING["md"], theme.SPACING["sm"],
                               theme.SPACING["md"], theme.SPACING["sm"])
        lay.setSpacing(theme.SPACING["xs"])
        row = QHBoxLayout()
        row.setSpacing(theme.SPACING["sm"])
        self.title = ElidedLabel()
        self.title.setObjectName("breadcrumbTitle")
        row.addWidget(self.title, 1)
        self._chip_row = QHBoxLayout()
        self._chip_row.setSpacing(theme.SPACING["sm"])
        if elastic_chips:
            box = QWidget()
            self._chip_row.setContentsMargins(0, 0, 0, 0)
            box.setLayout(self._chip_row)
            box.setMinimumWidth(1)
            row.addWidget(box)
        else:
            row.addLayout(self._chip_row)
        lay.addLayout(row)
        self.note = ElidedLabel()
        self.note.setObjectName("breadcrumbNote")
        lay.addWidget(self.note)
        self._chips: list[QLabel] = []

    def set_path(self, parts: Iterable[str]) -> None:
        self.title.setText(SEP.join(p for p in parts if p))

    def path_text(self) -> str:
        return self.title.full_text()

    def set_note(self, text: str) -> None:
        self.note.setText(" ".join((text or "").split()))

    def set_chips(self, chips: Iterable[tuple[str, str]]) -> None:
        """[(text, kind)], kind = neutral | danger | accent."""
        chips = list(chips)
        while len(self._chips) < len(chips):
            lab = QLabel()
            lab.setProperty("chip", "neutral")
            self._chip_row.addWidget(lab)
            self._chips.append(lab)
        for lab, (text, kind) in zip(self._chips, chips):
            lab.setText(text)
            set_chip(lab, kind)
            lab.setVisible(True)
        for lab in self._chips[len(chips):]:
            lab.setVisible(False)

    def chips(self) -> list[tuple[str, str]]:
        """Visible chips as (text, kind) - what the user sees."""
        return [(lab.text(), str(lab.property("chip")))
                for lab in self._chips if not lab.isHidden()]


# --------------------------------------------------------------------------- #
# Navigator
# --------------------------------------------------------------------------- #
class Navigator(QWidget):
    """Search + category tree of tables. Emits table_activated(name) when
    the user opens a table (click, Enter, or Enter in the search field)."""

    table_activated = Signal(str)

    def __init__(self, tables, settings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._tables = list(tables)
        self._by_name = {t.name: t for t in self._tables}
        self._settings = settings
        self._dot = theme.changed_dot_icon()
        self._blank = theme.blank_icon()
        self._no_stock = theme.no_stock_icon()
        self._needle = ""

        lay = QVBoxLayout(self)
        m = theme.SPACING["sm"]
        lay.setContentsMargins(m, m, 0, m)
        lay.setSpacing(m)
        self.search = QLineEdit()
        self.search.setObjectName("navSearch")
        self.search.setPlaceholderText("Search tables (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Matches table name, category and note. "
                               "Esc clears, Down moves into the list, Enter "
                               "opens the first match.")
        self.search.textChanged.connect(self.set_filter)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setObjectName("navigatorTree")
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(theme.SPACING["lg"])
        self.tree.setIconSize(QSize(10, 10))
        self.tree.setMinimumWidth(180)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemClicked.connect(self._on_item)
        self.tree.itemActivated.connect(self._on_item)
        lay.addWidget(self.tree, 1)

        self._recent_root = QTreeWidgetItem([RECENT_GROUP])
        self._recent_root.setData(0, Qt.UserRole + 1, "group")
        self._style_group(self._recent_root)
        self.tree.addTopLevelItem(self._recent_root)
        self._cats: dict[str, QTreeWidgetItem] = {}
        self._cat_total: dict[str, int] = {}
        self._leaves: dict[str, list[QTreeWidgetItem]] = {}
        for t in self._tables:
            cat = table_category(t)
            if cat not in self._cats:
                node = QTreeWidgetItem([cat])
                node.setData(0, Qt.UserRole + 1, "group")
                self._style_group(node)
                self.tree.addTopLevelItem(node)
                self._cats[cat] = node
                self._cat_total[cat] = 0
            self._cats[cat].addChild(self._make_leaf(t))
            self._cat_total[cat] += 1
        self._recent: list[str] = self._load_recent()
        self._rebuild_pending = False   # a deferred Recent rebuild is queued
        self._rebuild_recent()
        self._relabel_groups()
        self._recent_root.setExpanded(True)

    # -- construction ------------------------------------------------------ #
    @staticmethod
    def _style_group(item: QTreeWidgetItem) -> None:
        f = QFont(item.font(0))
        f.setBold(True)
        item.setFont(0, f)
        item.setFlags(Qt.ItemIsEnabled)

    def _make_leaf(self, t) -> QTreeWidgetItem:
        leaf = QTreeWidgetItem([table_label(t)])
        leaf.setData(0, Qt.UserRole, t.name)
        tip = t.name + (f"\n{t.note}" if t.note else "")
        if self.changed_state(t.name) == self.NO_STOCK:
            tip += "\nNo stock baseline: changes cannot be tracked"
        leaf.setToolTip(0, tip)
        self._apply_state(leaf, self.changed_state(t.name))
        self._leaves.setdefault(t.name, []).append(leaf)
        return leaf

    # -- recent ------------------------------------------------------------- #
    def _load_recent(self) -> list[str]:
        raw = self._settings.value(RECENT_KEY, "[]")
        try:
            names = json.loads(raw) if isinstance(raw, str) else []
        except ValueError:
            names = []
        if not isinstance(names, list):
            names = []
        out: list[str] = []
        for n in names:
            if isinstance(n, str) and n in self._by_name and n not in out:
                out.append(n)
        return out[:RECENT_MAX]

    def recent(self) -> list[str]:
        return list(self._recent)

    def push_recent(self, name: str, defer: bool = False) -> None:
        """Move name to the front of Recent. defer=True (the click path)
        rebuilds the Recent items on the next event-loop turn: rebuilding
        deletes the Recent leaves, and the clicked one may be the item Qt
        is still delivering itemClicked / itemActivated for."""
        if name not in self._by_name:
            return
        self._recent = [name] + [n for n in self._recent if n != name]
        self._recent = self._recent[:RECENT_MAX]
        self._settings.setValue(RECENT_KEY, json.dumps(self._recent))
        self._settings.sync()
        if not defer:
            self._rebuild_pending = False
            self._rebuild_recent()
        elif not self._rebuild_pending:
            self._rebuild_pending = True
            QTimer.singleShot(0, self, self._deferred_rebuild)

    def _deferred_rebuild(self) -> None:
        if self._rebuild_pending:
            self._rebuild_pending = False
            self._rebuild_recent()

    def _rebuild_recent(self) -> None:
        for i in reversed(range(self._recent_root.childCount())):
            leaf = self._recent_root.takeChild(i)
            name = leaf.data(0, Qt.UserRole)
            if name in self._leaves:
                self._leaves[name] = [x for x in self._leaves[name]
                                      if x is not leaf]
        for name in self._recent:
            self._recent_root.addChild(self._make_leaf(self._by_name[name]))
        self._relabel_groups()

    # -- filter ------------------------------------------------------------- #
    @staticmethod
    def matches(table, needle: str) -> bool:
        if not needle:
            return True
        hay = " ".join((table.name, table.category or "", table.note or ""))
        return needle in hay.lower()

    def set_filter(self, text: str) -> None:
        self._needle = (text or "").strip().lower()
        for cat, node in self._cats.items():
            for j in range(node.childCount()):
                leaf = node.child(j)
                t = self._by_name[leaf.data(0, Qt.UserRole)]
                leaf.setHidden(not self.matches(t, self._needle))
        # Recent is a shortcut list, not search results.
        self._recent_root.setHidden(bool(self._needle)
                                    or not self._recent)
        self._relabel_groups()
        if self._needle:
            for node in self._cats.values():
                node.setExpanded(not node.isHidden())

    def _relabel_groups(self) -> None:
        for cat, node in self._cats.items():
            n = sum(not node.child(j).isHidden()
                    for j in range(node.childCount()))
            node.setText(0, f"{cat} ({n})")
            node.setHidden(n == 0)
        n = self._recent_root.childCount()
        self._recent_root.setText(0, f"{RECENT_GROUP} ({n})")
        self._recent_root.setHidden(bool(self._needle) or n == 0)

    def category_label(self, cat: str) -> str:
        return self._cats[cat].text(0)

    def visible_table_names(self) -> list[str]:
        """Tables the category groups currently show (search results)."""
        out = []
        for node in self._cats.values():
            if node.isHidden():
                continue
            for j in range(node.childCount()):
                leaf = node.child(j)
                if not leaf.isHidden():
                    out.append(leaf.data(0, Qt.UserRole))
        return out

    # -- changed dots ------------------------------------------------------- #
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    NO_STOCK = "no-stock"

    def changed_state(self, name: str) -> str:
        """Three-valued verdict: CHANGED / UNCHANGED vs stock, or NO_STOCK
        when the table has no baseline (unknown, never 'unchanged')."""
        t = self._by_name.get(name)
        if t is None or t.stock_values is None:
            return self.NO_STOCK
        return self.CHANGED if self.is_changed(name) else self.UNCHANGED

    def is_changed(self, name: str) -> bool:
        t = self._by_name.get(name)
        if t is None or t.stock_values is None:
            return False
        from .editops import changed_cells
        return bool(changed_cells(t))

    STATE_ROLE = Qt.UserRole + 2

    def _apply_state(self, leaf: QTreeWidgetItem, state: str) -> None:
        """One mapping from changed_state to the leaf icon, used by both
        _make_leaf and refresh_changed: dot / blank / hollow muted ring."""
        icon = {self.CHANGED: self._dot, self.UNCHANGED: self._blank,
                self.NO_STOCK: self._no_stock}[state]
        leaf.setIcon(0, icon)
        leaf.setData(0, self.STATE_ROLE, state)

    def leaf_state(self, name: str) -> Optional[str]:
        leaves = self._leaves.get(name) or []
        return leaves[0].data(0, self.STATE_ROLE) if leaves else None

    def has_dot(self, name: str) -> bool:
        return self.leaf_state(name) == self.CHANGED

    def refresh_changed(self, name: Optional[str] = None) -> None:
        names = [name] if name else list(self._leaves)
        for n in names:
            state = self.changed_state(n)
            for leaf in self._leaves.get(n, []):
                self._apply_state(leaf, state)

    # -- selection / activation -------------------------------------------- #
    def select_name(self, name: str) -> None:
        """Highlight the table's category leaf (programmatic; no signal)."""
        t = self._by_name.get(name)
        if t is None:
            return
        node = self._cats[table_category(t)]
        for j in range(node.childCount()):
            leaf = node.child(j)
            if leaf.data(0, Qt.UserRole) == name:
                node.setExpanded(True)
                self.tree.setCurrentItem(leaf)
                self.tree.scrollToItem(leaf)
                return

    def _on_item(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        name = item.data(0, Qt.UserRole)
        if name:
            self.push_recent(name, defer=True)
            self.table_activated.emit(name)
            # push_recent rebuilt Recent: keep the highlight on a live item.
            self.select_name(name)

    def _first_visible_leaf(self) -> Optional[QTreeWidgetItem]:
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            if node.isHidden():
                continue
            for j in range(node.childCount()):
                if not node.child(j).isHidden():
                    return node.child(j)
        return None

    def focus_search(self) -> None:
        self.search.setFocus(Qt.ShortcutFocusReason)
        self.search.selectAll()

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.search and event.type() == QEvent.KeyPress:
            key = event.key()
            if key == Qt.Key_Escape:
                self.search.clear()
                return True
            if key == Qt.Key_Down:
                leaf = self._first_visible_leaf()
                if leaf is not None:
                    leaf.parent().setExpanded(True)
                    self.tree.setCurrentItem(leaf)
                self.tree.setFocus(Qt.OtherFocusReason)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                leaf = self._first_visible_leaf()
                if leaf is not None:
                    self._on_item(leaf)
                return True
        return super().eventFilter(obj, event)


# --------------------------------------------------------------------------- #
# Collapsible side panel
# --------------------------------------------------------------------------- #
class CollapsiblePanel(QWidget):
    """A QToolButton with a disclosure arrow over a QFrame body; collapsed,
    only the button takes room."""

    toggled = Signal(bool)

    def __init__(self, title: str, body: QWidget, expanded: bool = False,
                 body_width: Optional[int] = 230,
                 parent: Optional[QWidget] = None,
                 align: Qt.AlignmentFlag = Qt.AlignRight):
        """body_width None lets the body follow the panel's width (an inline
        section rather than a fixed side panel); align places the toggle."""
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACING["sm"])
        self.toggle = QToolButton()
        self.toggle.setObjectName("overlayToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setToolTip(f"Show / hide {title}")
        self.toggle.toggled.connect(self._on_toggle)
        lay.addWidget(self.toggle, 0, align)
        self.frame = QFrame()
        self.frame.setObjectName("overlayPanel")
        if body_width is not None:
            self.frame.setFixedWidth(body_width)
        fl = QVBoxLayout(self.frame)
        fl.setContentsMargins(theme.SPACING["md"], theme.SPACING["md"],
                              theme.SPACING["md"], theme.SPACING["md"])
        fl.addWidget(body)
        lay.addWidget(self.frame)
        lay.addStretch(1)
        self.toggle.setChecked(expanded)
        self._on_toggle(expanded, emit=False)

    def _on_toggle(self, on: bool, emit: bool = True) -> None:
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.LeftArrow)
        self.frame.setVisible(on)
        if emit:
            self.toggled.emit(on)

    def is_expanded(self) -> bool:
        return self.toggle.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.toggle.setChecked(on)


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #
#: Item-data role holding a cell's chip kind for ChipDelegate (absent =
#: danger, the scalars Δ).
CHIP_KIND_ROLE = Qt.UserRole + 40

#: chip kind -> (border, fill, text) colour tokens; the same pairs as
#: theme.chip_qss so a painted chip matches a QLabel chip.
CHIP_PAINT = {
    "danger": ("danger", "danger-surface", "danger-text"),
    "warning": ("warning", "warning-surface", "warning-text"),
    "ok": ("ok", "ok-surface", "ok-text"),
    "muted": ("border", "surface-sunken", "text-muted"),
    "neutral": ("border", "surface-raised", "text"),
    "accent": ("accent", "surface-raised", "link"),
}


class ChipDelegate(QStyledItemDelegate):
    """Draws a non-empty cell as a rounded chip: danger by default (the
    scalars Δ), or the kind in the cell's CHIP_KIND_ROLE. A muted chip has a
    dashed border, like the QLabel chip."""

    def paint(self, painter, option, index):
        text = index.data(Qt.DisplayRole)
        if not text:
            return super().paint(painter, option, index)
        # Selection / hover background first, then the chip on top.
        self.initStyleOption(option, index)
        option.text = ""
        option.widget.style().drawControl(QStyle.CE_ItemViewItem, option,
                                          painter, option.widget)
        fm = option.fontMetrics
        pad = theme.SPACING["md"]
        w = min(option.rect.width() - 4, fm.horizontalAdvance(text) + 2 * pad)
        h = min(option.rect.height() - 4, fm.height() + 2)
        r = QRectF(option.rect.x() + 2, option.rect.center().y() - h / 2,
                   w, h)
        kind = index.data(CHIP_KIND_ROLE) or "danger"
        border, fill, fg = CHIP_PAINT.get(kind, CHIP_PAINT["danger"])
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(theme.qcolor(border))
        if kind == "muted":
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(theme.qcolor(fill))
        painter.drawRoundedRect(r, theme.RADIUS["md"], theme.RADIUS["md"])
        font = index.data(Qt.FontRole)
        if font is not None:
            painter.setFont(font)
        painter.setPen(theme.qcolor(fg))
        painter.drawText(r, int(Qt.AlignCenter), text)
        painter.restore()


class _ScalarFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.needle = ""
        self.category: Optional[str] = None
        self.setRecursiveFilteringEnabled(True)

    def set_criteria(self, needle: Optional[str] = None,
                     category: Optional[str] = None,
                     set_category: bool = False) -> None:
        """Change the needle and/or category and re-filter (Qt 6.10+
        begin/endFilterChange; invalidateFilter is deprecated)."""
        self.beginFilterChange()
        if needle is not None:
            self.needle = needle
        if set_category:
            self.category = category or None
        self.endFilterChange()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        src = self.sourceModel()
        idx = src.index(row, 0, parent)
        kind = idx.data(Qt.UserRole + 1)
        if kind == "group":
            # Groups show when a child matches (recursive filtering) and
            # only if they are the chosen category.
            return False
        if self.category and idx.data(Qt.UserRole + 3) != self.category:
            return False
        if not self.needle:
            return True
        return self.needle in (idx.data(Qt.UserRole + 2) or "")


class ScalarsTree(QWidget):
    """Scalars grouped by category (collapsible), columns Parameter / Value
    (editable) / Unit / Stock / Δ. Emits edit_requested(index, text) when
    the user commits a Value cell; the window validates and applies it."""

    edit_requested = Signal(int, str)
    COLUMNS = ["Parameter", "Value", "Unit", "Stock", "Δ"]

    def __init__(self, scalars, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._scalars = scalars
        self._rows: dict[int, list[QStandardItem]] = {}
        self._updating = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.model = QStandardItemModel(0, len(self.COLUMNS), self)
        self.model.setHorizontalHeaderLabels(self.COLUMNS)
        self.proxy = _ScalarFilter(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTreeView()
        self.view.setObjectName("scalarsTree")
        self.view.setModel(self.proxy)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(False)
        self.view.setIndentation(theme.SPACING["lg"])
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked
                                  | QAbstractItemView.EditKeyPressed
                                  | QAbstractItemView.AnyKeyPressed)
        self.view.setItemDelegateForColumn(4, ChipDelegate(self.view))
        hdr = self.view.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in ((1, 90), (2, 70), (3, 80), (4, 80)):
            hdr.setSectionResizeMode(col, QHeaderView.Interactive)
            hdr.resizeSection(col, w)
        lay.addWidget(self.view)
        self._groups: dict[str, QStandardItem] = {}
        self._build()
        self.model.itemChanged.connect(self._on_item_changed)
        self.view.expandAll()

    def _build(self) -> None:
        for i, s in enumerate(self._scalars):
            cat = s.category or "Uncategorized"
            if cat not in self._groups:
                g = QStandardItem(cat)
                g.setData("group", Qt.UserRole + 1)
                g.setEditable(False)
                f = g.font()
                f.setBold(True)
                g.setFont(f)
                filler = [QStandardItem("") for _ in self.COLUMNS[1:]]
                for it in filler:
                    it.setEditable(False)
                self.model.appendRow([g] + filler)
                self._groups[cat] = g
            row = [QStandardItem("") for _ in self.COLUMNS]
            for col, it in enumerate(row):
                it.setEditable(col == 1)
            row[0].setData("scalar", Qt.UserRole + 1)
            row[0].setData(" ".join((s.name, cat, s.note or "")).lower(),
                           Qt.UserRole + 2)
            row[0].setData(cat, Qt.UserRole + 3)
            row[1].setData(i, Qt.UserRole)
            row[1].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row[3].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._groups[cat].appendRow(row)
            self._rows[i] = row
            self.refresh(i)
        for cat, g in self._groups.items():
            g.setText(f"{cat} ({g.rowCount()})")

    def categories(self) -> list[str]:
        return list(self._groups)

    def refresh(self, i: int) -> None:
        """Re-render scalar i's row from the Scalar."""
        s = self._scalars[i]
        row = self._rows[i]
        self._updating = True
        try:
            row[0].setText(s.name)
            if s.note:
                row[0].setToolTip(s.note)
            row[1].setText(f"{s.value:g}")
            row[2].setText(clean_unit(s.unit))
            row[3].setText("" if s.stock_value is None
                           else f"{s.stock_value:g}")
            delta = ""
            if s.stock_value is not None:
                d = s.value - s.stock_value
                delta = f"{d:+g}" if d else ""
            row[4].setText(delta)
            f = row[0].font()
            f.setBold(bool(delta))
            row[0].setFont(f)
        finally:
            self._updating = False

    def value_text(self, i: int) -> str:
        return self._rows[i][1].text()

    def delta_text(self, i: int) -> str:
        return self._rows[i][4].text()

    def value_item(self, i: int) -> QStandardItem:
        return self._rows[i][1]

    def _on_item_changed(self, item: QStandardItem) -> None:
        if self._updating or item.column() != 1:
            return
        i = item.data(Qt.UserRole)
        if i is None:
            return
        self.edit_requested.emit(int(i), item.text())

    def set_search(self, text: str) -> None:
        self.proxy.set_criteria(needle=(text or "").strip().lower())
        self.view.expandAll()

    def set_category(self, cat: Optional[str]) -> None:
        self.proxy.set_criteria(category=cat, set_category=True)
        self.view.expandAll()

    def visible_scalar_count(self) -> int:
        n = 0
        for g in range(self.proxy.rowCount()):
            n += self.proxy.rowCount(self.proxy.index(g, 0))
        return n


# --------------------------------------------------------------------------- #
# Log Analysis: channels + cursor readout
# --------------------------------------------------------------------------- #
#: Canonical channel -> group in the Log Analysis channel tree.
CHANNEL_GROUPS: dict[str, str] = {
    "rpm": "Engine", "load": "Engine", "abs_load": "Engine", "maf": "Engine",
    "map": "Engine", "tps": "Engine", "rel_tps": "Engine", "app": "Engine",
    "app_e": "Engine", "app_f": "Engine", "pedal": "Engine",
    "cmd_throttle": "Engine", "run_time": "Engine",
    "stft": "Fuel", "ltft": "Fuel", "stft_b2": "Fuel", "ltft_b2": "Fuel",
    "eq_ratio": "Fuel", "afr": "Fuel", "fuel_press": "Fuel",
    "fuel_level": "Fuel", "fuel_system": "Fuel", "ethanol": "Fuel",
    "spark": "Spark", "knock_retard": "Spark",
    "vss": "Trans", "gear": "Trans", "tft": "Trans", "tcc_slip": "Trans",
    "ect": "Sensors", "iat": "Sensors", "baro": "Sensors",
    "ambient": "Sensors", "voltage": "Sensors", "o2": "Sensors",
    "o2_b1s1": "Sensors", "o2_b1s2": "Sensors", "o2_b2s1": "Sensors",
    "o2_b2s2": "Sensors", "cat_b1s1": "Sensors", "cat_b2s1": "Sensors",
    # truck-mcp drive-log store names (Live Data tile groups)
    "engine_load": "Engine", "intake_map": "Engine", "throttle_pos": "Engine",
    "stft_bank1": "Fuel", "ltft_bank1": "Fuel", "stft_bank2": "Fuel",
    "ltft_bank2": "Fuel", "vehicle_speed": "Trans",
    "trans_fluid_temp": "Trans", "coolant_temp": "Sensors",
    "intake_air_temp": "Sensors", "control_voltage": "Sensors",
}
GROUP_ORDER = ["Engine", "Fuel", "Spark", "Trans", "Sensors", "Other"]


def channel_group(key: str) -> str:
    return CHANNEL_GROUPS.get(key, "Other")


class ChannelTree(QTreeWidget):
    """Checkable channels grouped Engine / Fuel / Spark / Trans / Sensors /
    Other; a group's box (auto-tristate) checks all its channels. changed
    is emitted once per batch of check-state changes."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("channelTree")
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setIndentation(theme.SPACING["lg"])
        self.setMinimumWidth(150)
        self._order: list[str] = []
        self._items: dict[str, QTreeWidgetItem] = {}
        self._loading = False
        self._pending = False
        self.itemChanged.connect(self._on_changed)

    def populate(self, keys: list[str], checked: Iterable[str]) -> None:
        want = set(checked)
        self._loading = True
        try:
            self.clear()
            self._order = list(keys)
            self._items = {}
            groups: dict[str, QTreeWidgetItem] = {}
            for name in GROUP_ORDER:
                members = [k for k in keys if channel_group(k) == name]
                if not members:
                    continue
                g = QTreeWidgetItem([f"{name} ({len(members)})"])
                g.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
                           | Qt.ItemIsAutoTristate)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                self.addTopLevelItem(g)
                groups[name] = g
                for k in members:
                    it = QTreeWidgetItem([k])
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable
                                | Qt.ItemIsSelectable)
                    it.setCheckState(0, Qt.Checked if k in want
                                     else Qt.Unchecked)
                    g.addChild(it)
                    self._items[k] = it
            self.expandAll()
        finally:
            self._loading = False

    def channels(self) -> list[str]:
        return list(self._order)

    def item(self, key: str) -> QTreeWidgetItem:
        return self._items[key]

    def group_item(self, name: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.topLevelItemCount()):
            g = self.topLevelItem(i)
            if g.text(0).split(" (")[0] == name:
                return g
        return None

    def checked_channels(self) -> list[str]:
        return [k for k in self._order
                if k in self._items
                and self._items[k].checkState(0) == Qt.Checked]

    def set_checked(self, key: str, on: bool) -> None:
        self._items[key].setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def _on_changed(self, _item, _col) -> None:
        if self._loading or self._pending:
            return
        # A group toggle changes every child: coalesce into one rebuild.
        self._pending = True
        QTimer.singleShot(0, self._flush)

    def _flush(self) -> None:
        self._pending = False
        self.changed.emit()


class CursorReadout(QTableWidget):
    """Each plotted channel's value at the chart cursor; first row is the
    cursor time."""

    TIME_ROW = "t"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(0, 2, parent)
        self.setObjectName("cursorReadout")
        self.setHorizontalHeaderLabels(["Cursor", "Value"])
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(theme.ROW_HEIGHT)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumWidth(150)
        self._values: dict[str, str] = {}

    def set_channels(self, keys: list[str]) -> None:
        """Rows for the plotted channels, values blank until the cursor
        moves over the chart."""
        self.show_values(None, [(k, None) for k in keys])

    def show_values(self, t: Optional[float],
                    rows: list[tuple[str, Optional[float]]]) -> None:
        all_rows = [(self.TIME_ROW,
                     None if t is None else f"{t:.2f} s")]
        all_rows += [(k, None if v is None else f"{v:g}") for k, v in rows]
        self.setRowCount(len(all_rows))
        self._values = {}
        for r, (k, text) in enumerate(all_rows):
            shown = text if text is not None else "—"
            self._values[k] = shown
            ki = QTableWidgetItem(k)
            vi = QTableWidgetItem(shown)
            vi.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if text is None:
                ki.setForeground(theme.qcolor("text-muted"))
                vi.setForeground(theme.qcolor("text-muted"))
            self.setItem(r, 0, ki)
            self.setItem(r, 1, vi)

    def value(self, key: str) -> Optional[str]:
        return self._values.get(key)

    def keys(self) -> list[str]:
        return list(self._values)


def empty_state_label(text: str) -> QLabel:
    lab = QLabel(text)
    # Callers append device / GT error text later via setText: never let a
    # reply's markup render as rich text.
    lab.setTextFormat(Qt.PlainText)
    lab.setObjectName("emptyState")
    lab.setAlignment(Qt.AlignCenter)
    lab.setWordWrap(True)
    return lab


# --------------------------------------------------------------------------- #
# Editor grid
# --------------------------------------------------------------------------- #
class CalTableView(QTableView):
    """The calibration grid. Its corner cell shows the axis names ('Y \\ X')
    in a label laid over the corner button (which keeps its select-all
    click: the label ignores the mouse)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.corner_label = QLabel(self)
        self.corner_label.setObjectName("cornerLabel")
        self.corner_label.setAlignment(Qt.AlignCenter)
        self.corner_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._corner = ""
        self.verticalHeader().setDefaultAlignment(Qt.AlignRight
                                                  | Qt.AlignVCenter)

    def set_corner_text(self, text: str) -> None:
        self._corner = text or ""
        self.corner_label.setToolTip(self._corner)
        self._place_corner()

    def corner_text(self) -> str:
        return self._corner

    def updateGeometries(self) -> None:  # noqa: N802 (Qt override)
        super().updateGeometries()
        self._place_corner()

    def _place_corner(self) -> None:
        vh, hh = self.verticalHeader(), self.horizontalHeader()
        fw = self.frameWidth()
        w = vh.width() if not vh.isHidden() else 0
        h = hh.height() if not hh.isHidden() else 0
        self.corner_label.setGeometry(fw, fw, w, h)
        self.corner_label.setText(self.corner_label.fontMetrics().elidedText(
            self._corner, Qt.ElideRight, max(0, w - 4)))
        self.corner_label.raise_()
