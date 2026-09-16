#!/usr/bin/env python3
"""Assemble the genuine STOCK calibration baseline from the offline VCM Editor sweep.

WHY THIS EXISTS
---------------
data/2010_silverado_best.cal.json carries 375 scalars / 252 tables but only six
genuine stock baselines (the hand-written provenance.source == "pin" entries).
Its merge key labelled "stock" was actually swept from "#24 - Claudes Edit
8.7.26" -- the TUNED file -- so 106 scalars carry #24's own values mislabelled
as stock and 263 have stock_value: null. The real stock .hpt was never properly
swept. Nobody could say what tune #24 actually changed.

This script turns the sweep of the authentic stock .hpt into a cal.json in the
same shape as data/2010_silverado_full.cal.json (which is #24), so the two can
be diffed parameter-for-parameter.

HONESTY RULES (non-negotiable)
------------------------------
* Nothing is invented. A parameter that was not read is ABSENT from the output
  and counted as missing in the coverage report. It is never interpolated,
  defaulted, or carried over from #24.
* Matching is by (module, param_id), never by display name. Names repeat across
  modules ("Final Drive Ratio" exists as ECM 9050 and TCM 5004).
* All file IO is UTF-8 explicitly. These files carry non-ASCII units (deg, mi^-1)
  and cp1252 is the default on Ron's workstation, which throws.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# The six hand-verified stock baselines already in best.cal.json. These are the
# only independent check on whether the sweep read the right cells.
PIN_CHECKS = [
    ("ECM", 9054, "Driven Tire Circumference", 2475),
    ("ECM", 9056, "Non-Driven Tire Circumference", 2475),
    ("ECM", 9050, "Final Drive Ratio", 3.08),
    ("ECM", 9052, "Final Drive Ratio - VSS Error", 3.08),
    ("TCM", 5004, "Final Drive Ratio - Trans", 3.08),
    ("ECM", 246, "DoD (AFM) Enable", 0),
]

STOCK_TUNE_NAME = "stock-10.13.24.hpt"
STOCK_TUNE_SHA256 = "0c108cea1fb037ac1b7e8c71edad4266b5eaa7a986d2ece3ba991825dfeeab16"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    # utf-8-sig: PowerShell's Add-Content -Encoding UTF8 writes a BOM on 5.1.
    with io.open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"  WARN: unparseable line in {path.name}: {exc}", file=sys.stderr)
    return out


def load_json(path: Path) -> dict:
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def norm_unit(u: str | None) -> str:
    return (u or "").replace("﻿", "").strip()


# VCM renders on/off parameters as an enumerated dropdown, not a checkbox, so the
# sweep records them with kind="enum" and a STRING raw_value. Mapping that string
# to 0/1 is a faithful transcription of what the UI shows, not a guess -- but it
# is applied ONLY to this closed vocabulary. Anything else keeps value=None and
# survives as raw_value alone, so an unrecognised enum can never be silently
# coerced into a number.
ENUM_BOOL = {
    "disable": 0, "disabled": 0, "off": 0, "no": 0, "false": 0, "inactive": 0,
    "enable": 1, "enabled": 1, "on": 1, "yes": 1, "true": 1, "active": 1,
}


def coerce_enum_bool(raw: str | None):
    if raw is None:
        return None
    return ENUM_BOOL.get(str(raw).strip().lower())



def parse_cell(raw):
    """Cell text -> float, or None. Never guesses: an unparseable cell stays None."""
    if raw is None:
        return None
    t = str(raw).strip().replace(",", "")
    if t == "":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def split_grid(grid):
    """Split a swept C1FlexGrid into axes + data.

    Layout confirmed live: row 0 holds the X axis, column 0 holds the Y axis,
    cell (0,0) is a corner label. Anything that does not fit that shape is
    returned with axes None and the raw grid preserved -- the caller still emits
    the grid, so nothing is lost, but no axis is fabricated.
    """
    if not grid or not isinstance(grid, list) or len(grid) < 1:
        return None, None, None
    n_rows = len(grid)
    n_cols = max(len(r) for r in grid)
    if n_rows < 2 or n_cols < 2:
        return None, None, None
    x_axis = [parse_cell(c) for c in grid[0][1:]]
    y_axis = [parse_cell(r[0]) if len(r) > 0 else None for r in grid[1:]]
    values = [[parse_cell(c) for c in r[1:]] for r in grid[1:]]
    # A 1-D table has a degenerate Y axis (a single unlabelled row).
    if len(y_axis) == 1 and y_axis[0] is None:
        y_axis = None
    return x_axis, y_axis, values


def build(sweep_dir: Path, data_dir: Path, out_path: Path, report_path: Path) -> int:
    scalars_raw = read_jsonl(sweep_dir / "scalars.jsonl")
    anomalies = read_jsonl(sweep_dir / "anomalies.jsonl")
    tables_raw = read_jsonl(sweep_dir / "tables.jsonl")

    if not scalars_raw:
        print("FATAL: no swept scalars found -- refusing to emit an empty baseline.", file=sys.stderr)
        return 2

    # ---- dedupe by (module, param_id); the sweep already dedupes, but a param
    # ---- can legitimately appear under two segments (e.g. Favorites mirrors).
    by_key: dict[tuple[str, int], dict] = {}
    conflicts: list[dict] = []
    for rec in scalars_raw:
        key = (str(rec["module"]), int(rec["param_id"]))
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = rec
            continue
        # Same parameter seen twice. Identical value -> fine. Different -> that is
        # a real integrity problem and must surface, not be quietly resolved.
        if prev.get("raw_value") != rec.get("raw_value"):
            conflicts.append({
                "key": f"{key[0]}:{key[1]}", "name": rec.get("name"),
                "first": {"value": prev.get("raw_value"), "where": prev.get("tab_path")},
                "second": {"value": rec.get("raw_value"), "where": rec.get("tab_path")},
            })

    # ---- reference shape: #24 -------------------------------------------------
    ref = load_json(data_dir / "2010_silverado_full.cal.json")
    best = load_json(data_dir / "2010_silverado_best.cal.json")

    # Reference index by param_id. full.cal.json has no module field, so index by
    # id alone and record collisions rather than guessing.
    ref_scalar_by_id: dict[int, list[dict]] = {}
    for s in ref["scalars"]:
        ref_scalar_by_id.setdefault(int(s["param_id"]), []).append(s)
    ref_table_by_id: dict[int, list[dict]] = {}
    for t in ref["tables"]:
        if t.get("param_id") is not None:
            ref_table_by_id.setdefault(int(t["param_id"]), []).append(t)
    best_scalar_by_id: dict[int, list[dict]] = {}
    for s in best["scalars"]:
        if s.get("param_id") is not None:
            best_scalar_by_id.setdefault(int(s["param_id"]), []).append(s)

    # ---- emit scalars ---------------------------------------------------------
    out_scalars = []
    enum_coerced = 0
    enum_uncoerced: list[dict] = []
    for (module, pid), rec in sorted(by_key.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        ref_hits = ref_scalar_by_id.get(pid, [])
        category = rec.get("segment") or ""
        if ref_hits:
            category = ref_hits[0].get("category") or category
        value = rec.get("value")
        if value is None and rec.get("kind") == "enum":
            value = coerce_enum_bool(rec.get("raw_value"))
            if value is not None:
                enum_coerced += 1
            else:
                enum_uncoerced.append({"key": f"{module}:{pid}", "name": rec.get("name"),
                                       "raw_value": rec.get("raw_value")})
        out_scalars.append({
            "name": rec.get("name"),
            "value": value,
            "unit": norm_unit(rec.get("unit")),
            "param_id": pid,
            "module": module,
            "kind": rec.get("kind"),
            "raw_value": rec.get("raw_value"),
            "category": category,
            "tab_path": rec.get("tab_path"),
            "note": rec.get("desc") or "",
            "provenance": {
                "source": "stock-sweep",
                "rule": f"offline VCM Editor read of {STOCK_TUNE_NAME}",
                "tune_sha256": STOCK_TUNE_SHA256,
            },
        })

    out_tables = []
    tables_by_key: dict[str, dict] = {}
    incomplete_tables: list[dict] = []
    for rec in tables_raw:
        key = str(rec.get("key"))
        if key in tables_by_key:
            continue
        tables_by_key[key] = rec
        grid = rec.get("grid")
        x_axis, y_axis, values = split_grid(grid)
        complete = bool(rec.get("complete", True))
        if not complete:
            incomplete_tables.append({
                "key": key, "name": rec.get("name"),
                "cells_read": rec.get("cells_read"),
                "declared_rows": rec.get("declared_rows"),
                "declared_cols": rec.get("declared_cols"),
            })
        ref_hits = ref_table_by_id.get(rec.get("param_id"), [])
        category = rec.get("segment") or ""
        if ref_hits:
            category = ref_hits[0].get("category") or category
        out_tables.append({
            "name": rec.get("name"),
            "unit": norm_unit(rec.get("unit")),
            "category": category,
            "param_id": rec.get("param_id"),
            "module": rec.get("module"),
            "note": rec.get("desc") or "",
            "tab_path": rec.get("tab_path"),
            "x_axis": x_axis,
            "y_axis": y_axis,
            "values": values,
            "raw_grid": grid,
            "n_rows": rec.get("n_rows"),
            "n_cols": rec.get("n_cols"),
            "cells_read": rec.get("cells_read"),
            "complete": complete,
            "provenance": {
                "source": "stock-sweep",
                "rule": f"offline VCM Editor UIA grid read of {STOCK_TUNE_NAME}",
                "tune_sha256": STOCK_TUNE_SHA256,
            },
        })

    doc = {
        "schema_version": 1,
        "metadata": {
            "vehicle": "2010 Chevrolet Silverado 1500 5.3L",
            "engine": "5.3L LMG V8 (E38 ECM / T43 TCM)",
            "trans": "6L80",
            "vin": "3GCRKTE35AG150432",
            "axle": "3.08 (as calibrated in this STOCK file; 4.11 is the physical axle installed later)",
            "tires": "stock (2475 mm rolling circumference as calibrated)",
            "base_tune": STOCK_TUNE_NAME,
            "base_tune_sha256": STOCK_TUNE_SHA256,
            "source": "offline VCM Editor UIA sweep on nexus-sweep-win11 (read-only, hover-identified)",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "is_stock_baseline": True,
            "warning": (
                "This file is the PRE-TUNING baseline. Parameters absent from it were "
                "not read by the sweep and must be treated as unknown, never as unchanged."
            ),
        },
        "scalars": out_scalars,
        "tables": out_tables,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # ---- pin cross-check ------------------------------------------------------
    pin_results = []
    pin_fail = 0
    for module, pid, name, expected in PIN_CHECKS:
        rec = by_key.get((module, pid))
        if rec is None:
            pin_results.append({"param": f"{module}:{pid}", "name": name,
                                "expected": expected, "swept": None, "result": "NOT-CAPTURED"})
            pin_fail += 1
            continue
        got = rec.get("value")
        if got is None and rec.get("kind") == "enum":
            got = coerce_enum_bool(rec.get("raw_value"))
        ok = got is not None and abs(float(got) - float(expected)) < 1e-6
        if not ok:
            pin_fail += 1
        pin_results.append({"param": f"{module}:{pid}", "name": name, "expected": expected,
                            "swept": got, "swept_name": rec.get("name"),
                            "swept_raw": rec.get("raw_value"), "swept_kind": rec.get("kind"),
                            "result": "MATCH" if ok else "MISMATCH"})

    # ---- coverage vs the existing 375 / 252 ----------------------------------
    swept_ids = {pid for (_m, pid) in by_key}
    best_scalar_ids = set(best_scalar_by_id)
    best_scalar_no_id = [s for s in best["scalars"] if s.get("param_id") is None]
    covered = sorted(best_scalar_ids & swept_ids)
    missing = sorted(best_scalar_ids - swept_ids)
    extra = sorted(swept_ids - best_scalar_ids)

    missing_detail = []
    for pid in missing:
        s = best_scalar_by_id[pid][0]
        missing_detail.append({"param_id": pid, "name": s.get("name"),
                               "category": s.get("category")})

    best_table_ids = {int(t["param_id"]) for t in best["tables"] if t.get("param_id") is not None}
    swept_table_ids = {int(t["param_id"]) for t in out_tables if t.get("param_id") is not None}

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "base_tune": STOCK_TUNE_NAME,
        "base_tune_sha256": STOCK_TUNE_SHA256,
        "pin_cross_check": {
            "checked": len(PIN_CHECKS),
            "failed": pin_fail,
            "verdict": "ALL MATCH" if pin_fail == 0 else f"{pin_fail} FAILED - DO NOT TRUST THIS BASELINE",
            "results": pin_results,
        },
        "scalars": {
            "swept_total": len(out_scalars),
            "by_kind": dict(Counter(s["kind"] for s in out_scalars)),
            "reference_population_best_cal": len(best["scalars"]),
            "reference_with_param_id": len(best_scalar_ids),
            "reference_without_param_id": len(best_scalar_no_id),
            "covered": len(covered),
            "missing": len(missing),
            "coverage_pct": round(100.0 * len(covered) / max(1, len(best_scalar_ids)), 1),
            "swept_not_in_reference": len(extra),
            "enum_bool_coerced": enum_coerced,
            "enum_left_as_raw_string": len(enum_uncoerced),
        },
        "tables": {
            "swept_total": len(out_tables),
            "reference_population_best_cal": len(best["tables"]),
            "reference_with_param_id": len(best_table_ids),
            "covered": len(best_table_ids & swept_table_ids),
            "missing": len(best_table_ids - swept_table_ids),
            "coverage_pct": round(100.0 * len(best_table_ids & swept_table_ids) / max(1, len(best_table_ids)), 1),
            "swept_not_in_reference": len(swept_table_ids - best_table_ids),
            "incomplete_grids": len(incomplete_tables),
        },
        "integrity": {
            "duplicate_value_conflicts": len(conflicts),
            "conflicts": conflicts[:50],
            "sweep_anomalies": len(anomalies),
            "anomaly_reasons": dict(Counter(a.get("reason", "?") for a in anomalies)),
        },
        "enums_not_coerced_to_number": enum_uncoerced,
        "incomplete_tables": incomplete_tables,
        "missing_tables": [
            {"param_id": pid,
             "name": next((t.get("name") for t in best["tables"] if t.get("param_id") == pid), None)}
            for pid in sorted(best_table_ids - swept_table_ids)
        ],
        "missing_scalars": missing_detail,
        "reference_scalars_without_param_id": [
            {"name": s.get("name"), "category": s.get("category")} for s in best_scalar_no_id
        ],
    }
    with io.open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # ---- console summary ------------------------------------------------------
    print(f"stock baseline written : {out_path}")
    print(f"  scalars captured     : {len(out_scalars)}  ({dict(Counter(s['kind'] for s in out_scalars))})")
    print(f"  tables captured      : {len(out_tables)}")
    print()
    print("PIN CROSS-CHECK (the only independent verification):")
    for r in pin_results:
        raw = f"  (raw {r.get('swept_raw')!r})" if r.get('swept_kind') == 'enum' else ''
        print(f"  [{r['result']:<12}] {r['param']:<10} {r['name']:<34} expected={r['expected']!s:<8} swept={r['swept']}{raw}")
    print(f"  verdict: {report['pin_cross_check']['verdict']}")
    print()
    print("COVERAGE vs best.cal.json:")
    sc = report["scalars"]
    print(f"  scalars : {sc['covered']}/{sc['reference_with_param_id']} with param_id ({sc['coverage_pct']}%)"
          f"; {sc['missing']} missing; {sc['swept_not_in_reference']} swept that reference lacks")
    tb = report["tables"]
    print(f"  tables  : {tb['covered']}/{tb['reference_with_param_id']} ({tb['coverage_pct']}%); "
          f"{tb['missing']} missing; {tb['swept_not_in_reference']} swept that reference lacks; "
          f"{tb['incomplete_grids']} incomplete grids")
    print(f"  conflicts: {len(conflicts)}   anomalies: {len(anomalies)} {report['integrity']['anomaly_reasons']}")
    print(f"report written         : {report_path}")

    if pin_fail:
        print("\nPIN CHECK FAILED -- the baseline is NOT trustworthy. Stop and investigate.", file=sys.stderr)
        return 3
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parent.parent
    ap.add_argument("--sweep-dir", type=Path, default=repo / "data" / "stock_sweep")
    ap.add_argument("--data-dir", type=Path, default=repo / "data")
    ap.add_argument("--out", type=Path, default=repo / "data" / "2010_silverado_stock.cal.json")
    ap.add_argument("--report", type=Path, default=repo / "data" / "stock_sweep_coverage.json")
    a = ap.parse_args()
    return build(a.sweep_dir, a.data_dir, a.out, a.report)


if __name__ == "__main__":
    raise SystemExit(main())
