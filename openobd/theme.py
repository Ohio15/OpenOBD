"""
theme — the one place OpenOBD's colours, spacing and control heights live.

Dark is the primary theme. Every widget styles through this module: named
colour tokens (COLORS), spacing (SPACING), control heights, and helpers that
return QSS fragments or Qt colours built from those tokens. A raw colour
literal anywhere else in the package is a defect (tests/test_theme.py greps
for them; gauges.py's dial artwork is the one documented exception).

The colour-mapping functions (heat_rgb, diff_rgb, contrast_text_rgb) are pure
and Qt-free so they can be unit-tested headless; Qt objects (QColor, QIcon,
QPalette) are built lazily by the helpers that need them.

Industry cues: HP Tuners VCM Editor / VCM Scanner, EFILive, TunerStudio. The
heatmap is the full-spectrum blue -> cyan -> green -> yellow -> red the
editors use; the diff view is a diverging blue (below stock) / grey (at
stock) / red (above stock) scale.
"""
from __future__ import annotations

from typing import Optional, Sequence

RGB = tuple[int, int, int]

# --------------------------------------------------------------------------- #
# Colour tokens
# --------------------------------------------------------------------------- #
#: Core UI tokens. Values are the ones the app shipped with before the theme
#: module existed, so moving a widget onto a token is never a visual change.
COLORS: dict[str, str] = {
    # surfaces
    "surface": "#25272b",            # window background
    "surface-sunken": "#1c1e22",     # grids, plots, text views (QPalette.Base)
    "surface-raised": "#2d3035",     # buttons, toolbars, headers
    "surface-hover": "#383b41",
    "surface-pressed": "#24262a",
    "surface-disabled": "#33353a",
    "tooltip": "#303238",
    "border": "#44474d",
    "border-strong": "#5a5e66",
    # text
    "text": "#dee0e4",
    "text-muted": "#9aa0a6",
    "text-disabled": "#787c82",
    "text-disabled-strong": "#8a8d93",
    "text-bright": "#f5f6f8",
    "text-on-dark": "#ebebeb",       # cell text on a dark heat colour
    "text-on-light": "#0f0f0f",      # cell text on a light heat colour
    "white": "#ffffff",
    # intent
    "accent": "#356ac3",             # selection / focus (QPalette.Highlight)
    "link": "#78aaf0",
    "danger": "#e5484d",
    "danger-text": "#ff786e",        # QPalette.BrightText
    "danger-soft": "#eb6e64",        # Δ text / chips on dark surfaces
    "danger-muted": "#e07b72",       # safety banner text
    "danger-surface": "#3b2b2b",
    "changed-marker": "#f5a524",     # corner triangle / navigator dot
    "ok": "#3caa5f",
    "ok-text": "#8fd0a6",            # ok chips / tiles on dark surfaces
    "ok-surface": "#263a2d",
    "warning": "#d6a93a",
    "warning-text": "#e5c77a",       # warning chips / banners
    "warning-surface": "#3a3629",
    # Dashboard primary row (Record / Mark), unchanged from v0.34.0
    "record": "#b3261e",
    "record-border": "#d9443a",
    "record-hover": "#c42d24",
    "record-pressed": "#8f1d17",
    "recording": "#3b1f1f",
    "recording-text": "#ffb4ab",
    "recording-hover": "#4a2626",
    # pyqtgraph log plots
    "plot-bg": "#1c1e22",
    "plot-fg": "#c8cbd0",
    "plot-trace": "#78aaf0",
    "plot-knock": "#dc463c",
    "plot-shift": "#50c8dc",
    "plot-marker": "#ebaa3c",
    "plot-cursor": "#ebebeb",
}

#: Full-spectrum heatmap (fraction of the table's min..max range -> colour).
HEAT_STOPS: tuple[tuple[float, str], ...] = (
    (0.00, "#2c5fd0"),   # blue
    (0.25, "#1aa9c9"),   # cyan
    (0.50, "#2fae5c"),   # green
    (0.75, "#e3c22e"),   # yellow
    (1.00, "#d6402f"),   # red
)

#: Diverging diff scale (signed fraction of the table's largest |delta|).
DIFF_STOPS: tuple[tuple[float, str], ...] = (
    (-1.0, "#2f6fe0"),   # below stock
    (0.0, "#5c6068"),    # at stock
    (1.0, "#e0463a"),    # above stock
)

#: Alpha applied to log-overlay tints (count / mean): the overlay is a wash
#: over the grid, not a solid fill like the heatmap.
OVERLAY_ALPHA = 210

# --------------------------------------------------------------------------- #
# Spacing and control heights
# --------------------------------------------------------------------------- #
SPACING: dict[str, int] = {"xs": 2, "sm": 4, "md": 8, "lg": 12, "xl": 16}
RADIUS: dict[str, int] = {"sm": 4, "md": 6, "lg": 8}
FONT_PT: dict[str, int] = {"sm": 8, "body": 9, "title": 11}

#: Toolbars across the Tuning and Diagnostics sub-tabs.
TOOLBAR_HEIGHT = 36
#: The Dashboard's big primary row (Record / Mark).
PRIMARY_HEIGHT = 56
#: Breadcrumb header above each Tuning / Diagnostics sub-tab (title + note).
BREADCRUMB_HEIGHT = 50
#: Compact tree / list row height (navigator, scalars, channels).
ROW_HEIGHT = 22
#: Toolbar icon edge.
ICON_SIZE = 16


