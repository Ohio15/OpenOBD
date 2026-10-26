"""Tuning tab UX (v0.35.0): navigator, toolbar editor, grid colouring, diff
mode, clipboard, scalars, Log Analysis chrome and the page's minimum width.
Offscreen Qt; settings redirected to a throwaway INI tree; no sleeps."""
import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import editops, theme  # noqa: E402
from openobd.calspec import Axis, Table  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      tempfile.mkdtemp(prefix="openobd_tuningui_"))
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def win(qapp):
    from openobd.app import MainWindow, load_default_cal
    cal, _path = load_default_cal()
    w = MainWindow(cal, None)       # no path: nothing can be saved over data/
    w.resize(1366, 768)
    w.show()
    qapp.processEvents()
    yield w
    w.dirty = False
    w._shutdown_pages()
    w.close()


def make_table():
    vals = [[0.0, 10.0, 20.0, 30.0],
            [5.0, 15.0, 25.0, 35.0],
            [9.0, 19.0, 29.0, 39.0]]
    return Table(
        name="Spark Test", unit="deg", category="Spark/Main",
        x_axis=Axis("RPM", "rpm", [800, 1600, 2400, 3200]),
        y_axis=Axis("Load", "g/cyl", [0.2, 0.4, 0.6]),
        values=[row[:] for row in vals],
        stock_values=[row[:] for row in vals],
    )


def select_rect(win, r0, c0, r1, c1):
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    m = win.current_model
    sel = QItemSelection(m.index(r0, c0), m.index(r1, c1))
    win.view.setCurrentIndex(m.index(r0, c0))
    win.view.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)


def open_table(win, name):
    win._select_table(name)
    return win.current_model.table


def first_2d_table(win):
    for t in win.cal.tables:
        if (t.y_axis is not None and t.n_rows >= 3 and t.n_cols >= 3
                and t.stock_values is not None):
            return t
    pytest.skip("sample calibration has no 3x3+ table with stock")


def restore_stock(win, t):
    for r in range(t.n_rows):
        t.values[r] = t.stock_values[r][:]
    win.current_model.refresh_all()


# --------------------------------------------------------------------------- #
# Navigator
# --------------------------------------------------------------------------- #
def nav_settings(tmp_path):
    from PySide6.QtCore import QSettings
    return QSettings(str(tmp_path / "nav.ini"), QSettings.IniFormat)


def tables_fixture():
    out = []
    for i in range(12):
        t = make_table()
        t.name = f"Table {i:02d}"
        t.category = "Spark/Main" if i % 2 else "Fuel/Base"
        t.note = "knock limit" if i == 3 else ""
        out.append(t)
    return out


def test_navigator_filter_counts_and_note_search(qapp, tmp_path):
    from openobd.tuningui import Navigator
    nav = Navigator(tables_fixture(), nav_settings(tmp_path))
    assert nav.category_label("Fuel") == "Fuel (6)"
    assert nav.category_label("Spark") == "Spark (6)"
    nav.search.setText("table 0")        # search-as-you-type
    assert sorted(nav.visible_table_names()) == [
        f"Table 0{i}" for i in range(10)]
    assert nav.category_label("Fuel") == "Fuel (5)"
    nav.search.setText("KNOCK")          # note text, case-insensitive
    assert nav.visible_table_names() == ["Table 03"]
    assert nav._cats["Fuel"].isHidden()  # empty category hides
    nav.search.setText("")
    assert len(nav.visible_table_names()) == 12


def test_navigator_recent_persists_and_caps_at_8(qapp, tmp_path):
    from openobd.tuningui import RECENT_MAX, Navigator
    tables = tables_fixture()
    nav = Navigator(tables, nav_settings(tmp_path))
    assert nav.recent() == [] and nav._recent_root.isHidden()
    for t in tables[:10]:
        nav.push_recent(t.name)
    nav.push_recent("Table 04")            # re-open moves it to the top
    nav.push_recent("not a table")         # ignored
    assert RECENT_MAX == 8
    want = ["Table 04", "Table 09", "Table 08", "Table 07", "Table 06",
            "Table 05", "Table 03", "Table 02"]
    assert nav.recent() == want
    assert nav._recent_root.text(0) == "Recent (8)"
    # A fresh navigator over the same settings file restores the list.
    again = Navigator(tables, nav_settings(tmp_path))
    assert again.recent() == want
    # Recent is a shortcut list: hidden while searching.
    again.search.setText("table")
    assert again._recent_root.isHidden()
    # Tables that no longer exist drop out on load.
    assert Navigator(tables[:3], nav_settings(tmp_path)).recent() == [
        "Table 02"]


