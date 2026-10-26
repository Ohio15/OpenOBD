"""
editops — headless editing operations for calibration tables.

The Qt editor calls these to implement the HP-Tuners staples: selection math
(set / add / multiply / percent-scale), interpolation (horizontal / vertical /
2-D bilinear), and TSV clipboard interchange with Excel.

Every mutating helper is *pure planning*: it returns a change map
{(row, col): (old, new)} and never touches the Table, so the GUI can wrap the
result in an undo command and the core stays unit-testable without Qt.
"""
from __future__ import annotations

import math
from typing import Optional

from .calspec import Table

Cells = list[tuple[int, int]]
Changes = dict[tuple[int, int], tuple[float, float]]


# --------------------------------------------------------------------------- #
# Selection math
# --------------------------------------------------------------------------- #
def apply_math(table: Table, cells: Cells, op: str, operand: float) -> Changes:
    """op: 'set' | 'add' | 'mul' | 'div' | 'pct' (pct: +N% -> value *
    (1 + N/100)). 'div' divides by operand (a true division, not a multiply
    by the reciprocal, so 30 / 3 is exactly 10); dividing by zero raises
    ZeroDivisionError before anything is planned."""
    if op == "div" and operand == 0:
        raise ZeroDivisionError("divide by zero")
    changes: Changes = {}
    for r, c in cells:
        old = table.values[r][c]
        if op == "set":
            new = operand
        elif op == "add":
            new = old + operand
        elif op == "mul":
            new = old * operand
        elif op == "div":
            new = old / operand
        elif op == "pct":
            new = old * (1.0 + operand / 100.0)
        else:
            raise ValueError(f"unknown op {op!r}")
        if new != old:
            changes[(r, c)] = (old, new)
    return changes


# --------------------------------------------------------------------------- #
# Interpolation
# --------------------------------------------------------------------------- #
def _lerp_run(vals: list[float]) -> list[float]:
    """Linear ramp between the first and last entry, endpoints kept."""
    n = len(vals)
    if n < 3:
        return list(vals)
    a, b = vals[0], vals[-1]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def interpolate(table: Table, cells: Cells, mode: str) -> Changes:
    """
    mode 'h': per selected row, ramp between the leftmost and rightmost
              selected cell in that row.
    mode 'v': per selected column, ramp between top and bottom selected cell.
    mode '2d': bilinear across the selection's bounding rectangle from its
              four corner values (all cells in the rect are written).
    """
    if not cells:
        return {}
    changes: Changes = {}
    if mode == "h":
        rows: dict[int, list[int]] = {}
        for r, c in cells:
            rows.setdefault(r, []).append(c)
        for r, cs in rows.items():
            cs = sorted(set(cs))
            span = list(range(cs[0], cs[-1] + 1))
            new_vals = _lerp_run([table.values[r][c] for c in span])
            for c, nv in zip(span, new_vals):
                old = table.values[r][c]
                if nv != old:
                    changes[(r, c)] = (old, nv)
    elif mode == "v":
        cols: dict[int, list[int]] = {}
        for r, c in cells:
            cols.setdefault(c, []).append(r)
        for c, rs in cols.items():
            rs = sorted(set(rs))
            span = list(range(rs[0], rs[-1] + 1))
            new_vals = _lerp_run([table.values[r][c] for r in span])
            for r, nv in zip(span, new_vals):
                old = table.values[r][c]
                if nv != old:
                    changes[(r, c)] = (old, nv)
    elif mode == "2d":
        r0 = min(r for r, _ in cells); r1 = max(r for r, _ in cells)
        c0 = min(c for _, c in cells); c1 = max(c for _, c in cells)
        if r1 == r0 or c1 == c0:  # degenerate rect -> fall back to 1-D
            return interpolate(table, cells, "h" if r1 == r0 else "v")
        v00 = table.values[r0][c0]; v01 = table.values[r0][c1]
        v10 = table.values[r1][c0]; v11 = table.values[r1][c1]
        for r in range(r0, r1 + 1):
            fy = (r - r0) / (r1 - r0)
            for c in range(c0, c1 + 1):
                fx = (c - c0) / (c1 - c0)
                nv = (v00 * (1 - fx) * (1 - fy) + v01 * fx * (1 - fy)
                      + v10 * (1 - fx) * fy + v11 * fx * fy)
                old = table.values[r][c]
                if nv != old:
                    changes[(r, c)] = (old, nv)
    else:
        raise ValueError(f"unknown interpolate mode {mode!r}")
    return changes


# --------------------------------------------------------------------------- #
# Smoothing
# --------------------------------------------------------------------------- #
def smooth(table: Table, cells: Cells) -> Changes:
    """3x3 mean over the selection: every selected cell becomes the mean of
    itself and its (up to) 8 neighbours inside the table, all read from the
    values BEFORE smoothing (one pass, no cascade through cells already
    written). Edge cells average the neighbours that exist; a 1-row table
    therefore smooths along its row only (1x3)."""
    changes: Changes = {}
    n_rows, n_cols = table.n_rows, table.n_cols
    for r, c in sorted(set(cells)):
        acc = 0.0
        n = 0
        for rr in range(max(0, r - 1), min(n_rows, r + 2)):
            for cc in range(max(0, c - 1), min(n_cols, c + 2)):
                acc += table.values[rr][cc]
                n += 1
        new = acc / n
        old = table.values[r][c]
        if new != old:
            changes[(r, c)] = (old, new)
    return changes