# --------------------------------------------------------------------------- #
# Pure colour math (no Qt)
# --------------------------------------------------------------------------- #
def hex_to_rgb(value: str) -> RGB:
    """'#rrggbb' -> (r, g, b)."""
    v = value.lstrip("#")
    if len(v) != 6:
        raise ValueError(f"not a #rrggbb colour: {value!r}")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def rgb_to_hex(rgb: Sequence[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb[:3]))


def token(name: str) -> str:
    """A colour token's hex value. Unknown names raise (a typo must fail
    loudly, never fall back to some default colour)."""
    return COLORS[name]


def token_rgb(name: str) -> RGB:
    return hex_to_rgb(COLORS[name])


def lerp_stops(stops: Sequence[tuple[float, str]], x: float) -> RGB:
    """Piecewise-linear interpolation through (position, '#rrggbb') stops,
    clamped to the first/last stop."""
    if not stops:
        raise ValueError("no colour stops")
    if x <= stops[0][0]:
        return hex_to_rgb(stops[0][1])
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if x <= p1:
            span = (p1 - p0) or 1.0
            k = (x - p0) / span
            a, b = hex_to_rgb(c0), hex_to_rgb(c1)
            return tuple(int(round(a[i] + (b[i] - a[i]) * k))
                         for i in range(3))  # type: ignore[return-value]
    return hex_to_rgb(stops[-1][1])


def heat_rgb(frac: float) -> RGB:
    """Heatmap colour for a fraction of the table's range; clamped to [0, 1].
    NaN maps to the low end rather than raising inside a paint."""
    if frac != frac:          # NaN
        frac = 0.0
    return lerp_stops(HEAT_STOPS, max(0.0, min(1.0, frac)))


def diff_rgb(signed_frac: float) -> RGB:
    """Diverging diff colour: -1 = furthest below stock (blue), 0 = at stock
    (grey), +1 = furthest above (red); clamped to [-1, 1]."""
    if signed_frac != signed_frac:
        signed_frac = 0.0
    return lerp_stops(DIFF_STOPS, max(-1.0, min(1.0, signed_frac)))


def luminance(rgb: Sequence[int]) -> float:
    """Perceived brightness 0..255 (ITU-R BT.601 weights)."""
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def contrast_text_rgb(bg: Sequence[int]) -> RGB:
    """Dark or light cell text, whichever reads better on bg."""
    return (token_rgb("text-on-light") if luminance(bg) >= 140
            else token_rgb("text-on-dark"))


# --------------------------------------------------------------------------- #
# Qt helpers (imported lazily so the pure functions stay Qt-free)
# --------------------------------------------------------------------------- #
def qcolor(name: str, alpha: Optional[int] = None):
    from PySide6.QtGui import QColor
    c = QColor(COLORS[name])
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def qcolor_rgb(rgb: Sequence[int], alpha: Optional[int] = None):
    from PySide6.QtGui import QColor
    c = QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def pen_rgb(name: str, alpha: Optional[int] = None) -> tuple:
    """A token as an (r, g, b[, a]) tuple, the form pyqtgraph pens take."""
    r, g, b = token_rgb(name)
    return (r, g, b) if alpha is None else (r, g, b, alpha)


