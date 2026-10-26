"""
Qt model + delegate for the calibration table editor.

CalTableModel wraps a calspec.Table as an editable QAbstractTableModel with:
  - a full-spectrum value heatmap (theme.HEAT_STOPS, scaled per table
    min..max) as the cell background, text colour picked for contrast
  - a diff-vs-stock mode (absolute or percent): cells show the delta on the
    diverging theme.DIFF_STOPS scale, the real value moves to the tooltip
  - changed-vs-stock cells in bold plus a corner triangle (HeatmapDelegate)
  - an optional log Overlay: count histogram tint + mean-value tooltip

Every colour comes from openobd.theme. Editing writes floats back into the
Table (or through edit_hook, so the window can wrap it in an undo command).
All Qt imports are local so the headless core (calspec/logbin/transport)
never pulls in PySide6.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QPointF
from PySide6.QtGui import QColor, QBrush, QFont, QPainter, QPolygonF
from PySide6.QtWidgets import QStyledItemDelegate

from . import theme
from .calspec import Axis, Table
from .editops import pct_delta
from .logbin import Overlay

#: Item-data role carrying "this cell differs from stock" for the delegate.
CHANGED_ROLE = int(Qt.UserRole) + 1


def clean_unit(unit: Optional[str]) -> str:
    """Units as imported from HPT exports can carry a UTF-8 BOM; strip it
    (and surrounding blanks) for display."""
    return (unit or "").replace("\ufeff", "").strip()


def axis_value_label(ax: Axis, i: int) -> str:
    """'2400 rpm' - an axis breakpoint with its unit."""
    v = ax.values[i] if i < len(ax.values) else i
    unit = clean_unit(ax.unit)
    return f"{v:g}{(' ' + unit) if unit else ''}"


def axis_name(ax: Optional[Axis], fallback: str) -> str:
    """An axis's display name: its label, else its unit, else fallback."""
    if ax is None:
        return fallback
    return (ax.label or "").strip() or clean_unit(ax.unit) or fallback


def heat_color(frac: float, alpha: int = 255) -> QColor:
    """frac in [0,1] -> the theme's full-spectrum heatmap colour."""
    return theme.qcolor_rgb(theme.heat_rgb(frac), alpha)


def text_color_for(bg: QColor) -> QColor:
    """Dark or light text, whichever reads better on bg."""
    return theme.qcolor_rgb(
        theme.contrast_text_rgb((bg.red(), bg.green(), bg.blue())))


