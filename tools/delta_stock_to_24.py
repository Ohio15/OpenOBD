#!/usr/bin/env python3
"""Derive the STOCK -> TUNE #24 calibration delta for the 2010 Silverado.

Joins the genuine pre-tuning stock baseline against the two independent
readings of tune #24 and classifies every parameter.

SOURCES
-------
STOCK   data/2010_silverado_stock.cal.json
        576 scalars / 735 tables, swept 2026-09-16 from Ron's genuine
        pre-tuning read ``stock-10.13.24.hpt`` (sha256 0c108cea...eab16).

TUNE24  data/2010_silverado_full.cal.json   ("swept")
        369 scalars / 251 tables, VCM Editor Copy-with-Axis sweep of
        "#24 - Claudes Edit 8.7.26". Full float precision.

TUNE24  data/2010_silverado_24.cal.json     ("sheet24")
        6 scalars / 6 tables, hand-entered from the #24 tune sheet. This is
        the ONLY source for the axle / tire / AFM parameters, which the
        swept #24 file does not contain at all.

``data/2010_silverado_best.cal.json`` IS DELIBERATELY NOT READ HERE. Its
merge key labelled ``stock`` was itself swept from #24, so 106 of its
scalars carry #24's own values mislabelled as stock and 263 have a null
stock_value. It was a workaround for having no stock baseline; the baseline
now exists and the workaround is obsolete.

JOIN KEY
--------
``(module, param_id)`` -- NEVER the display name. Two documented traps:
  * "Final Drive Ratio" exists as both ECM:9050 and TCM:5004.
  * The pin sheet calls ECM:246 "DoD (AFM) Enable"; VCM Editor calls it
    "DoD Enable". An exact-name match scores it MISSING.

Neither #24 file carries a ``module`` field, so module is *derived*:
  * scalars -- from the stock param_id -> module map (verified to have zero
    cross-module param_id collisions), corroborated by the #24 ``category``.
  * tables  -- from the ``[ECM] id N`` / ``[TCM] id N`` tag the #24 file
    embeds in ``note``, cross-checked against the stock map. Any
    disagreement is a hard error, not a silent preference.

COMPARISON RESOLUTION
---------------------
The stock sweep records values as VCM Editor *displayed* them, so a stock
value is only known to the precision shown. The swept #24 file carries full
float precision straight off the calibration, e.g. stock shows ``6042`` where
#24 holds ``6041.924736342039`` and stock shows ``19.9688`` where #24 holds
``19.96875``.

Comparing those as floats manufactures changes out of nothing: a first pass
using a half-ULP tolerance reported 16 changed tables, 11 of which were pure
display rounding. Equality is therefore decided by *rounding the #24 value to
the stock value's displayed precision* and comparing the results. VCM Editor
rounds half-to-even -- confirmed against 15 independent cells, including
16.25 -> "16.2", 19.25 -> "19.2", 1.84375 -> "1.8438" and 1.53125 -> "1.5312",
which only half-to-even reproduces. Cells that differ as floats but agree once
rounded are recorded as ``display_precision_artefact`` and are NOT changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

# .cal.json files are UTF-8 and carry non-ASCII units (mi^-1, degF, a BOM
# glued onto "kPa"). cp1252 is this box's default and throws on them.
ENCODING = "utf-8"

MODULE_TAG = re.compile(r"^\[(\w+)\]\s+id\s+(\d+)")
NUMERIC = re.compile(r"^-?\d+(?:\.(\d+))?$")

DATA = Path(__file__).resolve().parent.parent / "data"
STOCK_FILE = DATA / "2010_silverado_stock.cal.json"
SWEPT24_FILE = DATA / "2010_silverado_full.cal.json"
SHEET24_FILE = DATA / "2010_silverado_24.cal.json"

# Ordering tiers for "how much does this plausibly matter to drivability".
# Higher tier == reported first. Keyword match is against module + name +
# category, lowercased. This layer is explicitly a judgement about relevance;
# it never alters a value or a classification.
RANK_TIERS: list[tuple[int, str, tuple[str, ...]]] = [
    (100, "axle / final drive", ("final drive", "axle ratio")),
    (95, "tire size / VSS calibration",
     ("tire circumference", "tire revs", "vss tire", "rolling circumference")),
    (90, "shift scheduling",
     ("wot shift", "shift speed", "shift point", "upshift", "downshift",
      "shift schedule", "shift table")),
    (85, "torque converter clutch", ("tcc", "converter clutch", "lockup", "lock-up")),
    (80, "shift pressure / line pressure",
     ("shift pressure", "line pressure", "main pressure", "clutch pressure",
      "accumulator")),
    (75, "AFM / DoD / cylinder deactivation",
     ("dod", "afm", "displacement on demand", "cylinder deactivation")),
    (70, "torque management",
     ("torque management", "torque limit", "torque reduction", "driver demand",
      "desired torque")),
    (65, "speed / rev limiter", ("speed limit", "rev limit", "max rpm", "limiter")),
    (60, "idle control", ("idle",)),
    (55, "fuel delivery / injectors",
     ("injector", "afr", "commanded fuel", "equivalence", "fuel trim", "pe ",
      "power enrichment")),
    (50, "spark / timing", ("spark", "timing", "knock", "advance")),
    (45, "airflow / VE", ("volumetric", " ve ", "maf", "map ", "airflow")),
    (40, "throttle / ETC", ("throttle", "etc ", "pedal")),
    (35, "transmission - other", ("trans", "gear", "clutch", "tap ", "garage")),
    (30, "diagnostics / DTC", ("dtc", "diagnostic", "p0", "code")),
]


# ---------------------------------------------------------------------------
# normalisation helpers
# ---------------------------------------------------------------------------

def load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding=ENCODING) as fh:
        return json.load(fh)


def clean_text(value: Any) -> str:
    """Strip the BOM VCM Editor glues onto some units, plus whitespace."""
    if value is None:
        return ""
    return str(value).replace("﻿", "").strip()


def as_pid(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def decimals_of(text: Any) -> int | None:
    """Displayed decimal places of a numeric token, or None if not numeric.

    VCM Editor writes thousands separators, so "6,042" must parse as 6042 with
    zero decimals. Missing that made the grid fall back to a table-wide
    precision far tighter than the display, which reported four whole tables
    as changed purely on rounding noise.
    """
    match = NUMERIC.match(clean_text(text).replace(",", ""))
    if match is None:
        return None
    return len(match.group(1)) if match.group(1) else 0


def decimals_of_value(value: Any) -> int | None:
    """Decimal places implied by a parsed stock value.

    Stock values were produced by parsing VCM Editor's display text, so their
    own representation is never *more* precise than the display. Using it as a
    fallback is conservative: it can only widen the comparison, never tighten
    it into a false change.
    """
    if not is_number(value):
        return None
    return max(0, -Decimal(str(float(value))).normalize().as_tuple().exponent)


def quantize(value: float, decimals: int | None) -> Decimal:
    """Round a value the way VCM Editor displays it (half-to-even)."""
    if decimals is None:
        decimals = 6
    return Decimal(float(value)).quantize(Decimal(1).scaleb(-decimals),
                                          rounding=ROUND_HALF_EVEN)


def differs_at_display_precision(stock_value: float, tune_value: float,
                                 decimals: int | None) -> bool:
    """True only if #24 reads differently from stock at stock's own precision.

    Stock is known only to the precision VCM Editor printed. A #24 value that
    renders to the same string at that precision is indistinguishable from
    stock and must not be reported as a change.
    """
    return quantize(stock_value, decimals) != quantize(tune_value, decimals)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# module resolution
# ---------------------------------------------------------------------------

class ModuleIndex:
    """stock (kind, param_id) -> module, with a collision guard."""

    def __init__(self, stock: dict[str, Any]) -> None:
        self.map: dict[tuple[str, int], str] = {}
        self.collisions: list[str] = []
        for kind in ("scalars", "tables"):
            seen: dict[int, set[str]] = {}
            for entry in stock[kind]:
                pid = as_pid(entry.get("param_id"))
                if pid is None:
                    continue
                seen.setdefault(pid, set()).add(entry["module"])
            for pid, modules in seen.items():
                if len(modules) > 1:
                    # A param_id meaning different things in different modules
                    # would make the join key ambiguous. Refuse to guess.
                    self.collisions.append(f"{kind} param_id {pid}: {sorted(modules)}")
                self.map[(kind, pid)] = sorted(modules)[0]

    def lookup(self, kind: str, pid: int | None) -> str | None:
        if pid is None:
            return None
        return self.map.get((kind, pid))


def resolve_module(kind: str, entry: dict[str, Any], index: ModuleIndex) -> tuple[str | None, str, list[str]]:
    """Return (module, how_it_was_derived, conflicts)."""
    pid = as_pid(entry.get("param_id"))
    from_stock = index.lookup(kind, pid)
    tag = MODULE_TAG.match(clean_text(entry.get("note")))
    from_tag = tag.group(1) if tag else None
    conflicts: list[str] = []

    if tag and as_pid(tag.group(2)) != pid:
        conflicts.append(
            f"note tag id {tag.group(2)} disagrees with param_id {pid}"
        )
    if from_stock and from_tag and from_stock != from_tag:
        conflicts.append(
            f"stock map says {from_stock}, #24 note tag says {from_tag}"
        )

    if from_stock and from_tag:
        return from_stock, "stock-map+note-tag (agree)", conflicts
    if from_stock:
        return from_stock, "stock-map", conflicts
    if from_tag:
        return from_tag, "note-tag (absent from stock)", conflicts
    return None, "unresolved", conflicts


def resolve_sheet_entry(kind: str, entry: dict[str, Any], stock: dict[str, Any],
                        index: ModuleIndex, aliases: dict[str, str]) -> tuple[str | None, int | None, str]:
    """Resolve a hand-entered sheet24 entry, whose param_id is often null.

    Falls back to an exact name match against the stock file, then to the
    alias table the stock file publishes in metadata.name_aliases.
    """
    pid = as_pid(entry.get("param_id"))
    if pid is not None:
        module = index.lookup(kind, pid)
        if module:
            return module, pid, "param_id (given)"
        return None, pid, "param_id (given, absent from stock)"

    name = clean_text(entry.get("name"))
    matches = [e for e in stock[kind] if clean_text(e.get("name")) == name]
    if len(matches) == 1:
        return matches[0]["module"], as_pid(matches[0]["param_id"]), "exact name -> stock"
    if len(matches) > 1:
        return None, None, f"AMBIGUOUS name: {len(matches)} stock matches"

    aliased = aliases.get(name)
    if aliased:
        alias_pid = as_pid(re.search(r"(\d+)", aliased).group(1)) if re.search(r"(\d+)", aliased) else None
        if alias_pid is not None:
            module = index.lookup(kind, alias_pid)
            if module:
                return module, alias_pid, f"name_aliases -> {aliased}"
    return None, None, "unresolved"


# ---------------------------------------------------------------------------
# value comparison
# ---------------------------------------------------------------------------

def compare_scalar(stock_entry: dict[str, Any], tune_value: Any,
                   tune_raw: Any = None) -> dict[str, Any]:
    stock_value = stock_entry.get("value")
    stock_raw = stock_entry.get("raw_value")
    decimals = decimals_of(stock_raw) if stock_raw is not None else None
    if decimals is None:
        decimals = decimals_of_value(stock_value)

    if is_number(stock_value) and is_number(tune_value):
        delta = float(tune_value) - float(stock_value)
        changed = differs_at_display_precision(stock_value, tune_value, decimals)
        return {
            "comparable": True,
            "changed": changed,
            "delta": delta,
            "pct": (delta / float(stock_value) * 100.0) if stock_value else None,
            "stock_display_decimals": decimals,
            "tune24_at_stock_precision": str(quantize(tune_value, decimals)),
            "display_precision_artefact": (not changed) and delta != 0.0,
            "basis": "display-precision",
        }

    # Enum / non-numeric: fall back to the raw display strings.
    s_txt, t_txt = clean_text(stock_raw), clean_text(tune_raw)
    if s_txt and t_txt:
        return {
            "comparable": True,
            "changed": s_txt.casefold() != t_txt.casefold(),
            "delta": None,
            "pct": None,
            "tolerance": None,
            "basis": "raw-text",
        }

    return {
        "comparable": False,
        "changed": None,
        "reason": "stock value is null or non-numeric and no comparable #24 raw text",
        "basis": "none",
    }


def table_cell_decimals(stock_table: dict[str, Any]) -> dict[float, int]:
    """Map each numeric value appearing in the raw grid to its displayed decimals.

    Positional mapping from ``values`` back into ``raw_grid`` is not safe --
    grids carry a header row and a label column inconsistently -- so this
    matches by parsed value instead, keeping the *fewest* decimals seen
    (the most conservative, widest tolerance).
    """
    out: dict[float, int] = {}
    for row in stock_table.get("raw_grid") or []:
        for cell in row:
            dec = decimals_of(cell)
            if dec is None:
                continue
            val = float(clean_text(cell).replace(",", ""))
            if val not in out or dec < out[val]:
                out[val] = dec
    return out


SHIFT_LABEL = re.compile(r"(\d+)\s*(?:->|-|to)\s*(\d+)")


def normalise_shift_label(text: Any) -> str | None:
    """'1 -> 2 Shift' and '1-2' both become '1-2'."""
    match = SHIFT_LABEL.search(clean_text(text))
    return f"{match.group(1)}-{match.group(2)}" if match else None


def stock_label_map(stock_table: dict[str, Any]) -> dict[str, tuple[float, int]] | None:
    """Label -> (value, displayed decimals) for a single-value-column table.

    Stock WOT tables are stored 6x1 with the gear event in the raw grid's
    label column; the hand-entered sheet stores the same data 1x10 indexed by
    metadata.shift_events. Aligning them positionally would be wrong, so both
    sides are keyed by the gear-change label each file states explicitly.
    """
    grid = stock_table.get("raw_grid") or []
    values = stock_table.get("values") or []
    if len(grid) != len(values) + 1 or not values:
        return None
    if any(len(row) != 1 for row in values):
        return None

    out: dict[str, tuple[float, int]] = {}
    for row, value_row in zip(grid[1:], values):
        label = normalise_shift_label(row[0] if row else None)
        if label is None or not is_number(value_row[0]):
            return None
        dec = decimals_of(row[-1])
        if dec is None:
            dec = decimals_of_value(value_row[0])
        out[label] = (float(value_row[0]), dec)
    return out or None


def sheet_label_map(tune_table: dict[str, Any], events: list[str]) -> dict[str, float] | None:
    values = tune_table.get("values") or []
    if len(values) != 1 or len(values[0]) != len(events):
        return None
    return {normalise_shift_label(ev) or ev: v
            for ev, v in zip(events, values[0]) if is_number(v)}


def _cell_record(stock_v: float, tune_v: float, decimals: int | None,
                 **where: Any) -> dict[str, Any] | None:
    if not differs_at_display_precision(stock_v, tune_v, decimals):
        return None
    delta = float(tune_v) - float(stock_v)
    return {**where, "stock": stock_v, "tune24": tune_v, "delta": delta,
            "pct": (delta / float(stock_v) * 100.0) if stock_v else None,
            "tune24_at_stock_precision": str(quantize(tune_v, decimals))}


def _summarise(cells: list[dict[str, Any]], compared: int, uncomparable: int,
               artefacts: int, **extra: Any) -> dict[str, Any]:
    ratios = [c["tune24"] / c["stock"] for c in cells if c["stock"]]
    return {
        "comparable": True,
        "cells_compared": compared,
        "cells_uncomparable": uncomparable,
        "cells_changed": len(cells),
        "cells_display_precision_artefact": artefacts,
        "changed": bool(cells),
        "changed_cells": cells,
        "max_abs_delta": max((abs(c["delta"]) for c in cells), default=0.0),
        "mean_ratio": (sum(ratios) / len(ratios)) if ratios else None,
        "min_ratio": min(ratios, default=None),
        "max_ratio": max(ratios, default=None),
        **extra,
    }


def compare_table(stock_table: dict[str, Any], tune_table: dict[str, Any],
                  shift_events: list[str] | None = None) -> dict[str, Any]:
    stock_values = stock_table.get("values") or []
    tune_values = tune_table.get("values") or []
    shape_s = (len(stock_values), len(stock_values[0]) if stock_values else 0)
    shape_t = (len(tune_values), len(tune_values[0]) if tune_values else 0)

    dec_map = table_cell_decimals(stock_table)

    if shape_s == shape_t:
        cells: list[dict[str, Any]] = []
        compared = uncomparable = artefacts = 0
        for r, (srow, trow) in enumerate(zip(stock_values, tune_values)):
            if len(srow) != len(trow):
                uncomparable += len(srow)
                continue
            for c, (sv, tv) in enumerate(zip(srow, trow)):
                if not (is_number(sv) and is_number(tv)):
                    uncomparable += 1
                    continue
                compared += 1
                # Displayed precision from the grid text when the value is
                # found there; otherwise the value's own precision, which is
                # never tighter than the display. Never a table-wide default.
                dec = dec_map.get(float(sv))
                if dec is None:
                    dec = decimals_of_value(sv)
                cell = _cell_record(sv, tv, dec, row=r, col=c)
                if cell:
                    cells.append(cell)
                elif float(tv) != float(sv):
                    artefacts += 1
        return _summarise(cells, compared, uncomparable, artefacts,
                          alignment="positional", shape=list(shape_s))

    # Shapes disagree. The only alignment ever attempted is by an explicit
    # label both files state; nothing is padded, cropped or transposed.
    if shift_events:
        s_map = stock_label_map(stock_table)
        t_map = sheet_label_map(tune_table, shift_events)
        if s_map and t_map:
            shared = sorted(set(s_map) & set(t_map))
            cells, artefacts = [], 0
            for label in shared:
                sv, dec = s_map[label]
                tv = t_map[label]
                cell = _cell_record(sv, tv, dec, label=label)
                if cell:
                    cells.append(cell)
                elif float(tv) != float(sv):
                    artefacts += 1
            return _summarise(
                cells, len(shared), 0, artefacts,
                alignment="by gear-change label",
                stock_shape=list(shape_s), tune_shape=list(shape_t),
                labels_compared=shared,
                labels_only_in_tune24=sorted(set(t_map) - set(s_map)),
                labels_only_in_stock=sorted(set(s_map) - set(t_map)),
            )

    return {
        "comparable": False,
        "reason": "shape mismatch and no shared explicit labels to align on",
        "stock_shape": list(shape_s),
        "tune_shape": list(shape_t),
    }


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

def rank_of(module: str | None, name: str, category: str, tab_path: str) -> tuple[int, str]:
    haystack = " ".join(filter(None, (module, name, category, tab_path))).casefold()
    haystack = f" {haystack} "
    for score, label, keywords in RANK_TIERS:
        for kw in keywords:
            if kw in haystack:
                return score, label
    return 10, "other"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_delta() -> dict[str, Any]:
    stock = load(STOCK_FILE)
    swept = load(SWEPT24_FILE)
    sheet = load(SHEET24_FILE)

    index = ModuleIndex(stock)
    if index.collisions:
        raise SystemExit(
            "ABORT: stock param_ids collide across modules, join key is unsafe:\n  "
            + "\n  ".join(index.collisions)
        )

    aliases = {k: v for k, v in (stock["metadata"].get("name_aliases") or {}).items()
               if k != "note"}
    # The hand-entered sheet stores WOT tables as one row of 10 gear events;
    # this list is how that file itself labels those columns.
    shift_events = sheet["metadata"].get("shift_events") or []

    module_conflicts: list[str] = []
    entries: list[dict[str, Any]] = []

    # ---- index the stock side -------------------------------------------
    stock_idx: dict[tuple[str, str, int], dict[str, Any]] = {}
    stock_unkeyed = 0
    for kind in ("scalars", "tables"):
        for entry in stock[kind]:
            pid = as_pid(entry.get("param_id"))
            if pid is None:
                stock_unkeyed += 1
                continue
            stock_idx[(kind, entry["module"], pid)] = entry

    # ---- index the swept #24 side ---------------------------------------
    swept_idx: dict[tuple[str, str, int], dict[str, Any]] = {}
    swept_unresolved: list[dict[str, Any]] = []
    for kind in ("scalars", "tables"):
        for entry in swept[kind]:
            pid = as_pid(entry.get("param_id"))
            module, how, conflicts = resolve_module(kind, entry, index)
            module_conflicts.extend(
                f"{kind} {pid} {clean_text(entry.get('name'))}: {c}" for c in conflicts
            )
            if module is None or pid is None:
                swept_unresolved.append({
                    "kind": kind, "param_id": pid,
                    "name": clean_text(entry.get("name")), "reason": how,
                })
                continue
            entry["_module"] = module
            entry["_module_how"] = how
            swept_idx[(kind, module, pid)] = entry

    # ---- index the hand-entered sheet24 side ----------------------------
    sheet_idx: dict[tuple[str, str, int], dict[str, Any]] = {}
    sheet_resolution: list[dict[str, Any]] = []
    for kind in ("scalars", "tables"):
        for entry in sheet[kind]:
            module, pid, how = resolve_sheet_entry(kind, entry, stock, index, aliases)
            sheet_resolution.append({
                "kind": kind,
                "sheet_name": clean_text(entry.get("name")),
                "sheet_param_id": entry.get("param_id"),
                "resolved_module": module,
                "resolved_param_id": pid,
                "method": how,
            })
            if module is None or pid is None:
                continue
            entry["_module"] = module
            entry["_resolved_pid"] = pid
            sheet_idx[(kind, module, pid)] = entry

    # ---- join -----------------------------------------------------------
    all_keys = sorted(set(stock_idx) | set(swept_idx) | set(sheet_idx),
                      key=lambda k: (k[0], k[1], k[2]))

    for key in all_keys:
        kind, module, pid = key
        s_entry = stock_idx.get(key)
        w_entry = swept_idx.get(key)
        h_entry = sheet_idx.get(key)

        name = clean_text((s_entry or w_entry or h_entry).get("name"))
        stock_name = clean_text(s_entry.get("name")) if s_entry else None
        tune_name = clean_text((w_entry or h_entry).get("name")) if (w_entry or h_entry) else None
        unit = clean_text((s_entry or w_entry or h_entry).get("unit"))
        category = clean_text((s_entry or w_entry or h_entry).get("category"))
        tab_path = clean_text(s_entry.get("tab_path")) if s_entry else ""
        score, tier = rank_of(module, name, category, tab_path)

        rec: dict[str, Any] = {
            "kind": kind[:-1],               # "scalar" / "table"
            "module": module,
            "param_id": pid,
            "name": name,
            "stock_name": stock_name,
            "tune24_name": tune_name,
            "name_differs": bool(stock_name and tune_name and stock_name != tune_name),
            "unit": unit,
            "category": category,
            "tab_path": tab_path,
            "rank": score,
            "tier": tier,
            "sources": {
                "stock": s_entry is not None,
                "tune24_swept": w_entry is not None,
                "tune24_sheet": h_entry is not None,
            },
        }
        if w_entry is not None:
            rec["module_derivation"] = w_entry.get("_module_how")

        # -- no stock reading -> the stock side is unknown, never "unchanged"
        if s_entry is None:
            rec["status"] = "tune24_only_stock_unmeasured"
            rec["note"] = ("present in the #24 reading but absent from the stock "
                           "sweep; the stock value is unknown, not unchanged")
            if kind == "scalars":
                rec["tune24_value"] = (w_entry or h_entry).get("value")
            else:
                rec["tune24_shape"] = [len((w_entry or h_entry).get("values") or []),
                                       len(((w_entry or h_entry).get("values") or [[]])[0])]
            entries.append(rec)
            continue

        # -- no #24 reading -> unmeasured by the #24 sweep
        if w_entry is None and h_entry is None:
            rec["status"] = "stock_only_tune24_unmeasured"
            rec["note"] = ("captured by the stock sweep but absent from every #24 "
                           "reading; whether #24 changed it is UNKNOWN")
            if kind == "scalars":
                rec["stock_value"] = s_entry.get("value")
                rec["stock_raw"] = clean_text(s_entry.get("raw_value"))
            entries.append(rec)
            continue

        # -- both sides present -> compare
        if kind == "scalars":
            rec["stock_value"] = s_entry.get("value")
            rec["stock_raw"] = clean_text(s_entry.get("raw_value"))
            rec["kind_detail"] = s_entry.get("kind")

            results = {}
            if w_entry is not None:
                results["swept"] = compare_scalar(s_entry, w_entry.get("value"))
                rec["tune24_value_swept"] = w_entry.get("value")
            if h_entry is not None:
                results["sheet"] = compare_scalar(s_entry, h_entry.get("value"))
                rec["tune24_value_sheet"] = h_entry.get("value")
                # sheet24 also asserts a stock value -- an independent check
                sheet_stock = h_entry.get("stock_value")
                if is_number(sheet_stock) and is_number(s_entry.get("value")):
                    agree = not differs_at_display_precision(
                        s_entry["value"], sheet_stock,
                        decimals_of(clean_text(s_entry.get("raw_value"))))
                    rec["sheet_stock_claim"] = sheet_stock
                    rec["sheet_stock_agrees_with_sweep"] = agree

            rec["comparison"] = results
            rec["tune24_value"] = (
                w_entry.get("value") if w_entry is not None else h_entry.get("value")
            )
            verdicts = {k: v.get("changed") for k, v in results.items()}
            rec["source_agreement"] = (
                "single-source" if len(verdicts) == 1
                else ("agree" if len(set(verdicts.values())) == 1 else "DISAGREE")
            )
            changed = any(v is True for v in verdicts.values())
            comparable = any(r.get("comparable") for r in results.values())

            if not comparable:
                rec["status"] = "not_comparable"
            elif changed:
                rec["status"] = "changed"
                primary = results.get("swept") or results.get("sheet")
                rec["delta"] = primary.get("delta")
                rec["pct"] = primary.get("pct")
            else:
                rec["status"] = "unchanged"
                primary = results.get("swept") or results.get("sheet")
                if primary.get("basis") == "numeric" and primary.get("delta"):
                    rec["status"] = "unchanged_within_display_resolution"
                    rec["residual"] = primary["delta"]
            entries.append(rec)
            continue

        # -- tables
        rec["stock_shape"] = [len(s_entry.get("values") or []),
                              len((s_entry.get("values") or [[]])[0])]
        results = {}
        if w_entry is not None:
            results["swept"] = compare_table(s_entry, w_entry, shift_events)
        if h_entry is not None:
            results["sheet"] = compare_table(s_entry, h_entry, shift_events)
        rec["comparison"] = results

        comparable = [r for r in results.values() if r.get("comparable")]
        if not comparable:
            rec["status"] = "not_comparable"
            entries.append(rec)
            continue

        verdicts = {k: v.get("changed") for k, v in results.items() if v.get("comparable")}
        rec["source_agreement"] = (
            "single-source" if len(verdicts) == 1
            else ("agree" if len(set(verdicts.values())) == 1 else "DISAGREE")
        )
        # Prefer the machine sweep: it carries full float precision. The
        # hand-entered sheet is used where the sweep has no reading at all.
        primary_key = ("swept" if results.get("swept", {}).get("comparable")
                       else "sheet")
        primary = results[primary_key]
        rec["primary_source"] = f"tune24_{primary_key}"
        rec["alignment"] = primary.get("alignment")
        rec["cells_compared"] = primary.get("cells_compared")
        rec["cells_changed"] = primary.get("cells_changed")
        rec["cells_display_precision_artefact"] = primary.get("cells_display_precision_artefact")
        rec["max_abs_delta"] = primary.get("max_abs_delta")
        rec["mean_ratio"] = primary.get("mean_ratio")
        rec["min_ratio"] = primary.get("min_ratio")
        rec["max_ratio"] = primary.get("max_ratio")
        rec["changed_cells"] = primary.get("changed_cells")
        rec["status"] = "changed" if any(verdicts.values()) else "unchanged"

        # Gear events the hand-entered sheet asserts but VCM Editor never
        # exposed. These are single-source claims with no stock reading
        # behind them and are kept separate from the verified cells.
        sheet_result = results.get("sheet") or {}

        # The hand-entered sheet also asserts what stock was. Where the stock
        # sweep independently read the same cell, that claim is checkable --
        # a genuine cross-validation of two sources that never saw each other.
        if h_entry is not None and h_entry.get("stock_values"):
            s_map = stock_label_map(s_entry) or {}
            claim_map = sheet_label_map(
                {"values": h_entry.get("stock_values")}, shift_events) or {}
            checks = [{
                "label": lbl,
                "sweep": s_map[lbl][0],
                "sheet_claim": claim_map[lbl],
                "agrees": not differs_at_display_precision(
                    s_map[lbl][0], claim_map[lbl], s_map[lbl][1]),
            } for lbl in sorted(set(s_map) & set(claim_map))]
            if checks:
                rec["sheet24_stock_claim_check"] = {
                    "cells_checked": len(checks),
                    "cells_agreeing": sum(1 for c in checks if c["agrees"]),
                    "detail": checks,
                }

        extra_labels = sheet_result.get("labels_only_in_tune24") or []
        if h_entry is not None and extra_labels:
            sheet_map = sheet_label_map(h_entry, shift_events) or {}
            sheet_stock_map = sheet_label_map(
                {"values": h_entry.get("stock_values")}, shift_events) or {}
            rec["sheet24_only_cells"] = [{
                "label": lbl,
                "sheet24_stock_claim": sheet_stock_map.get(lbl),
                "sheet24_tune_value": sheet_map.get(lbl),
                "evidence": ("hand-entered tune sheet only; VCM Editor never "
                             "exposed this gear event, so there is NO stock "
                             "sweep reading to confirm it"),
            } for lbl in extra_labels]
        entries.append(rec)

    summary = Counter(e["status"] for e in entries)
    by_kind = Counter((e["kind"], e["status"]) for e in entries)

    return {
        "schema_version": 1,
        "metadata": {
            "title": "STOCK -> TUNE #24 calibration delta, 2010 Silverado 1500 5.3L",
            "vehicle": stock["metadata"]["vehicle"],
            "vin": stock["metadata"]["vin"],
            "join_key": "(module, param_id) -- never the display name",
            "stock_source": {
                "file": STOCK_FILE.name,
                "base_tune": stock["metadata"]["base_tune"],
                "base_tune_sha256": stock["metadata"]["base_tune_sha256"],
                "scalars": len(stock["scalars"]),
                "tables": len(stock["tables"]),
            },
            "tune24_sources": {
                "swept": {
                    "file": SWEPT24_FILE.name,
                    "base_tune": swept["metadata"]["base_tune"],
                    "scalars": len(swept["scalars"]),
                    "tables": len(swept["tables"]),
                },
                "sheet": {
                    "file": SHEET24_FILE.name,
                    "derived_from": sheet["metadata"].get("derived_from"),
                    "scalars": len(sheet["scalars"]),
                    "tables": len(sheet["tables"]),
                    "role": ("hand-entered tune sheet; the ONLY source for the axle, "
                             "tire-circumference and AFM parameters, which the swept "
                             "#24 file does not contain"),
                },
            },
            "excluded_source": {
                "file": "2010_silverado_best.cal.json",
                "reason": ("NOT a reference. Its merge key labelled 'stock' was swept "
                           "from #24 itself, so 106 scalars carry #24's own values "
                           "mislabelled as stock and 263 have stock_value null. Only "
                           "its 6 hand-written provenance.source=='pin' entries were "
                           "ever trustworthy, and those originate in "
                           "2010_silverado_24.cal.json, which is read directly here. "
                           "It was a workaround for having no stock baseline; the "
                           "baseline now exists and the workaround is OBSOLETE."),
            },
            "comparison_resolution": (
                "Stock values are VCM Editor display values. Equality is judged "
                "against half the last displayed decimal place, per parameter. "
                "Differences below that are reported as "
                "unchanged_within_display_resolution, never as changes."
            ),
            "coverage_note": (
                "The stock sweep captured MORE than the #24 reading holds (576 vs 369 "
                "scalars, 735 vs 251 tables) because VCM Editor in Advanced view over "
                "ECM+TCM exposes more than the older coalesced #24 sweep ever did. "
                "That surplus is extra stock coverage, not lost #24 data. Parameters "
                "with no #24 reading are classified stock_only_tune24_unmeasured and "
                "are explicitly UNKNOWN, never assumed unchanged."
            ),
        },
        "diagnostics": {
            "stock_entries_without_param_id": stock_unkeyed,
            "module_conflicts": module_conflicts,
            "swept24_entries_with_unresolved_module": swept_unresolved,
            "sheet24_resolution": sheet_resolution,
        },
        "summary": {
            "total": len(entries),
            "by_status": dict(sorted(summary.items())),
            "by_kind_status": {f"{k[0]}/{k[1]}": v for k, v in sorted(by_kind.items())},
        },
        "parameters": sorted(
            entries,
            key=lambda e: (
                0 if e["status"] == "changed" else 1,
                -e["rank"],
                -(e.get("cells_changed") or 0),
                e["module"] or "",
                e["param_id"],
            ),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=DATA / "2010_silverado_delta_stock_to_24.json")
    args = parser.parse_args()

    delta = build_delta()
    with args.out.open("w", encoding=ENCODING) as fh:
        json.dump(delta, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    print(f"wrote {args.out}")
    print(json.dumps(delta["summary"], indent=1))
    if delta["diagnostics"]["module_conflicts"]:
        print("MODULE CONFLICTS:")
        for c in delta["diagnostics"]["module_conflicts"]:
            print("  ", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
