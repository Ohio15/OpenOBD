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
    show_editor(win)                # copy / paste act on the Editor only
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


# --------------------------------------------------------------------------- #
# Review round 1 / gate pass 1 regressions
# --------------------------------------------------------------------------- #
def show_editor(win):
    win.main_tabs.setCurrentWidget(win.tuning_page)
    win.tune_tabs.setCurrentWidget(win.editor_tab)


def test_paste_and_setdata_refuse_non_finite(win, qapp):
    from PySide6.QtWidgets import QApplication
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    before = [row[:] for row in t.values]
    for bad in ("nan", "inf", "-inf"):
        QApplication.clipboard().setText(f"1\t{bad}\n2\t3\n")
        select_rect(win, 0, 0, 0, 0)
        n = win.undo_stack.count()
        win.paste_selection()
        assert win.undo_stack.count() == n, bad
        assert t.values == before, bad
        msg = win.statusBar().currentMessage()
        assert "rejected" in msg and "finite" in msg, msg
    m = win.current_model
    n = win.undo_stack.count()
    for bad in ("inf", "-inf", "nan"):
        assert m.setData(m.index(0, 0), bad) is False
    assert t.values == before and win.undo_stack.count() == n


def test_overlay_grids_built_once_per_sweep(qapp):
    from PySide6.QtCore import Qt
    from openobd.logbin import CellBin, Overlay
    from openobd.model import CalTableModel
    n = 16
    vals = [[float(r * n + c) for c in range(n)] for r in range(n)]
    t = Table(name="Sweep", unit="", category="X",
              x_axis=Axis("x", "", list(range(n))),
              y_axis=Axis("y", "", list(range(n))),
              values=vals, stock_values=[row[:] for row in vals])
    bins = [[CellBin() for _ in range(n)] for _ in range(n)]
    for r in range(n):
        for c in range(0, n, 3):
            bins[r][c].add(float(r + c))
    ov = Overlay(n, n, bins, n * 6, "afr")
    calls = {"count": 0, "mean": 0}
    real_count, real_mean = ov.count_grid, ov.mean_grid

    def count_grid():
        calls["count"] += 1
        return real_count()

    def mean_grid():
        calls["mean"] += 1
        return real_mean()
    ov.count_grid, ov.mean_grid = count_grid, mean_grid
    m = CalTableModel(t)
    roles = (Qt.DisplayRole, Qt.BackgroundRole, Qt.ForegroundRole,
             Qt.ToolTipRole)
    for mode in (CalTableModel.OVERLAY_COUNT, CalTableModel.OVERLAY_MEAN):
        m.set_overlay(ov, mode)
        calls.update(count=0, mean=0)
        for r in range(n):
            for c in range(n):
                for role in roles:
                    m.data(m.index(r, c), role)
        assert calls == {"count": 1, "mean": 1}, (mode, calls)
    # An edit notification drops the cache like _stats.
    m.refresh_all()
    calls.update(count=0, mean=0)
    m.data(m.index(0, 0), Qt.BackgroundRole)
    m.data(m.index(1, 1), Qt.BackgroundRole)
    assert calls == {"count": 1, "mean": 1}


def test_clipboard_actions_gated_to_editor(win, qapp):
    from PySide6.QtWidgets import QApplication
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    assert win.act_paste.isEnabled() and win.act_copy.isEnabled()
    before = [row[:] for row in t.values]
    QApplication.clipboard().setText("1234\t5678\n")
    select_rect(win, 0, 0, 0, 0)
    for switch in (lambda: win.tune_tabs.setCurrentIndex(1),     # Scalars
                   lambda: win.tune_tabs.setCurrentIndex(2),     # Log
                   lambda: win.main_tabs.setCurrentIndex(0)):    # Dashboard
        show_editor(win)
        switch()
        assert not win.act_paste.isEnabled()
        assert not win.act_copy.isEnabled()
        n = win.undo_stack.count()
        win.act_paste.trigger()
        win.paste_selection()            # the explicit in-slot check
        assert t.values == before and win.undo_stack.count() == n
        assert "refused" in win.statusBar().currentMessage()
    show_editor(win)
    assert win.act_paste.isEnabled()
    n = win.undo_stack.index()
    win.act_paste.trigger()
    assert t.values[0][0] == 1234.0 and win.undo_stack.index() == n + 1
    assert win.undo_stack.undoText() == "paste"
    win.undo_stack.undo()
    assert t.values == before