def test_navigator_changed_dot(qapp, tmp_path):
    from openobd.tuningui import Navigator
    tables = tables_fixture()
    tables[1].values[0][0] = 99.0
    nav = Navigator(tables, nav_settings(tmp_path))
    assert nav.has_dot("Table 01") and not nav.has_dot("Table 00")
    tables[1].values[0][0] = tables[1].stock_values[0][0]
    nav.refresh_changed("Table 01")
    assert not nav.has_dot("Table 01")


def test_navigator_keyboard_esc_down_enter(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from openobd.tuningui import Navigator
    nav = Navigator(tables_fixture(), nav_settings(tmp_path))
    opened = []
    nav.table_activated.connect(opened.append)
    nav.search.setText("table 1")
    QTest.keyClick(nav.search, Qt.Key_Return)
    assert opened == ["Table 10"]
    QTest.keyClick(nav.search, Qt.Key_Escape)
    assert nav.search.text() == ""
    QTest.keyClick(nav.search, Qt.Key_Down)
    assert nav.tree.currentItem() is not None
    assert nav.tree.currentItem().data(0, Qt.UserRole)


def test_ctrl_f_focuses_search_and_esc_clears(win, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    win.main_tabs.setCurrentIndex(0)                 # Dashboard
    assert not win._find_shortcut.isEnabled()
    win.main_tabs.setCurrentIndex(1)                 # Tuning
    assert win._find_shortcut.isEnabled()
    assert win._find_shortcut.key().toString() == "Ctrl+F"
    win.activateWindow()
    win.view.setFocus()
    QTest.keyClick(win, Qt.Key_F, Qt.ControlModifier)
    qapp.processEvents()
    assert QApplication.focusWidget() is win.navigator.search
    QTest.keyClicks(win.navigator.search, "spark")
    assert win.navigator.search.text() == "spark"
    QTest.keyClick(win.navigator.search, Qt.Key_Escape)
    assert win.navigator.search.text() == ""


def test_breadcrumb_chips(win):
    t = first_2d_table(win)
    open_table(win, t.name)
    path = win.editor_crumb.path_text()
    assert path.endswith(t.name) and " › " in path
    texts = [c for c, _k in win.editor_crumb.chips()]
    if t.param_id:
        assert f"HPT id {t.param_id}" in texts
    assert any(x.endswith("changed") for x in texts)


# --------------------------------------------------------------------------- #
# Editor toolbar
# --------------------------------------------------------------------------- #
def test_editor_has_one_36px_toolbar_with_all_actions(win):
    from PySide6.QtWidgets import QToolBar
    tb = win.editor_toolbar
    assert tb.maximumHeight() == theme.TOOLBAR_HEIGHT == tb.minimumHeight()
    editor_tab = win.tune_tabs.widget(0)
    assert editor_tab.findChildren(QToolBar) == [tb]
    for key in ("set", "add", "sub", "mul", "div", "pct", "interp_h",
                "interp_v", "interp_2d", "smooth", "revert", "next", "copy",
                "paste", "heatmap"):
        assert key in win.editor_actions
    assert win.editor_actions["next"].shortcut().toString() == "F3"
    assert set(win.diff_actions) == {0, 1, 2}


def test_min_width_under_1000(win):
    assert win.tuning_page.minimumSizeHint().width() < 1000


def test_grid_keys_run_math_with_amount(win):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    t = open_table(win, first_2d_table(win).name)
    select_rect(win, 0, 0, 0, 0)
    before = t.values[0][0]
    win.amount_spin.setValue(2.0)
    QTest.keyClick(win.view, Qt.Key_Plus)
    assert t.values[0][0] == pytest.approx(before + 2.0)
    assert win.undo_stack.undoText() == "add"
    win.undo_stack.undo()
    assert t.values[0][0] == before


def test_smooth_is_3x3_mean_and_undoable(win):
    t = open_table(win, first_2d_table(win).name)
    for r in range(3):
        for c in range(3):
            t.values[r][c] = float(r * 3 + c)
    t.values[1][1] = 100.0
    snapshot = [row[:] for row in t.values]
    select_rect(win, 1, 1, 1, 1)
    win._smooth()
    neigh = [snapshot[r][c] for r in range(3) for c in range(3)]
    assert t.values[1][1] == pytest.approx(sum(neigh) / 9)
    assert win.undo_stack.undoText() == "smooth"
    win.undo_stack.undo()
    assert t.values == snapshot
    win.undo_stack.redo()
    assert t.values[1][1] == pytest.approx(sum(neigh) / 9)
    win.undo_stack.undo()
    restore_stock(win, t)


def test_smooth_edges_use_neighbours_inside_table():
    t = make_table()
    ch = editops.smooth(t, [(0, 0)])
    want = (0 + 10 + 5 + 15) / 4
    assert ch == {(0, 0): (0.0, want)}
    one_row = Table(name="r", unit="", x_axis=Axis("x", "", [1, 2, 3]),
                    y_axis=None, values=[[0.0, 3.0, 9.0]])
    assert editops.smooth(one_row, [(0, 1)]) == {(0, 1): (3.0, 4.0)}


def test_tsv_round_trip_clip_and_reject(win, qapp):
    from PySide6.QtWidgets import QApplication
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    for r in range(3):          # distinct values so every paste is visible
        for c in range(3):
            t.values[r][c] = 1000.0 + r * 10 + c
    win.current_model.refresh_all()
    stock = [row[:] for row in t.values]
    select_rect(win, 0, 0, 1, 1)
    win.copy_selection()
    tsv = QApplication.clipboard().text()
    assert tsv == editops.to_tsv(t, [(0, 0), (0, 1), (1, 0), (1, 1)])
    # Round trip: paste the copied block one cell down and right.
    select_rect(win, 1, 1, 1, 1)
    win.paste_selection()
    assert t.values[1][1] == stock[0][0] and t.values[2][2] == stock[1][1]
    assert win.undo_stack.undoText() == "paste"
    win.undo_stack.undo()
    assert t.values == stock
    # Clipping: a 2x3 block at the bottom-right corner keeps one cell.
    rl, cl = t.n_rows - 1, t.n_cols - 1
    QApplication.clipboard().setText("1\t2\t3\n4\t5\t6\n")
    select_rect(win, rl, cl, rl, cl)
    win.paste_selection()
    assert t.values[rl][cl] == 1.0
    assert sum(t.values[r][c] != stock[r][c]
               for r in range(t.n_rows) for c in range(t.n_cols)) <= 1
    assert editops.paste_clipped(t, rl, cl, [[1, 2, 3], [4, 5, 6]]) == 5
    assert "clipped" in win.statusBar().currentMessage()
    win.undo_stack.undo()
    assert t.values == stock
    # Non-numeric anywhere rejects the whole paste (nothing pushed).
    QApplication.clipboard().setText("RPM\t800\n1\t2\n")
    select_rect(win, 0, 0, 0, 0)
    n = win.undo_stack.count()
    win.paste_selection()
    assert win.undo_stack.count() == n and t.values == stock
    assert "rejected" in win.statusBar().currentMessage()
    restore_stock(win, t)


def test_parse_tsv_checked_reports_rejects():
    grid, rejects = editops.parse_tsv_checked("1\tx\n\t2.5\r\n")
    assert rejects == [(0, 1, "x")]
    assert grid[1][1] == 2.5


def test_next_delta_and_revert_selection(win):
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    t.values[2][1] += 5
    t.values[0][2] += 1
    win.current_model.refresh_all()
    select_rect(win, 0, 0, 0, 0)
    win._goto_next_changed()
    cur = win.view.currentIndex()
    assert (cur.row(), cur.column()) == (0, 2)
    win._goto_next_changed()
    cur = win.view.currentIndex()
    assert (cur.row(), cur.column()) == (2, 1)
    win._goto_next_changed()                     # wraps to the first
    cur = win.view.currentIndex()
    assert (cur.row(), cur.column()) == (0, 2)
    select_rect(win, 0, 0, t.n_rows - 1, t.n_cols - 1)
    win._revert_selected()
    assert editops.changed_cells(t) == []
    assert win.undo_stack.undoText() == "revert selection to stock"


# --------------------------------------------------------------------------- #
# Grid model: heatmap, diff mode, tooltips, corner, selection readout
# --------------------------------------------------------------------------- #
def rgb(brush):
    c = brush.color()
    return (c.red(), c.green(), c.blue())


def test_heatmap_colours_span_table_min_to_max(qapp):
    from PySide6.QtCore import Qt
    from openobd.model import CalTableModel
    m = CalTableModel(make_table())
    assert rgb(m.data(m.index(0, 0), Qt.BackgroundRole)) == theme.heat_rgb(0)
    assert rgb(m.data(m.index(2, 3), Qt.BackgroundRole)) == theme.heat_rgb(1)
    assert rgb(m.data(m.index(0, 0), Qt.ForegroundRole)) == \
        theme.contrast_text_rgb(theme.heat_rgb(0))
    m.set_heatmap(False)
    assert m.data(m.index(0, 0), Qt.BackgroundRole) is None


def test_axis_headers_and_corner(qapp):
    from PySide6.QtCore import Qt
    from openobd.model import CalTableModel
    m = CalTableModel(make_table())
    assert m.headerData(2, Qt.Horizontal, Qt.DisplayRole) == "2400 rpm"
    assert m.headerData(1, Qt.Vertical, Qt.DisplayRole) == "0.4 g/cyl"
    assert m.corner_text() == "Load \\ RPM"


def test_diff_mode_colours_text_and_tooltip(qapp):
    from PySide6.QtCore import Qt
    from openobd.model import CHANGED_ROLE, CalTableModel
    t = make_table()
    t.values[0][1] = 12.0          # +2 over stock 10 (+20%)
    t.values[1][1] = 14.0          # -1 under stock 15
    m = CalTableModel(t)
    with pytest.raises(ValueError):
        m.set_diff_mode(9)
    m.set_diff_mode(CalTableModel.DIFF_ABS)
    up, down, same = m.index(0, 1), m.index(1, 1), m.index(2, 2)
    assert m.data(up, Qt.DisplayRole) == "+2"
    assert m.data(down, Qt.DisplayRole) == "-1"
    assert rgb(m.data(up, Qt.BackgroundRole)) == theme.diff_rgb(1.0)
    assert rgb(m.data(down, Qt.BackgroundRole)) == theme.diff_rgb(-0.5)
    assert rgb(m.data(same, Qt.BackgroundRole)) == theme.diff_rgb(0.0)
    m.set_diff_mode(CalTableModel.DIFF_PCT)
    assert m.data(up, Qt.DisplayRole) == "+20.0%"
    tip = m.data(up, Qt.ToolTipRole)
    for part in ("X=1600 rpm", "Y=0.2 g/cyl", "value 12 deg", "stock 10",
                 "Δ +2 (+20.0%)"):
        assert part in tip, (part, tip)
    assert m.data(up, CHANGED_ROLE) is True
    assert m.data(same, CHANGED_ROLE) is False
    m.set_diff_mode(CalTableModel.DIFF_OFF)
    assert m.data(up, Qt.DisplayRole) == "12"


def test_changed_corner_triangle_painted_in_every_mode(qapp):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem, QTableView
    from openobd.model import CalTableModel, HeatmapDelegate
    t = make_table()
    t.values[0][0] = 3.0
    m = CalTableModel(t)
    view = QTableView()
    view.setModel(m)
    d = HeatmapDelegate(view)
    marker = theme.qcolor("changed-marker").rgb()
    for mode in (CalTableModel.DIFF_OFF, CalTableModel.DIFF_ABS,
                 CalTableModel.DIFF_PCT):
        m.set_diff_mode(mode)
        for idx, want in ((m.index(0, 0), True), (m.index(1, 1), False)):
            img = QImage(60, 24, QImage.Format_RGB32)
            img.fill(0)
            p = QPainter(img)
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, 0, 60, 24)
            opt.widget = view
            d.paint(p, opt, idx)
            p.end()
            assert (img.pixel(58, 1) == marker) is want, (mode, idx.row())


def test_selection_stats_and_readout(win):
    from openobd.tuningui import format_selection_stats
    t = make_table()
    t.values[0][0] = 4.0
    st = editops.selection_stats(t, [(0, 0), (0, 1), (0, 0)])
    assert st == {"n": 2, "min": 4.0, "max": 10.0, "mean": 7.0, "changed": 1}
    assert editops.selection_stats(t, [])["n"] == 0
    text = format_selection_stats(st, "deg")
    assert text.startswith("2 cells") and "mean 7 deg" in text
    assert "1 changed" in text
    assert format_selection_stats({"n": 0}) == "No selection"
    tbl = open_table(win, first_2d_table(win).name)
    select_rect(win, 0, 0, 1, 1)
    vals = [tbl.values[r][c] for r in (0, 1) for c in (0, 1)]
    want = format_selection_stats(
        editops.selection_stats(tbl, [(r, c) for r in (0, 1)
                                      for c in (0, 1)]), tbl.unit)
    assert win.selection_readout.full_text() == want
    assert want.startswith("4 cells") and len(vals) == 4


def test_overlay_panel_state_persists(win):
    from openobd.app import OVERLAY_PANEL_KEY
    p = win.overlay_panel
    p.set_expanded(True)
    assert str(win.settings.value(OVERLAY_PANEL_KEY)) == "true"
    assert p.frame.isVisibleTo(p)
    p.set_expanded(False)
    assert str(win.settings.value(OVERLAY_PANEL_KEY)) == "false"
    assert not p.frame.isVisibleTo(p)


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #
def test_scalar_edit_validation_undo_and_delta_chip(win):
    i = next(k for k, s in enumerate(win.cal.scalars)
             if s.stock_value is not None and s.value == s.stock_value)
    s = win.cal.scalars[i]
    stock = s.value
    n = win.undo_stack.count()
    win._on_scalar_edit(i, "abc")
    win._on_scalar_edit(i, "inf")
    assert win.undo_stack.count() == n and s.value == stock
    assert win.scalars_view.value_text(i) == f"{stock:g}"
    win._on_scalar_edit(i, repr(stock + 1))
    assert s.value == stock + 1
    assert win.scalars_view.delta_text(i) == "+1"
    assert win.undo_stack.undoText() == f"edit {s.name}"
    win.undo_stack.undo()
    assert s.value == stock and win.scalars_view.delta_text(i) == ""
    win.undo_stack.redo()
    assert s.value == stock + 1
    win.undo_stack.undo()
    assert s.value == stock


def test_scalars_grouped_searchable_with_safety_banner(win):
    sv = win.scalars_view
    total = sv.visible_scalar_count()
    assert total == len(win.cal.scalars)
    cats = sv.categories()
    assert len(cats) > 1
    win.scalar_category.setCurrentIndex(1)
    assert 0 < sv.visible_scalar_count() < total
    assert win.scalars_crumb.path_text().endswith(cats[0])
    win.scalar_category.setCurrentIndex(0)
    win.scalar_search.setText("zzzz-no-such-parameter")
    assert sv.visible_scalar_count() == 0
    win.scalar_search.setText(win.cal.scalars[0].name)
    assert sv.visible_scalar_count() >= 1
    win.scalar_search.setText("")
    assert sv.visible_scalar_count() == total
    assert win.safety_banner.objectName() == "safetyBanner"


# --------------------------------------------------------------------------- #
# Log Analysis + consistency
# --------------------------------------------------------------------------- #
def test_channel_tree_groups_and_cursor_readout(qapp):
    from openobd.tuningui import ChannelTree, CursorReadout, channel_group
    tree = ChannelTree()
    keys = ["rpm", "stft", "spark", "vss", "ect", "mystery"]
    tree.populate(keys, ["rpm", "spark"])
    groups = [tree.topLevelItem(i).text(0)
              for i in range(tree.topLevelItemCount())]
    assert groups == ["Engine (1)", "Fuel (1)", "Spark (1)", "Trans (1)",
                      "Sensors (1)", "Other (1)"]
    assert channel_group("mystery") == "Other"
    assert tree.checked_channels() == ["rpm", "spark"]
    tree.set_checked("vss", True)
    assert tree.checked_channels() == ["rpm", "spark", "vss"]
    ro = CursorReadout()
    ro.set_channels(["rpm"])
    assert ro.value("rpm") == "—" and ro.value("t") == "—"
    ro.show_values(1.25, [("rpm", 2400.0)])
    assert ro.value("t") == "1.25 s" and ro.value("rpm") == "2400"


def test_log_tab_empty_state_and_shared_chrome(win):
    from PySide6.QtWidgets import QToolBar
    from openobd.tuningui import Breadcrumb
    if win.log is None:
        assert win.plot_stack.currentWidget() is win.plot_empty
        assert win.log_crumb.path_text().endswith("No log loaded")
    for i in range(3):
        tab = win.tune_tabs.widget(i)
        bars = tab.findChildren(QToolBar)
        crumbs = tab.findChildren(Breadcrumb)
        assert len(bars) == 1 and len(crumbs) == 1
        assert bars[0].maximumHeight() == theme.TOOLBAR_HEIGHT
        assert crumbs[0].maximumHeight() == theme.BREADCRUMB_HEIGHT
