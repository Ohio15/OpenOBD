"""Headless tests for the editor operations (selection math, interp, TSV)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.calspec import Axis, Table
from openobd import editops


def make_table():
    return Table(
        name="t", unit="deg",
        x_axis=Axis("x", "", [1, 2, 3, 4]),
        y_axis=Axis("y", "", [10, 20, 30]),
        values=[[0.0, 10.0, 20.0, 30.0],
                [5.0, 15.0, 25.0, 35.0],
                [9.0, 19.0, 29.0, 39.0]],
        stock_values=[[0.0, 10.0, 20.0, 30.0],
                      [5.0, 15.0, 25.0, 35.0],
                      [9.0, 19.0, 29.0, 39.0]],
    )


def apply(table, changes):
    for (r, c), (_old, new) in changes.items():
        table.values[r][c] = new


def test_math_ops():
    t = make_table()
    ch = editops.apply_math(t, [(0, 0), (0, 1)], "add", 2.0)
    assert ch == {(0, 0): (0.0, 2.0), (0, 1): (10.0, 12.0)}
    ch = editops.apply_math(t, [(1, 1)], "mul", 2.0)
    assert ch[(1, 1)] == (15.0, 30.0)
    ch = editops.apply_math(t, [(2, 0)], "pct", 100.0)
    assert ch[(2, 0)] == (9.0, 18.0)
    ch = editops.apply_math(t, [(0, 0)], "set", 7.0)
    assert ch[(0, 0)] == (0.0, 7.0)
    # no-op values are omitted (nothing to undo)
    assert editops.apply_math(t, [(0, 0)], "add", 0.0) == {}


def test_interpolate_h_v():
    t = make_table()
    t.values[0] = [0.0, 99.0, 99.0, 30.0]
    ch = editops.interpolate(t, [(0, 0), (0, 3)], "h")
    apply(t, ch)
    assert t.values[0] == [0.0, 10.0, 20.0, 30.0]

    t.values[1][1] = 99.0
    ch = editops.interpolate(t, [(0, 1), (2, 1)], "v")
    apply(t, ch)
    assert t.values[1][1] == (10.0 + 19.0) / 2


def test_interpolate_2d_bilinear():
    t = make_table()
    for r in range(3):
        for c in range(4):
            t.values[r][c] = 99.0
    t.values[0][0], t.values[0][3] = 0.0, 30.0
    t.values[2][0], t.values[2][3] = 9.0, 39.0
    cells = [(r, c) for r in range(3) for c in range(4)]
    apply(t, editops.interpolate(t, cells, "2d"))
    # fx=1/3, fy=1/2: 0*(2/3)(1/2) + 30*(1/3)(1/2) + 9*(2/3)(1/2) + 39*(1/3)(1/2)
    assert abs(t.values[1][1] - 14.5) < 1e-9
    assert t.values[0][3] == 30.0  # corners untouched


def test_revert_and_changed():
    t = make_table()
    t.values[0][0] = 42.0
    t.values[2][3] = 43.0
    assert set(editops.changed_cells(t)) == {(0, 0), (2, 3)}
    apply(t, editops.revert_cells(t, editops.all_cells(t)))
    assert editops.changed_cells(t) == []


def test_tsv_round_trip():
    t = make_table()
    cells = [(0, 1), (0, 2), (1, 1), (1, 2)]
    tsv = editops.to_tsv(t, cells)
    assert tsv == "10\t20\n15\t25"
    grid = editops.parse_tsv(tsv)
    assert grid == [[10.0, 20.0], [15.0, 25.0]]
    # ragged selection -> blanks preserved, paste skips them
    tsv2 = editops.to_tsv(t, [(0, 1), (1, 2)])
    assert tsv2 == "10\t\n\t25"
    grid2 = editops.parse_tsv(tsv2)
    ch = editops.paste_grid(t, 2, 2, grid2)
    assert ch == {(2, 2): (29.0, 10.0)}  # (2,3) blank skipped, row 3 clipped


def test_paste_clipping():
    t = make_table()
    grid = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    ch = editops.paste_grid(t, 2, 3, grid)  # bottom-right corner
    assert ch == {(2, 3): (39.0, 1.0)}


def test_parse_number_refuses_non_finite():
    assert editops.parse_number("1.5") == 1.5
    assert editops.parse_number(" -2 ") == -2.0
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "", "abc", None,
                float("nan"), float("inf")):
        assert editops.parse_number(bad) is None, bad


def test_parse_tsv_checked_rejects_nan_and_inf():
    grid, rejects = editops.parse_tsv_checked("1\tnan\n inf \t-inf\n")
    assert [(r, c) for r, c, _ in rejects] == [(0, 1), (1, 0), (1, 1)]
    # parse_tsv never yields a non-finite float (they read as blanks).
    assert grid == [[1.0, None]]