class CalTableModel(QAbstractTableModel):
    OVERLAY_NONE = 0
    OVERLAY_COUNT = 1
    OVERLAY_MEAN = 2

    DIFF_OFF = 0
    DIFF_ABS = 1
    DIFF_PCT = 2

    def __init__(self, table: Table, shift_events: Optional[list[str]] = None):
        super().__init__()
        self.table = table
        self.shift_events = shift_events
        self.overlay: Optional[Overlay] = None
        self.overlay_mode = self.OVERLAY_NONE
        self.show_heatmap = True
        self.diff_mode = self.DIFF_OFF
        # When set, in-place edits are routed here (r, c, old, new) so the
        # window can wrap them in an undo command instead of a direct write.
        self.edit_hook = None
        # Per-table scale statistics (min/max, largest |delta|) feed every
        # cell's colour; computing them per cell made a repaint O(cells^2).
        # Cached, and dropped on every change notification. These
        # connections are made before any view connects, so the cache is
        # cleared before a view re-reads the cells.
        self._stats: Optional[dict] = None
        self.dataChanged.connect(self._drop_stats)
        self.layoutChanged.connect(self._drop_stats)
        self.modelReset.connect(self._drop_stats)

    # -- cached table statistics ------------------------------------------- #
    def _drop_stats(self, *_a) -> None:
        self._stats = None

    def _table_stats(self) -> dict:
        if self._stats is None:
            lo, hi = self.table.vmin_vmax()
            max_abs = 0.0
            max_pct = 0.0
            stock = self.table.stock_values
            if stock is not None:
                for r in range(self.table.n_rows):
                    for c in range(self.table.n_cols):
                        v, s = self.table.values[r][c], stock[r][c]
                        max_abs = max(max_abs, abs(v - s))
                        p = pct_delta(v, s)
                        if p is not None:
                            max_pct = max(max_pct, abs(p))
            self._stats = {"lo": lo, "hi": hi, "max_abs": max_abs,
                           "max_pct": max_pct}
        return self._stats

    # -- shape ------------------------------------------------------------- #
    def rowCount(self, parent=QModelIndex()) -> int:
        return self.table.n_rows

    def columnCount(self, parent=QModelIndex()) -> int:
        return self.table.n_cols

    # -- headers ----------------------------------------------------------- #
    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.TextAlignmentRole:
            if orientation == Qt.Vertical:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole:
            ax = (self.table.x_axis if orientation == Qt.Horizontal
                  else self.table.y_axis)
            return axis_name(ax, "X" if orientation == Qt.Horizontal else "Y")
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            # WOT shift tables: label columns with gear-change events.
            if self.shift_events and section < len(self.shift_events):
                return self.shift_events[section]
            return axis_value_label(self.table.x_axis, section)
        if self.table.y_axis:
            return axis_value_label(self.table.y_axis, section)
        return clean_unit(self.table.unit) or "value"

    def corner_text(self) -> str:
        """Axis names for the grid's corner cell: 'Y \\ X' (just X for a
        1-D table)."""
        x = ("Gear change" if self.shift_events
             else axis_name(self.table.x_axis, "X"))
        if self.table.y_axis is None:
            return x
        return f"{axis_name(self.table.y_axis, 'Y')} \\ {x}"

    # -- per-cell helpers --------------------------------------------------- #
    def stock(self, r: int, c: int) -> Optional[float]:
        sv = self.table.stock_values
        try:
            return sv[r][c] if sv is not None else None
        except IndexError:
            return None

    def delta_text(self, r: int, c: int) -> str:
        """The cell's diff-mode text: '+1.5' (absolute) or '+3.2%'. A table
        without stock values shows the value itself."""
        val = self.table.values[r][c]
        st = self.stock(r, c)
        if st is None:
            return f"{val:g}"
        if self.diff_mode == self.DIFF_PCT:
            p = pct_delta(val, st)
            return "n/a" if p is None else f"{p:+.1f}%"
        return f"{val - st:+g}"

    def diff_fraction(self, r: int, c: int) -> float:
        """Signed position of the cell on the diverging diff scale (-1..1):
        its delta over the table's largest |delta| in the active mode."""
        val = self.table.values[r][c]
        st = self.stock(r, c)
        if st is None:
            return 0.0
        stats = self._table_stats()
        if self.diff_mode == self.DIFF_PCT:
            p = pct_delta(val, st)
            if p is None:                 # moved off a zero stock: extreme
                return 1.0 if val > st else -1.0
            span = stats["max_pct"]
            return p / span if span else 0.0
        span = stats["max_abs"]
        return (val - st) / span if span else 0.0

    # -- data -------------------------------------------------------------- #
    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        val = self.table.values[r][c]

        if role == Qt.DisplayRole:
            if self.diff_mode != self.DIFF_OFF:
                return self.delta_text(r, c)
            return f"{val:g}"
        if role == Qt.EditRole:
            return f"{val:g}"

        if role == Qt.BackgroundRole:
            bg = self._bg_color(r, c, val)
            return QBrush(bg) if bg is not None else None

        if role == Qt.ForegroundRole:
            bg = self._bg_color(r, c, val)
            # No explicit background -> let the palette pick the text color.
            return QBrush(text_color_for(bg)) if bg is not None else None

        if role == Qt.FontRole and self.table.cell_changed(r, c):
            f = QFont()
            f.setBold(True)
            return f

        if role == CHANGED_ROLE:
            return self.table.cell_changed(r, c)

        if role == Qt.ToolTipRole:
            return self._tooltip(r, c, val)

        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        return None

    def _bg_color(self, r: int, c: int, val: float) -> Optional[QColor]:
        # Overlay tint takes precedence when active; cells the log never
        # touched keep the palette background (None).
        if self.overlay and self.overlay_mode == self.OVERLAY_COUNT:
            counts = self.overlay.count_grid()
            mx = max((max(row) for row in counts), default=0)
            cnt = counts[r][c] if r < len(counts) and c < len(counts[r]) else 0
            if mx > 0 and cnt > 0:
                return heat_color(cnt / mx, theme.OVERLAY_ALPHA)
            return None
        if self.overlay and self.overlay_mode == self.OVERLAY_MEAN:
            means = self.overlay.mean_grid()
            flat = [m for row in means for m in row if m is not None]
            if flat:
                lo, hi = min(flat), max(flat)
                m = means[r][c] if r < len(means) and c < len(means[r]) else None
                if m is not None:
                    span = (hi - lo) or 1.0
                    return heat_color((m - lo) / span, theme.OVERLAY_ALPHA)
            return None

        if self.diff_mode != self.DIFF_OFF:
            return theme.qcolor_rgb(theme.diff_rgb(self.diff_fraction(r, c)))
        if not self.show_heatmap:
            return None
        stats = self._table_stats()
        span = (stats["hi"] - stats["lo"]) or 1.0
        return heat_color((val - stats["lo"]) / span)

    def _tooltip(self, r: int, c: int, val: float) -> str:
        """'X=2400 rpm, Y=60 kPa, value 12.5 deg, stock 12, Δ +0.5 (+4.2%)'
        plus the log overlay's hits / mean when one is active."""
        t = self.table
        unit = clean_unit(t.unit)
        if self.shift_events and c < len(self.shift_events):
            xs = self.shift_events[c]
        else:
            xs = axis_value_label(t.x_axis, c)
        parts = [f"X={xs}"]
        if t.y_axis is not None:
            parts.append(f"Y={axis_value_label(t.y_axis, r)}")
        parts.append(f"value {val:g}{(' ' + unit) if unit else ''}")
        st = self.stock(r, c)
        if st is not None:
            p = pct_delta(val, st)
            pct = "n/a" if p is None else f"{p:+.1f}%"
            parts.append(f"stock {st:g}")
            parts.append(f"Δ {val - st:+g} ({pct})")
        lines = [", ".join(parts)]
        if self.overlay:
            counts = self.overlay.count_grid()
            means = self.overlay.mean_grid()
            if r < len(counts) and c < len(counts[r]):
                lines.append(f"log hits: {counts[r][c]}")
                m = means[r][c]
                if m is not None and self.overlay.value_channel:
                    lines.append(f"mean {self.overlay.value_channel}: {m:.2f}")
        return "\n".join(lines)

    # -- editing ----------------------------------------------------------- #
    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return False
        r, c = index.row(), index.column()
        old = self.table.values[r][c]
        if fv == old:
            return True
        if self.edit_hook is not None:
            self.edit_hook(r, c, old, fv)
            return True
        self.table.values[r][c] = fv
        self.dataChanged.emit(index, index)
        return True

    def refresh_all(self) -> None:
        """Repaint every cell after an external write to the Table."""
        self._drop_stats()
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(self.rowCount() - 1, self.columnCount() - 1))

    # -- helpers used by the window --------------------------------------- #
    def revert_cell(self, r: int, c: int) -> None:
        if self.table.stock_values is not None:
            self.table.values[r][c] = self.table.stock_values[r][c]
            idx = self.index(r, c)
            self.dataChanged.emit(idx, idx)

    def set_overlay(self, overlay: Optional[Overlay], mode: int) -> None:
        self.layoutAboutToBeChanged.emit()
        self.overlay = overlay
        self.overlay_mode = mode
        self.layoutChanged.emit()

    def set_heatmap(self, on: bool) -> None:
        self.show_heatmap = on
        self.layoutAboutToBeChanged.emit()
        self.layoutChanged.emit()

    def set_diff_mode(self, mode: int) -> None:
        """DIFF_OFF / DIFF_ABS / DIFF_PCT."""
        if mode not in (self.DIFF_OFF, self.DIFF_ABS, self.DIFF_PCT):
            raise ValueError(f"unknown diff mode {mode!r}")
        self.diff_mode = mode
        self.layoutAboutToBeChanged.emit()
        self.layoutChanged.emit()


class HeatmapDelegate(QStyledItemDelegate):
    """The model supplies Background/ForegroundRole (heatmap, diff or
    overlay tint, all theme colours), so default painting colours the cell;
    this delegate adds the changed-vs-stock corner triangle on top in the
    theme's changed-marker colour, in every mode."""

    #: Triangle leg length in px.
    MARKER = 7

    def paint(self, painter: QPainter, option, index):
        super().paint(painter, option, index)
        if not index.data(CHANGED_ROLE):
            return
        rect = option.rect
        k = self.MARKER
        right = rect.x() + rect.width()
        top = rect.y()
        tri = QPolygonF([QPointF(right - k, top), QPointF(right, top),
                         QPointF(right, top + k)])
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.qcolor("changed-marker"))
        painter.drawPolygon(tri)
        painter.restore()