def test_dashboard_shortcuts_do_not_leak_into_item_views(win, qapp):
    """R / Ctrl+R / M / Space / P are Dashboard shortcuts; typed into the
    calibration grid or the scalars tree they must not record/mark/pause."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    hits = []
    slots = []
    for sc in win.dashboard._shortcuts:
        slot = (lambda k=sc.key().toString(): hits.append(k))
        sc.activated.connect(slot)
        slots.append((sc, slot))
    try:
        win.activateWindow()
        show_editor(win)
        t = open_table(win, first_2d_table(win).name)
        restore_stock(win, t)
        before = [row[:] for row in t.values]
        for tab, view in ((0, win.view), (1, win.scalars_view.view)):
            win.tune_tabs.setCurrentIndex(tab)
            qapp.processEvents()
            view.setFocus()
            for key in (Qt.Key_R, Qt.Key_M, Qt.Key_Space, Qt.Key_P):
                QTest.keyClick(view, key)
                fw = QApplication.focusWidget()
                if fw is not None and fw is not view:   # cell editor opened
                    QTest.keyClick(fw, Qt.Key_Escape)
            QTest.keyClick(view, Qt.Key_R, Qt.ControlModifier)
            qapp.processEvents()
            assert hits == [], (tab, hits)
        assert t.values == before
        # Positive control: the same shortcuts are live on the Dashboard,
        # so the empty result above is not a dead harness.
        win.main_tabs.setCurrentWidget(win.dashboard)
        qapp.processEvents()
        win.dashboard.setFocus()
        QTest.keyClick(win, Qt.Key_M)
        qapp.processEvents()
        assert hits == ["M"]
    finally:
        for sc, slot in slots:
            sc.activated.disconnect(slot)
        show_editor(win)


def _fresh_window(qapp, tmp_path, stem):
    from openobd.app import MainWindow, load_default_cal
    from openobd.calspec import Calibration
    cal, _ = load_default_cal()
    path = str(tmp_path / f"{stem}.cal.json")
    cal.save(path)
    w = MainWindow(Calibration.load(path), path)
    w.show()
    qapp.processEvents()
    return w, path


def _close_window(w):
    w.dirty = False
    w._shutdown_pages()
    w.close()


def _first_value(path, name):
    from openobd.calspec import Calibration
    return next(t for t in Calibration.load(path).tables
                if t.name == name).values[0][0]


def test_dirty_follows_undo_clean_index(qapp, tmp_path):
    w, _path = _fresh_window(qapp, tmp_path, "dirty")
    try:
        show_editor(w)
        m = w.current_model
        v0 = m.table.values[0][0]
        assert w.dirty is False
        m.setData(m.index(0, 0), str(v0 + 1))
        assert w.dirty is True and w.windowTitle().endswith("*")
        w.undo_stack.undo()
        assert w.dirty is False and not w.windowTitle().endswith("*")
        m.setData(m.index(0, 0), str(v0 + 2))
        w.save_cal()
        assert w.dirty is False
        m.setData(m.index(0, 0), str(v0 + 3))
        assert w.dirty is True
        w.undo_stack.undo()                 # back to the saved state
        assert w.dirty is False
        w.undo_stack.undo()                 # past it: differs from disk
        assert w.dirty is True
    finally:
        _close_window(w)


def test_grid_math_key_ignores_auto_repeat(win, qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    before = [row[:] for row in t.values]
    win.amount_spin.setValue(1)
    select_rect(win, 0, 0, 0, 0)
    n = win.undo_stack.count()
    rep = QKeyEvent(QEvent.KeyPress, Qt.Key_Plus, Qt.NoModifier, "+", True)
    assert rep.isAutoRepeat()
    assert win.eventFilter(win.view, rep) is True     # consumed, no-op
    assert win.undo_stack.count() == n and t.values == before
    n = win.undo_stack.index()
    first = QKeyEvent(QEvent.KeyPress, Qt.Key_Plus, Qt.NoModifier, "+")
    assert win.eventFilter(win.view, first) is True
    assert win.undo_stack.index() == n + 1
    win.undo_stack.undo()
    assert t.values == before


def test_click_recent_item_defers_rebuild(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from openobd.tuningui import Navigator
    nav = Navigator(tables_fixture(), nav_settings(tmp_path))
    nav.resize(320, 600)
    for name in ("Table 01", "Table 02", "Table 03"):
        nav.push_recent(name)
    nav.show()
    nav._recent_root.setExpanded(True)
    qapp.processEvents()
    opened = []
    nav.table_activated.connect(opened.append)
    root = nav._recent_root
    names = [root.child(i).data(0, Qt.UserRole)
             for i in range(root.childCount())]
    assert names == ["Table 03", "Table 02", "Table 01"]
    leaf = root.child(2)
    nav.tree.scrollToItem(leaf)
    qapp.processEvents()
    rect = nav.tree.visualItemRect(leaf)
    assert rect.isValid()
    QTest.mouseClick(nav.tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     rect.center())
    assert opened == ["Table 01"]
    assert nav.recent()[0] == "Table 01"
    assert nav._rebuild_pending          # not rebuilt inside the signal
    qapp.processEvents()
    assert not nav._rebuild_pending
    names = [root.child(i).data(0, Qt.UserRole)
             for i in range(root.childCount())]
    assert names == ["Table 01", "Table 03", "Table 02"]
    nav.close()


def test_table_stats_tolerate_stock_shape_mismatch(qapp):
    from PySide6.QtCore import Qt
    from openobd.model import CalTableModel
    t = make_table()
    t.stock_values = [row[:2] for row in t.stock_values[:2]]   # 2x2 of 3x4
    m = CalTableModel(t)
    m.set_diff_mode(CalTableModel.DIFF_ABS)
    t.values[0][0] += 4.0
    m.refresh_all()
    for r in range(t.n_rows):
        for c in range(t.n_cols):
            m.data(m.index(r, c), Qt.BackgroundRole)
    assert m._table_stats()["max_abs"] == 4.0
    assert m.delta_text(2, 3) == "n/a"


def test_open_cal_saves_old_file_not_new(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    app = QApplication.instance()
    prev = getattr(app, "_openobd_main", None)
    try:
        for answer in (QMessageBox.Save, QMessageBox.Discard):
            tag = "save" if answer == QMessageBox.Save else "discard"
            w, a_path = _fresh_window(qapp, tmp_path, f"a_{tag}")
            b_path = str(tmp_path / f"b_{tag}.cal.json")
            from openobd.calspec import Calibration
            Calibration.load(a_path).save(b_path)
            new = None
            try:
                show_editor(w)
                m = w.current_model
                name = m.table.name
                v0 = m.table.values[0][0]
                m.setData(m.index(0, 0), str(v0 + 7))
                assert w.dirty
                monkeypatch.setattr(
                    QFileDialog, "getOpenFileName",
                    staticmethod(lambda *a, **k: (b_path, "")))
                asked = []

                def question(*_a, **_k):
                    asked.append(1)
                    return answer
                monkeypatch.setattr(QMessageBox, "question",
                                    staticmethod(question))
                w.open_cal()
                qapp.processEvents()
                new = app._openobd_main
                assert new is not w and new.path == b_path
                assert asked == [1]                 # asked exactly once
                assert w.path == a_path and not w.isVisible()
                want_a = v0 + 7 if answer == QMessageBox.Save else v0
                assert _first_value(a_path, name) == want_a
                assert _first_value(b_path, name) == v0   # new file untouched
            finally:
                if new is not None and new is not w:
                    _close_window(new)
                if w.isVisible():
                    _close_window(w)
    finally:
        app._openobd_main = prev


def test_no_stock_table_is_a_distinct_state(win, qapp, tmp_path):
    from openobd.model import CalTableModel
    from openobd.tuningui import Navigator
    tables = tables_fixture()
    tables[2].stock_values = None
    tables[1].values[0][0] = 99.0
    nav = Navigator(tables, nav_settings(tmp_path))
    assert nav.changed_state("Table 02") == Navigator.NO_STOCK
    assert nav.changed_state("Table 01") == Navigator.CHANGED
    assert nav.changed_state("Table 00") == Navigator.UNCHANGED
    assert not nav.has_dot("Table 02")
    assert "No stock baseline" in nav._leaves["Table 02"][0].toolTip(0)
    # The icon itself is three-valued, not only the tooltip.
    assert nav.leaf_state("Table 02") == Navigator.NO_STOCK
    assert nav.leaf_state("Table 01") == Navigator.CHANGED
    assert nav.leaf_state("Table 00") == Navigator.UNCHANGED
    key = {n: nav._leaves[n][0].icon(0).cacheKey()
           for n in ("Table 00", "Table 01", "Table 02")}
    assert len(set(key.values())) == 3
    assert key["Table 02"] == nav._no_stock.cacheKey()
    nav.refresh_changed("Table 02")   # refresh uses the same mapping
    assert (nav._leaves["Table 02"][0].icon(0).cacheKey()
            == nav._no_stock.cacheKey())
    assert nav.leaf_state("Table 02") == Navigator.NO_STOCK
    # Breadcrumb chip and the diff text in the window.
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    stock = t.stock_values
    try:
        t.stock_values = None
        win._update_table_info()
        chips = win.editor_crumb.chips()
        assert ("no stock baseline", "muted") in chips
        assert not any(text.endswith("changed") for text, _ in chips)
        m = win.current_model
        m.set_diff_mode(CalTableModel.DIFF_ABS)
        assert m.delta_text(0, 0) == "n/a"
        assert m.data(m.index(0, 0)) == "n/a"
    finally:
        win.current_model.set_diff_mode(CalTableModel.DIFF_OFF)
        t.stock_values = stock
        win._update_table_info()
    assert any(text.endswith("changed") for text, _ in
               win.editor_crumb.chips())


# --------------------------------------------------------------------------- #
# Review round 2: finite chokepoint, no-stock readouts, interior blank rows
# --------------------------------------------------------------------------- #
def test_math_overflow_refused_at_chokepoint(win):
    import math
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    t.values[0][0] = 1.0 if t.values[0][0] == 0 else t.values[0][0]
    win.current_model.refresh_all()
    select_rect(win, 0, 0, 0, 0)
    win.amount_spin.setValue(1e9)
    refused = False
    for _ in range(40):          # 1e9 ** 40 overflows a double
        idx, n, v0 = (win.undo_stack.index(), win.undo_stack.count(),
                      t.values[0][0])
        win._math_op("mul")
        assert all(math.isfinite(v) for row in t.values for v in row)
        if "edit refused: non-finite result" in \
                win.statusBar().currentMessage():
            refused = True
            assert win.undo_stack.index() == idx
            assert win.undo_stack.count() == n
            assert t.values[0][0] == v0
            break
    assert refused
    while win.undo_stack.canUndo() and win.undo_stack.undoText() == "multiply":
        win.undo_stack.undo()
    win.amount_spin.setValue(0.0)
    restore_stock(win, t)


def test_smooth_overflow_refused_at_chokepoint(win):
    import math
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    for r in range(3):
        for c in range(3):
            t.values[r][c] = 1.7e308   # neighbour sum overflows to inf
    win.current_model.refresh_all()
    snapshot = [row[:] for row in t.values]
    select_rect(win, 1, 1, 1, 1)
    n = win.undo_stack.count()
    win._smooth()
    assert win.undo_stack.count() == n
    assert t.values == snapshot
    assert all(math.isfinite(v) for row in t.values for v in row)
    assert "non-finite" in win.statusBar().currentMessage()
    restore_stock(win, t)


def test_bulk_and_scalar_commands_refuse_non_finite(win):
    from openobd.app import BulkEditCommand, ScalarEditCommand
    t = make_table()
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            BulkEditCommand(win, t, {(0, 0): (0.0, bad)}, "x")
        with pytest.raises(ValueError):
            ScalarEditCommand(win, 0, 0.0, bad)


def test_scalar_edit_1e999_refused(win):
    i = next(k for k, s in enumerate(win.cal.scalars)
             if s.stock_value is not None)
    s = win.cal.scalars[i]
    before = s.value
    n = win.undo_stack.count()
    win._on_scalar_edit(i, "1e999")
    assert win.undo_stack.count() == n and s.value == before


def test_selection_stats_no_stock_baseline():
    from openobd.tuningui import format_selection_stats
    t = make_table()
    t.stock_values = None
    st = editops.selection_stats(t, [(0, 0), (0, 1)])
    assert st["changed"] is None and st["n"] == 2
    assert editops.selection_stats(t, [])["changed"] is None
    text = format_selection_stats(st, "deg")
    assert "no stock baseline" in text and "changed" not in text
    assert "1 changed" in format_selection_stats(
        {"n": 1, "min": 1.0, "max": 1.0, "mean": 1.0, "changed": 1})


def test_scalars_crumb_no_stock_baseline(win):
    saved = [s.stock_value for s in win.cal.scalars]
    try:
        for s in win.cal.scalars:
            s.stock_value = None
        win._update_scalars_crumb()
        chips = win.scalars_crumb.chips()
        assert ("no stock baseline", "muted") in chips
        assert not any(text.endswith("changed") for text, _ in chips)
    finally:
        for s, v in zip(win.cal.scalars, saved):
            s.stock_value = v
        win._update_scalars_crumb()
    assert any(text.endswith("changed") for text, _ in
               win.scalars_crumb.chips())


def test_parse_tsv_keeps_interior_blank_rows(win, qapp):
    from PySide6.QtWidgets import QApplication
    assert editops.parse_tsv("1\n\n3") == [[1.0], [None], [3.0]]
    assert editops.parse_tsv("1\n\n3\n\n\n") == [[1.0], [None], [3.0]]
    grid, rejects = editops.parse_tsv_checked("1\n\n3")
    assert rejects == [] and grid == [[1.0], [None], [3.0]]
    show_editor(win)
    t = open_table(win, first_2d_table(win).name)
    restore_stock(win, t)
    mid = t.values[1][0]
    QApplication.clipboard().setText("1\n\n3")
    select_rect(win, 0, 0, 0, 0)
    win.paste_selection()
    assert t.values[0][0] == 1.0 and t.values[2][0] == 3.0
    assert t.values[1][0] == mid
    win.undo_stack.undo()
    restore_stock(win, t)


def test_runtime_tooltips_render_markup_literally(qapp, tmp_path):
    """DR-032 868953ce: table name/note, scalar note and the grid corner text
    come from calibration files; markup in them must not render as HTML."""
    from openobd.calspec import Scalar
    from openobd.tuningui import CalTableView, Navigator, ScalarsTree
    hostile = "<b>x</b>"
    tables = tables_fixture()
    tables[0].name = hostile
    tables[0].note = "<i>note</i>"
    nav = Navigator(tables, nav_settings(tmp_path))
    assert nav._leaves[hostile][0].toolTip(0) == (
        "<qt>&lt;b&gt;x&lt;/b&gt;<br>&lt;i&gt;note&lt;/i&gt;</qt>")
    tree = ScalarsTree([Scalar(name="s", value=1.0, note=hostile)])
    assert tree._rows[0][0].toolTip() == "<qt>&lt;b&gt;x&lt;/b&gt;</qt>"
    view = CalTableView()
    view.set_corner_text(hostile)
    assert view.corner_label.toolTip() == "<qt>&lt;b&gt;x&lt;/b&gt;</qt>"
    view.set_corner_text("")
    assert view.corner_label.toolTip() == ""


def test_plain_tooltip_keeps_line_breaks_and_escapes_literal_br(qapp,
                                                                tmp_path):
    """Review MINOR (DR-032 868953ce): <qt> is rich text, which collapses
    newlines; a multi-line calibration note keeps its lines as <br>, while a
    note containing the text "<br>" is escaped and shows as written."""
    from openobd.calspec import Scalar
    from openobd.tuningui import (
        CalTableView, Navigator, ScalarsTree, plain_tooltip)
    assert plain_tooltip("a\nb") == "<qt>a<br>b</qt>"
    assert plain_tooltip("a<br>b") == "<qt>a&lt;br&gt;b</qt>"
    tree = ScalarsTree([Scalar(name="s", value=1.0, note="line 1\nline 2")])
    tip = tree._rows[0][0].toolTip()
    assert tip == "<qt>line 1<br>line 2</qt>" and "\n" not in tip
    tables = tables_fixture()
    tables[0].note = "first\nsecond <br> third"
    nav = Navigator(tables, nav_settings(tmp_path))
    tip = nav._leaves[tables[0].name][0].toolTip(0)
    assert "\n" not in tip
    assert "first<br>second &lt;br&gt; third" in tip
    view = CalTableView()
    view.set_corner_text("top\nbottom")
    assert view.corner_label.toolTip() == "<qt>top<br>bottom</qt>"