def apply_dark_palette(app) -> None:
    """App-wide dark Fusion palette built from the tokens (dark mode is the
    primary theme). main() calls this through app.apply_dark_theme."""
    from PySide6.QtGui import QPalette
    app.setStyle("Fusion")
    p = QPalette()
    text = qcolor("text")
    disabled = qcolor("text-disabled")
    p.setColor(QPalette.Window, qcolor("surface"))
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, qcolor("surface-sunken"))
    p.setColor(QPalette.AlternateBase, qcolor("surface"))
    p.setColor(QPalette.ToolTipBase, qcolor("tooltip"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.PlaceholderText, disabled)
    p.setColor(QPalette.Button, qcolor("surface-raised"))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, qcolor("danger-text"))
    p.setColor(QPalette.Link, qcolor("link"))
    p.setColor(QPalette.Highlight, qcolor("accent"))
    p.setColor(QPalette.HighlightedText, qcolor("text-bright"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    app.setPalette(p)


def configure_pyqtgraph() -> None:
    """pyqtgraph's global plot colours from the tokens."""
    import pyqtgraph as pg
    pg.setConfigOption("background", pen_rgb("plot-bg"))
    pg.setConfigOption("foreground", pen_rgb("plot-fg"))
    pg.setConfigOptions(antialias=True)


# --------------------------------------------------------------------------- #
# QSS fragments
# --------------------------------------------------------------------------- #
def _c(name: str) -> str:
    return COLORS[name]


def record_button_qss() -> str:
    """The Dashboard's primary Record/Stop button (objectName recordButton)."""
    return f"""
QPushButton#recordButton {{
    background-color: {_c('record')}; color: {_c('white')};
    border: 2px solid {_c('record-border')}; border-radius: {RADIUS['lg']}px;
    font-size: 16pt; font-weight: bold; padding: 4px 16px;
}}
QPushButton#recordButton:hover {{ background-color: {_c('record-hover')}; }}
QPushButton#recordButton:pressed {{ background-color: {_c('record-pressed')}; }}
QPushButton#recordButton[recording="true"] {{
    background-color: {_c('recording')}; color: {_c('recording-text')}; border: 2px solid {_c('danger')};
}}
QPushButton#recordButton[recording="true"]:hover {{
    background-color: {_c('recording-hover')};
}}
QPushButton#recordButton:disabled {{
    background-color: {_c('surface-disabled')}; color: {_c('text-disabled-strong')}; border: 2px solid {_c('border')};
}}
"""


def mark_button_qss() -> str:
    """The Dashboard's Mark button (objectName markButton)."""
    return f"""
QPushButton#markButton {{
    background-color: {_c('surface-raised')}; color: {_c('text')};
    border: 2px solid {_c('border-strong')}; border-radius: {RADIUS['lg']}px;
    font-size: 14pt; font-weight: bold; padding: 4px 12px;
}}
QPushButton#markButton:hover {{ background-color: {_c('surface-hover')}; }}
QPushButton#markButton:pressed {{ background-color: {_c('surface-pressed')}; }}
"""


def muted_qss(size_pt: Optional[int] = None) -> str:
    """Muted help / secondary text."""
    size = f" font-size:{size_pt}pt;" if size_pt else ""
    return f"color:{_c('text-muted')};{size}"


def text_qss(name: str, extra: str = "") -> str:
    """A label coloured by token, with optional extra declarations."""
    return f"color:{_c(name)};{(' ' + extra) if extra else ''}"


#: objectNames of the workspace toolbars that share one look.
TOOLBAR_NAMES = ("tuningToolbar", "diagToolbar", "liveToolbar")


def _toolbar_block(name: str) -> str:
    return f"""
QToolBar#{name} {{
    background: {_c('surface-raised')}; border: none;
    border-bottom: 1px solid {_c('border')};
    spacing: {SPACING['xs']}px; padding: 0px {SPACING['sm']}px;
    /* QSS heights are the content box: + the 1 px bottom border = TOOLBAR_HEIGHT */
    min-height: {TOOLBAR_HEIGHT - 1}px; max-height: {TOOLBAR_HEIGHT - 1}px;
}}
QToolBar#{name} QToolButton {{
    color: {_c('text')}; background: transparent;
    border: 1px solid transparent; border-radius: {RADIUS['sm']}px;
    padding: 2px {SPACING['sm']}px;
}}
QToolBar#{name} QToolButton:hover {{
    background: {_c('surface-hover')}; border-color: {_c('border')};
}}
QToolBar#{name} QToolButton:pressed {{ background: {_c('surface-pressed')}; }}
QToolBar#{name} QToolButton:checked {{
    background: {_c('surface-pressed')}; border-color: {_c('accent')};
}}
QToolBar#{name} QToolButton[primary="true"] {{
    font-weight: bold; border-color: {_c('accent')};
}}
QToolBar#{name} QToolButton:disabled {{
    color: {_c('text-disabled')}; border-color: transparent;
}}
QToolBar#{name} QLabel {{ color: {_c('text-muted')}; padding: 0px {SPACING['xs']}px; }}
QToolBar#{name}::separator {{
    background: {_c('border')}; width: 1px; margin: {SPACING['sm']}px {SPACING['xs']}px;
}}
"""


def toolbar_qss() -> str:
    """The 36 px workspace toolbar (Tuning and Diagnostics). A QToolButton
    with the dynamic property primary=true is the sub-tab's main action."""
    return "".join(_toolbar_block(n) for n in TOOLBAR_NAMES)


def breadcrumb_qss() -> str:
    return f"""
QFrame#breadcrumb {{
    background: {_c('surface')}; border: none;
    border-bottom: 1px solid {_c('border')};
}}
QLabel#breadcrumbTitle {{ color: {_c('text')}; font-weight: bold; font-size: {FONT_PT['title']}pt; }}
QLabel#breadcrumbNote {{ color: {_c('text-muted')}; }}
"""


def chip_qss() -> str:
    """Chips are QLabels with the dynamic property chip = neutral | danger |
    warning | accent | muted | ok. Re-polish after changing the property
    (tuningui.set_chip)."""
    return f"""
QLabel[chip="neutral"] {{
    color: {_c('text')}; background: {_c('surface-raised')};
    border: 1px solid {_c('border')}; border-radius: {RADIUS['md']}px; padding: 1px {SPACING['md']}px;
}}
QLabel[chip="danger"] {{
    color: {_c('danger-text')}; background: {_c('danger-surface')};
    border: 1px solid {_c('danger')}; border-radius: {RADIUS['md']}px; padding: 1px {SPACING['md']}px;
}}
QLabel[chip="muted"] {{
    color: {_c('text-muted')}; background: {_c('surface-sunken')};
    border: 1px dashed {_c('border')}; border-radius: {RADIUS['md']}px; padding: 1px {SPACING['md']}px;
}}
QLabel[chip="accent"] {{
    color: {_c('link')}; background: {_c('surface-raised')};
    border: 1px solid {_c('accent')}; border-radius: {RADIUS['md']}px; padding: 1px {SPACING['md']}px;
}}
QLabel[chip="ok"] {{
    color: {_c('ok-text')}; background: {_c('ok-surface')};
    border: 1px solid {_c('ok')}; border-radius: {RADIUS['md']}px; padding: 1px {SPACING['md']}px;
}}
QLabel[chip="warning"] {{
    color: {_c('warning-text')}; background: {_c('warning-surface')};
    border: 1px solid {_c('warning')}; border-radius: {RADIUS['md']}px; padding: 1px {SPACING['md']}px;
}}
"""


def banner_qss() -> str:
    """The calibration safety note (#safetyBanner) and the inline status
    banners: QLabels with the dynamic property banner = danger | warning |
    muted (Diagnostics scan / link errors, MIL state)."""
    return f"""
QLabel#safetyBanner {{
    color: {_c('danger-muted')}; background: {_c('surface-sunken')};
    border: 1px solid {_c('border')}; border-left: 3px solid {_c('danger-muted')};
    border-radius: {RADIUS['sm']}px; padding: {SPACING['md']}px;
}}
QLabel[banner="danger"] {{
    color: {_c('danger-text')}; background: {_c('danger-surface')};
    border: 1px solid {_c('danger')}; border-left: 3px solid {_c('danger')};
    border-radius: {RADIUS['sm']}px; padding: {SPACING['md']}px; font-weight: bold;
}}
QLabel[banner="warning"] {{
    color: {_c('warning-text')}; background: {_c('warning-surface')};
    border: 1px solid {_c('warning')}; border-left: 3px solid {_c('warning')};
    border-radius: {RADIUS['sm']}px; padding: {SPACING['md']}px;
}}
QLabel[banner="muted"] {{
    color: {_c('text-muted')}; background: {_c('surface-sunken')};
    border: 1px dashed {_c('border')}; border-left: 3px solid {_c('border-strong')};
    border-radius: {RADIUS['sm']}px; padding: {SPACING['md']}px;
}}
"""


def splitter_qss() -> str:
    return f"""
QSplitter::handle {{ background: {_c('border')}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QSplitter::handle:hover {{ background: {_c('accent')}; }}
"""


def tree_qss() -> str:
    """Compact rows for the navigator, scalars and channel trees."""
    return f"""
QTreeView#navigatorTree, QTreeView#scalarsTree, QTreeWidget#channelTree,
QTableWidget#cursorReadout, QTreeView#dtcTree, QTableWidget#graphTable {{
    background: {_c('surface-sunken')}; border: 1px solid {_c('border')};
}}
QTableWidget#graphTable {{ gridline-color: {_c('surface-raised')}; color: {_c('text')}; }}
QTableWidget#graphTable QHeaderView::section {{
    background: {_c('surface-raised')}; color: {_c('text-muted')};
    border: none; border-bottom: 1px solid {_c('border')};
    padding: 2px {SPACING['md']}px;
}}
QTreeView#navigatorTree::item, QTreeView#scalarsTree::item,
QTreeWidget#channelTree::item, QTreeView#dtcTree::item {{ min-height: {ROW_HEIGHT}px; }}
QLineEdit#navSearch, QLineEdit#scalarSearch, QLineEdit#dtcSearch,
QLineEdit#liveFilter {{
    background: {_c('surface-sunken')}; color: {_c('text')};
    border: 1px solid {_c('border')}; border-radius: {RADIUS['sm']}px;
    padding: 3px {SPACING['sm']}px;
}}
QLineEdit#navSearch:focus, QLineEdit#scalarSearch:focus,
QLineEdit#dtcSearch:focus, QLineEdit#liveFilter:focus {{ border-color: {_c('accent')}; }}
"""


def panel_qss() -> str:
    """The editor's collapsible Log overlay panel and the readout strips."""
    return f"""
QFrame#overlayPanel {{
    background: {_c('surface')}; border: 1px solid {_c('border')}; border-radius: {RADIUS['sm']}px;
}}
QToolButton#overlayToggle {{
    color: {_c('text')}; background: {_c('surface-raised')};
    border: 1px solid {_c('border')}; border-radius: {RADIUS['sm']}px; padding: 2px {SPACING['sm']}px;
}}
QToolButton#overlayToggle:checked {{ border-color: {_c('accent')}; }}
QLabel#selectionReadout {{
    color: {_c('text-muted')}; background: {_c('surface')};
    border-top: 1px solid {_c('border')}; padding: 2px {SPACING['md']}px;
}}
QLabel#emptyState {{ color: {_c('text-muted')}; font-size: {FONT_PT['title']}pt; }}
QLabel#cornerLabel {{
    color: {_c('text-muted')}; background: {_c('surface-raised')};
    font-size: {FONT_PT['sm']}pt; padding: 0px {SPACING['xs']}px;
}}
"""


def diag_panel_qss() -> str:
    """Diagnostics: the module card, the map legend row and the readiness
    monitor tiles."""
    return f"""
QLabel#cardTitle {{ color: {_c('text')}; font-weight: bold; font-size: {FONT_PT['title']}pt; }}
QLabel#cardKey {{ color: {_c('text-muted')}; }}
QLabel#cardValue, QLabel#cardNotes {{ color: {_c('text')}; }}
QLabel#sectionTitle {{ color: {_c('text')}; font-weight: bold; }}
QLabel#legendText {{ color: {_c('text-muted')}; }}
QFrame#monitorTile {{
    background: {_c('surface-raised')}; border: 1px solid {_c('border')};
    border-left: 3px solid {_c('border-strong')}; border-radius: {RADIUS['sm']}px;
}}
QFrame#monitorTile[state="ready"] {{ border-left: 3px solid {_c('ok')}; }}
QFrame#monitorTile[state="notready"] {{ border-left: 3px solid {_c('warning')}; }}
QLabel#monitorName {{ color: {_c('text')}; }}
QScrollArea#readinessScroll, QScrollArea#cardScroll {{ background: transparent; border: none; }}
QWidget#readinessGrid, QWidget#cardBody {{ background: transparent; }}
"""


def workspace_qss() -> str:
    """Everything a workspace page (Tuning, Diagnostics) needs, applied once
    to its root so both pages share one set of chrome."""
    return (toolbar_qss() + breadcrumb_qss() + chip_qss() + banner_qss()
            + splitter_qss() + tree_qss() + panel_qss() + diag_panel_qss())


def livedata_qss() -> str:
    """Everything the Live Data page needs: the shared workspace chrome plus
    the tile-group headers, the tile name / unit / footer rows and the
    session picker inside the 36 px toolbar."""
    return workspace_qss() + f"""
QToolButton#tileGroupHeader {{
    color: {_c('text')}; background: transparent; border: none;
    border-bottom: 1px solid {_c('border')}; font-weight: bold;
    padding: 2px {SPACING['xs']}px; text-align: left;
}}
QToolButton#tileGroupHeader:hover {{ color: {_c('text-bright')}; }}
QLabel#tileName {{ color: {_c('text-muted')}; font-size: {FONT_PT['sm']}pt; }}
QLabel#tileUnit {{ color: {_c('text-muted')}; font-size: {FONT_PT['sm']}pt; }}
QLabel#tileRange {{ color: {_c('text-disabled')}; font-size: {FONT_PT['sm'] - 1}pt; }}
QScrollArea#tileScroll {{ background: {_c('surface')}; border: none; }}
QWidget#tileHost {{ background: {_c('surface')}; }}
QLabel#chartNote {{ color: {_c('text-muted')}; padding: 0px {SPACING['sm']}px; }}
"""


def swatch_icon(color, size: int = 10):
    """A filled rounded square in a trace colour (a QColor or '#rrggbb'
    palette value): the graphed-channels table's legend swatch."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    qc = color if isinstance(color, QColor) else hex_qcolor(color)
    px = QPixmap(size * 2, size * 2)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(qc)
    p.drawRoundedRect(QRectF(size * 0.3, size * 0.3, size * 1.4, size * 1.4),
                      size * 0.3, size * 0.3)
    p.end()
    return QIcon(px)


def tuning_qss() -> str:
    """Everything the Tuning workspace needs, applied once to its root."""
    return workspace_qss()


def diag_qss() -> str:
    """Everything the Diagnostics workspace needs: the same chrome."""
    return workspace_qss()


# --------------------------------------------------------------------------- #
# Toolbar glyph icons — vector-drawn, so they render without any icon or
# emoji font (offscreen / minimal systems have neither).
# --------------------------------------------------------------------------- #
GLYPHS = ("set", "add", "sub", "mul", "div", "pct", "interp_h", "interp_v",
          "interp_2d", "smooth", "revert", "next", "copy", "paste", "heatmap",
          "diff", "open", "events", "learn", "search", "scan", "read",
          "clear", "not_examined", "incomplete", "refresh", "fields", "hold")


def glyph_icon(kind: str, color: str = "text", size: int = ICON_SIZE):
    """A QIcon for a toolbar action, drawn with QPainter lines in a token
    colour. Unknown kinds raise."""
    if kind not in GLYPHS:
        raise ValueError(f"unknown glyph {kind!r}")
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap

    scale = 2                                  # crisp on high-DPI
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(scale, scale)
    s = float(size)
    pen = QPen(qcolor(color), max(1.4, s / 10.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    m = s * 0.2                                # margin
    lo, hi, mid = m, s - m, s / 2.0

    def line(x0, y0, x1, y1):
        p.drawLine(QPointF(x0, y0), QPointF(x1, y1))

    def arrow_head(x, y, dx, dy):
        # small chevron at (x, y) pointing along (dx, dy)
        k = s * 0.16
        if dx:
            line(x, y, x - dx * k, y - k); line(x, y, x - dx * k, y + k)
        else:
            line(x, y, x - k, y - dy * k); line(x, y, x + k, y - dy * k)

    if kind == "set":
        line(lo, mid - s * 0.12, hi, mid - s * 0.12)
        line(lo, mid + s * 0.12, hi, mid + s * 0.12)
    elif kind == "add":
        line(lo, mid, hi, mid); line(mid, lo, mid, hi)
    elif kind == "sub":
        line(lo, mid, hi, mid)
    elif kind == "mul":
        d = s * 0.25
        line(mid - d, mid - d, mid + d, mid + d)
        line(mid - d, mid + d, mid + d, mid - d)
    elif kind == "div":
        line(lo, mid, hi, mid)
        r = s * 0.06
        p.setBrush(qcolor(color))
        p.drawEllipse(QPointF(mid, lo + r), r, r)
        p.drawEllipse(QPointF(mid, hi - r), r, r)
    elif kind == "pct":
        line(hi, lo, lo, hi)
        r = s * 0.11
        p.drawEllipse(QPointF(lo + r, lo + r), r, r)
        p.drawEllipse(QPointF(hi - r, hi - r), r, r)
    elif kind == "interp_h":
        line(lo, mid, hi, mid)
        arrow_head(hi, mid, 1, 0); arrow_head(lo, mid, -1, 0)
    elif kind == "interp_v":
        line(mid, lo, mid, hi)
        arrow_head(mid, hi, 0, 1); arrow_head(mid, lo, 0, -1)
    elif kind == "interp_2d":
        p.drawRect(QRectF(lo, lo, hi - lo, hi - lo))
        line(lo, hi, hi, lo)
    elif kind == "smooth":
        path = QPainterPath(QPointF(lo, mid))
        path.cubicTo(QPointF(lo + s * 0.2, lo), QPointF(mid, lo),
                     QPointF(mid, mid))
        path.cubicTo(QPointF(mid, hi), QPointF(hi - s * 0.2, hi),
                     QPointF(hi, mid))
        p.drawPath(path)
    elif kind == "revert":
        path = QPainterPath()
        path.arcMoveTo(QRectF(lo, lo, hi - lo, hi - lo), 150)
        path.arcTo(QRectF(lo, lo, hi - lo, hi - lo), 150, -260)
        p.drawPath(path)
        arrow_head(lo + s * 0.04, mid - s * 0.12, 0, -1)
    elif kind == "next":
        line(lo, lo, hi - s * 0.15, mid); line(hi - s * 0.15, mid, lo, hi)
        line(hi, lo, hi, hi)
    elif kind == "copy":
        p.drawRect(QRectF(lo, lo, s * 0.4, s * 0.45))
        p.drawRect(QRectF(mid - s * 0.05, mid - s * 0.05, s * 0.4, s * 0.45))
    elif kind == "paste":
        p.drawRect(QRectF(lo, lo + s * 0.08, hi - lo, hi - lo - s * 0.08))
        line(mid - s * 0.15, lo, mid + s * 0.15, lo)
        line(lo + s * 0.15, mid, hi - s * 0.15, mid)
        line(lo + s * 0.15, mid + s * 0.18, hi - s * 0.15, mid + s * 0.18)
    elif kind == "heatmap":
        p.setPen(Qt.NoPen)
        cell = (hi - lo) / 2.0
        cols = [heat_rgb(0.0), heat_rgb(0.5), heat_rgb(0.75), heat_rgb(1.0)]
        for i, rgb in enumerate(cols):
            p.setBrush(qcolor_rgb(rgb))
            p.drawRect(QRectF(lo + (i % 2) * cell, lo + (i // 2) * cell,
                              cell - 0.5, cell - 0.5))
    elif kind == "diff":
        p.setPen(Qt.NoPen)
        w = (hi - lo) / 2.0
        p.setBrush(qcolor_rgb(diff_rgb(-1.0)))
        p.drawRect(QRectF(lo, lo, w, hi - lo))
        p.setBrush(qcolor_rgb(diff_rgb(1.0)))
        p.drawRect(QRectF(lo + w, lo, w, hi - lo))
    elif kind == "open":
        path = QPainterPath(QPointF(lo, hi))
        path.lineTo(lo, lo + s * 0.1)
        path.lineTo(mid - s * 0.1, lo + s * 0.1)
        path.lineTo(mid, lo + s * 0.22)
        path.lineTo(hi, lo + s * 0.22)
        path.lineTo(hi, hi)
        path.closeSubpath()
        p.drawPath(path)
    elif kind == "events":
        for x in (lo + s * 0.1, mid, hi - s * 0.1):
            line(x, lo, x, hi)
    elif kind == "learn":
        path = QPainterPath(QPointF(lo, hi))
        path.lineTo(lo + s * 0.2, hi)
        path.lineTo(hi, lo)
        p.drawPath(path)
        line(lo, hi, hi, hi)
    elif kind == "search":
        r = s * 0.22
        p.drawEllipse(QPointF(mid - s * 0.06, mid - s * 0.06), r, r)
        line(mid + r * 0.6, mid + r * 0.6, hi, hi)
    elif kind == "scan":
        # a node tree: the tool on the left fanning out to three modules
        r = s * 0.07
        p.setBrush(qcolor(color))
        p.drawEllipse(QPointF(lo, mid), r * 1.4, r * 1.4)
        for y in (lo, mid, hi):
            line(lo, mid, hi - s * 0.12, y)
            p.drawEllipse(QPointF(hi - s * 0.04, y), r, r)
    elif kind == "read":
        # a list: three bulleted rows
        r = s * 0.06
        p.setBrush(qcolor(color))
        for y in (lo + s * 0.04, mid, hi - s * 0.04):
            p.drawEllipse(QPointF(lo + r, y), r, r)
            line(lo + s * 0.25, y, hi, y)
    elif kind == "refresh":
        # a circular arrow: rescan
        rect = QRectF(lo, lo, hi - lo, hi - lo)
        path = QPainterPath()
        path.arcMoveTo(rect, 60)
        path.arcTo(rect, 60, 280)
        p.drawPath(path)
        tip = path.pointAtPercent(0.0)
        line(tip.x(), tip.y(), tip.x() + s * 0.2, tip.y() - s * 0.02)
        line(tip.x(), tip.y(), tip.x() + s * 0.02, tip.y() - s * 0.2)
    elif kind == "fields":
        # a checklist: two ticked boxes and an empty one
        b = s * 0.18
        for i, y in enumerate((lo, mid - b / 2, hi - b)):
            p.drawRect(QRectF(lo, y, b, b))
            line(lo + b + s * 0.12, y + b / 2, hi, y + b / 2)
            if i < 2:
                line(lo + b * 0.2, y + b * 0.5, lo + b * 0.45, y + b * 0.8)
                line(lo + b * 0.45, y + b * 0.8, lo + b * 0.9, y + b * 0.15)
    elif kind == "hold":
        # pause: two bars
        p.setPen(Qt.NoPen)
        p.setBrush(qcolor(color))
        w = s * 0.18
        p.drawRect(QRectF(mid - w * 1.6, lo, w, hi - lo))
        p.drawRect(QRectF(mid + w * 0.6, lo, w, hi - lo))
    elif kind == "clear":
        # an eraser: slanted block with its band
        path = QPainterPath(QPointF(lo + s * 0.05, mid + s * 0.12))
        path.lineTo(mid - s * 0.02, lo + s * 0.05)
        path.lineTo(hi, mid - s * 0.02)
        path.lineTo(mid + s * 0.08, hi - s * 0.02)
        path.lineTo(lo + s * 0.22, hi - s * 0.02)
        path.closeSubpath()
        p.drawPath(path)
        line(mid - s * 0.2, mid - s * 0.12, mid + s * 0.12, mid + s * 0.2)
    elif kind == "not_examined":
        # dashed ring with a centre dot: nothing was looked at
        dash = QPen(pen)
        dash.setStyle(Qt.DashLine)
        p.setPen(dash)
        p.drawEllipse(QPointF(mid, mid), (hi - lo) / 2, (hi - lo) / 2)
        p.setPen(Qt.NoPen)
        p.setBrush(qcolor(color))
        p.drawEllipse(QPointF(mid, mid), s * 0.07, s * 0.07)
    elif kind == "incomplete":
        # a ring with one half filled: part of the answer is missing
        rect = QRectF(lo, lo, hi - lo, hi - lo)
        p.drawEllipse(rect)
        path = QPainterPath(QPointF(mid, mid))
        path.arcTo(rect, 90, 180)
        path.closeSubpath()
        p.setBrush(qcolor(color))
        p.drawPath(path)
    p.end()
    return QIcon(px)


def changed_dot_icon(size: int = 10):
    """The navigator's changed-vs-stock dot."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QIcon, QPainter, QPixmap
    px = QPixmap(size * 2, size * 2)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(qcolor("changed-marker"))
    p.drawEllipse(QPointF(size, size), size * 0.55, size * 0.55)
    p.end()
    return QIcon(px)


def no_stock_icon(size: int = 10):
    """The navigator's no-stock-baseline marker: a hollow muted ring, so a
    table whose changes cannot be tracked never looks verified-unchanged."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap
    px = QPixmap(size * 2, size * 2)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(qcolor("text-muted"), max(1.5, size * 0.18)))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(size, size), size * 0.45, size * 0.45)
    p.end()
    return QIcon(px)


def blank_icon(size: int = 10):
    """Same footprint as changed_dot_icon, nothing drawn: keeps the
    navigator's rows aligned whether or not a table carries the dot."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon, QPixmap
    px = QPixmap(size * 2, size * 2)
    px.fill(Qt.transparent)
    return QIcon(px)


# ---- Live Data / Diagnostics / strip chart palettes (moved from their modules, values unchanged) ----
# Each value is byte-for-byte the literal its module used before the move
# (hex case preserved; former QColor(r, g, b) literals written as the
# equivalent #rrggbb), so adopting these tokens is never a visual change.

def hex_qcolor(value: str, alpha: Optional[int] = None):
    """QColor from a '#rrggbb' palette value (any case), optional alpha."""
    return qcolor_rgb(hex_to_rgb(value), alpha)


# -- Diagnostics (diagui.py): module map, DTC table, readiness ------------- #
DIAG_COLORS: dict[str, str] = {
    # module / segment verdicts
    "status-ok": COLORS["ok"],             # (60, 170, 95)
    "status-silent": "#cd3c32",            # (205, 60, 50)  no response / failed
    "status-unreachable": "#cd962d",       # (205, 150, 45)
    "status-not-examined": "#6e768c",      # (110, 118, 140) cooler grey
    "status-unknown": "#5f646e",           # (95, 100, 110)  never scanned
    # module-map canvas
    "map-canvas": "#181a1e",               # (24, 26, 30)
    "node-title": "#e4e6ea",               # (228, 230, 234)
    "node-sub": "#a5aab2",                 # (165, 170, 178)
    "node-tool": "#5a82c8",                # (90, 130, 200)  PC / unscanned link
    "segment-failed-mark": "#eb5046",      # (235, 80, 70)   warning glyph
    "segment-label": "#8c929c",            # (140, 146, 156)
    "legend-text": "#aaafb7",              # (170, 175, 183)
    # codes & readiness
    "not-examined-text": "#969eb4",        # (150, 158, 180) DTC rows + MIL label
    "dtc-stored": COLORS["danger-soft"],   # (235, 110, 100)
    "monitor-ready": "#60be78",            # (96, 190, 120)  also MIL off
    "monitor-not-ready": "#e1a53c",        # (225, 165, 60)
    "mil-on": "#e06c60",
}
#: Alpha of a module node's translucent fill (its verdict colour).
DIAG_NODE_FILL_ALPHA = 46


def diag_qcolor(name: str, alpha: Optional[int] = None):
    return hex_qcolor(DIAG_COLORS[name], alpha)


# -- Live Data (livedata.py) ---------------------------------------------- #
#: Value-tile display states: (background, accent/border, value text). Each
#: state means exactly one thing and may take ONLY these colours.
LIVEDATA_STATE_COLORS: dict[str, tuple[str, str, str]] = {
    "fresh":    ("#2E3B31", "#5FD08A", "#DEE0E4"),   # measured now — live
    "stale":    ("#3A3629", "#D6A93A", "#D6C9A0"),   # live view, but not advancing
    "archive":  ("#2B3138", "#5A7EC3", "#AEBBD0"),   # finished drive (not live)
    "carried":  ("#3A3629", "#D6A93A", "#D6C9A0"),   # measured earlier, carried
    "error":    ("#3B2B2B", "#CD3C32", "#E7A9A4"),   # module returned an error
    "unavail":  ("#2B3138", "#5A7EC3", "#9FB0C8"),   # answered with nothing
    "notread":  ("#25272B", "#5F646E", "#7B8088"),   # nobody ever asked
    "badvalue": ("#3B2B33", "#C0568F", "#E3A9C6"),   # tool-side format fault
    "failed":   ("#3B2B2B", "#CD3C32", "#E7A9A4"),   # the read itself failed
}

LIVEDATA_COLORS: dict[str, str] = {
    "secondary-text": "#9DA3AD",     # tile channel name, page status line
    "faint-text": "#7B8088",         # tile state caption, empty state, stamp
    "info-bg": "#2B3138",            # Controls safety banner
    "info-border": "#5A7EC3",
    "info-text": "#C7D2E4",
    "clean-text": "#8FD0A6",         # control state: nothing unreleased
    "journal-activate": "#D6A93A",   # activate / fire_refused journal rows
    "danger-text": "#E7A9A4",
    "danger-bg": "#3B2B2B",
    "danger-border": "#CD3C32",
    "warn-text": "#E5C77A",
    "warn-bg": "#3A3629",
    "warn-border": "#D6A93A",
}


def livedata_qcolor(name: str, alpha: Optional[int] = None):
    return hex_qcolor(LIVEDATA_COLORS[name], alpha)


def livedata_text_qss(name: str, extra: str = "") -> str:
    """'color:<token>;' plus optional extra declarations (same shape as
    text_qss, over the Live Data palette)."""
    return f"color:{LIVEDATA_COLORS[name]};{(' ' + extra) if extra else ''}"


def livedata_tile_qss(state: str) -> tuple[str, str]:
    """(frame QSS, value-label QSS) for a ValueTile in a display state;
    unknown states fall back to 'notread'."""
    bg, accent, text = LIVEDATA_STATE_COLORS.get(
        state, LIVEDATA_STATE_COLORS["notread"])
    return (f"ValueTile {{ background:{bg}; border:1px solid {accent}; "
            f"border-radius:6px; }}", f"color:{text};")


def livedata_banner_qss() -> str:
    c = LIVEDATA_COLORS
    return (f"background:{c['info-bg']}; border:1px solid {c['info-border']}; "
            f"border-radius:6px; color:{c['info-text']}; padding:12px; "
            f"font-size:12px;")


def livedata_alert_qss(kind: str) -> str:
    """Control-state alert box; kind is 'danger' or 'warn'."""
    c = LIVEDATA_COLORS
    return (f"color:{c[kind + '-text']}; background:{c[kind + '-bg']}; "
            f"border:1px solid {c[kind + '-border']}; "
            f"border-radius:6px; font-size:13px; padding:10px;")


# -- Strip chart (stripchart.py) ------------------------------------------ #
#: Per-slot channel colours within a lane (VCM-style: red, green, white, cyan…).
STRIP_CHANNEL_COLORS: list[str] = [
    "#FF5048", "#54E06E", "#F2F4F8", "#4FC3FF",
    "#E060D8", "#E8D44D", "#FF9A40", "#8AB4FF"]

#: View-state caption colours — same meanings as LIVEDATA_STATE_COLORS accents.
STRIP_CAPTION_COLORS: dict[str, str] = {
    "live": "#5FD08A", "not advancing": "#D6A93A",
    "archived drive": "#5A7EC3", "read failed": "#CD3C32",
    "held": "#D6A93A",
}

STRIP_COLORS: dict[str, str] = {
    "canvas": "#05060a",             # (5, 6, 10)
    "empty-text": "#7b8088",         # (123, 128, 136)
    "label-glow-start": "#0a0c18",   # (10, 12, 24)
    "label-glow-end": "#101634",     # (16, 22, 52)
    "plot-glow-start": "#0c1026",    # (12, 16, 38)
    "plot-glow-mid": "#060812",      # (6, 8, 18)
    "plot-glow-end": "#04050a",      # (4, 5, 10)
    "gridline": "#262c3a",           # (38, 44, 58)
    "lane-separator": "#3a3f4a",     # (58, 63, 74)
    "caption-fallback": "#CD3C32",
    "readout-bg": "#0B0D14",
    "readout-border": "#3A3F4A",
    "readout-text": "#DEE0E4",
    "readout-grid": "#262C3A",
    "readout-header-bg": "#1A1E2A",
    "secondary-text": "#9DA3AD",     # readout header / unit / lane, toolbar note
    # time axis, hover cursor and session-event markers
    "axis-text": "#8A909A",
    "axis-tick": "#3A3F4A",
    "time-grid": "#161B28",
    "cursor": "#EBEBEB",
    "cursor-box-bg": "#10131C",
    "cursor-box-border": "#5A5E66",
    "event-marker": "#EBAA3C",
}


def strip_qcolor(name: str, alpha: Optional[int] = None):
    return hex_qcolor(STRIP_COLORS[name], alpha)


def strip_readout_qss() -> str:
    """The cursor-readout table under the strip chart."""
    c = STRIP_COLORS
    return (f"QTableWidget {{ background:{c['readout-bg']}; "
            f"border:1px solid {c['readout-border']}; "
            f"color:{c['readout-text']}; gridline-color:{c['readout-grid']}; }}"
            f"QHeaderView::section {{ background:{c['readout-header-bg']}; "
            f"color:{c['secondary-text']}; "
            f"border:0px; padding:2px 8px; }}")