# --------------------------------------------------------------------------- #
# Selection statistics (the editor's readout strip)
# --------------------------------------------------------------------------- #
def pct_delta(value: float, stock: float) -> Optional[float]:
    """(value - stock) / |stock| * 100; None when stock is 0 and the value
    moved (no meaningful percentage), 0.0 when both are 0."""
    if stock == 0:
        return 0.0 if value == 0 else None
    return (value - stock) / abs(stock) * 100.0


def selection_stats(table: Table, cells: Cells) -> dict:
    """Statistics of the selected cells: n, min, max, mean (None for an
    empty selection) and how many of them differ from stock."""
    uniq = sorted(set(cells))
    vals = [table.values[r][c] for r, c in uniq]
    if not vals:
        return {"n": 0, "min": None, "max": None, "mean": None, "changed": 0}
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": sum(vals) / len(vals),
        "changed": sum(1 for r, c in uniq if table.cell_changed(r, c)),
    }


# --------------------------------------------------------------------------- #
# Revert to stock
# --------------------------------------------------------------------------- #
def revert_cells(table: Table, cells: Cells) -> Changes:
    if table.stock_values is None:
        return {}
    changes: Changes = {}
    for r, c in cells:
        old = table.values[r][c]
        new = table.stock_values[r][c]
        if new != old:
            changes[(r, c)] = (old, new)
    return changes


def all_cells(table: Table) -> Cells:
    return [(r, c) for r in range(table.n_rows) for c in range(table.n_cols)]


def changed_cells(table: Table) -> Cells:
    return [(r, c) for r in range(table.n_rows) for c in range(table.n_cols)
            if table.cell_changed(r, c)]


# --------------------------------------------------------------------------- #
# TSV clipboard interchange (Excel round-trip)
# --------------------------------------------------------------------------- #
def to_tsv(table: Table, cells: Cells) -> str:
    """Bounding-rect TSV of the selection; unselected cells in the rect are
    empty fields (so ragged selections round-trip as Excel blanks)."""
    if not cells:
        return ""
    sel = set(cells)
    r0 = min(r for r, _ in cells); r1 = max(r for r, _ in cells)
    c0 = min(c for _, c in cells); c1 = max(c for _, c in cells)
    lines = []
    for r in range(r0, r1 + 1):
        lines.append("\t".join(
            f"{table.values[r][c]:g}" if (r, c) in sel else ""
            for c in range(c0, c1 + 1)))
    return "\n".join(lines)


def parse_number(value) -> Optional[float]:
    """The one gate every edit sink uses: value as a finite float, or None.
    float() accepts 'nan', 'inf' and '-inf'; none of those is a calibration
    value (a NaN never compares equal, so it also defeats change tracking),
    so they are refused here exactly like non-numeric text."""
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


def parse_tsv(text: str) -> list[list[Optional[float]]]:
    """Clipboard TSV -> grid of floats (None for blanks, non-numeric and
    non-finite fields). Accepts trailing newline; tolerates CRLF."""
    grid: list[list[Optional[float]]] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "" and grid:  # trailing blank line
            continue
        row: list[Optional[float]] = []
        for cell in line.split("\t"):
            cell = cell.strip()
            row.append(parse_number(cell) if cell else None)
        grid.append(row)
    while grid and all(v is None for v in grid[-1]):
        grid.pop()
    return grid


def parse_tsv_checked(text: str) -> tuple[list[list[Optional[float]]],
                                          list[tuple[int, int, str]]]:
    """parse_tsv plus the cells it could not read: (grid, rejects) where
    rejects lists (row, col, text) for every non-blank field that is not a
    finite number (text, nan, inf, -inf). The editor refuses a paste with
    any reject rather than silently
    skipping it (an Excel header row would otherwise land as a partial,
    shifted paste)."""
    rejects: list[tuple[int, int, str]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for r, line in enumerate(lines):
        for c, cell in enumerate(line.split("\t")):
            cell = cell.strip()
            if cell and parse_number(cell) is None:
                rejects.append((r, c, cell))
    return parse_tsv(text), rejects


def paste_clipped(table: Table, r0: int, c0: int,
                  grid: list[list[Optional[float]]]) -> int:
    """How many non-blank grid cells fall outside the table when pasted at
    (r0, c0) (paste_grid drops them)."""
    out = 0
    for dr, row in enumerate(grid):
        for dc, v in enumerate(row):
            if v is not None and (r0 + dr >= table.n_rows
                                  or c0 + dc >= table.n_cols):
                out += 1
    return out


def paste_grid(table: Table, r0: int, c0: int,
               grid: list[list[Optional[float]]]) -> Changes:
    """Plan pasting grid with its top-left at (r0, c0); clipped to the table,
    None cells skipped (Excel blanks leave the target untouched)."""
    changes: Changes = {}
    for dr, row in enumerate(grid):
        r = r0 + dr
        if r >= table.n_rows:
            break
        for dc, v in enumerate(row):
            c = c0 + dc
            if c >= table.n_cols or v is None:
                continue
            old = table.values[r][c]
            if v != old:
                changes[(r, c)] = (old, v)
    return changes
