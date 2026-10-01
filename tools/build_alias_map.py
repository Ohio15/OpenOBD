"""Build the tune24->stock scalar alias map.

The desktop tune24 hover capture names scalars with full panel-title phrases
("Clutch Fuel Cutoff Enable RPM"); the UIA harvester (stock/trailering/hd2500)
captures the short on-screen label ("Enable RPM") with the panel path in
`category` and the group-box caption in `desc`. Exact-name joins therefore hit
only ~20/369. This tool matches each tune24 scalar to the stock capture's
unique (category, desc, name) triple by token evidence and writes
data/tune24_scalar_aliases.json for build_full.py's stock-baseline join.

Only confident matches are emitted: every label token must be found in the
tune24 name, the total evidence score must clear a floor, and the best
candidate must beat the runner-up by a clear margin. Unmatched / ambiguous
scalars are reported, not guessed.
"""
import json, os, re, sys, unicodedata

HERE = os.environ.get("OPENOBD_HPT_EXTRACT", os.path.join(os.path.expanduser("~"), "hpt_extract"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUNE = os.path.join(HERE, "tune24_read", "scalars.jsonl")
STOCK = os.path.join(HERE, "stock_read", "scalars.jsonl")
OUT = os.path.join(ROOT, "data", "tune24_scalar_aliases.json")

STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "by", "with", "vs"}
# same-file UIA capture of the tune24 .hpt: the identity oracle. A candidate
# alias is only correct if the desktop value equals the UIA value at the
# aliased triple (same file, two captures — any mismatch proves a wrong join;
# caught the CFCO/DFCO caption mispairing that token evidence alone passed).
TUNE_UIA = os.path.join(HERE, "tune24_uia_read", "scalars.jsonl")

def load(path):
    return [json.loads(l) for l in open(path, encoding="utf-8-sig") if l.strip()]

def norm_unit(u):
    u = unicodedata.normalize("NFKD", u or "")
    u = "".join(ch for ch in u if ch.isascii()).strip().lower()
    return {"deg f": "f", "degf": "f", "°f": "f", "f": "f", "deg c": "c", "c": "c"}.get(u, u)

def tokens(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch if (ch.isascii() and (ch.isalnum() or ch.isspace())) else " " for ch in s)
    return [t for t in s.lower().split() if t and t not in STOP]

def tok_match(a, b):
    """True if token a and b refer to the same word, allowing truncation
    ("min"/"minimum") and initials ("pw"/"pulsewidth")."""
    if a == b:
        return True
    lo, hi = (a, b) if len(a) < len(b) else (b, a)
    if len(lo) >= 3 and hi.startswith(lo):
        return True
    # initials-as-subsequence from the first char ("pw" in "pulsewidth")
    if 2 <= len(lo) <= 4 and lo[0] == hi[0]:
        i = 0
        for ch in hi:
            if i < len(lo) and ch == lo[i]:
                i += 1
        if i == len(lo):
            return True
    return False

def coverage(needles, hay):
    """Fraction of needle tokens with a match in hay tokens."""
    if not needles:
        return 0.0
    hit = 0
    for n in needles:
        if any(tok_match(n, h) for h in hay):
            hit += 1
    return hit / len(needles)

def score(trec, srec):
    tname = tokens(trec.get("name", ""))
    label = tokens(srec.get("name", ""))
    desc = tokens(srec.get("desc", ""))
    cat_tail = tokens((srec.get("category", "").split("/") or [""])[-1])
    lab_cov = coverage(label, tname)
    if lab_cov < 1.0:  # the on-screen label must be fully present in the full name
        return None
    desc_cov = coverage(desc, tname)
    cat_cov = coverage(cat_tail, tname)
    # how much of the full tune24 name is explained by label+desc+category
    explained = coverage(tname, label + desc + cat_tail + tokens(srec.get("category", "")))
    # every distinctive token of the full name must be accounted for by the
    # stock record's label/group/panel — otherwise a generic label like
    # "Disable RPM" under System/A/C magnetizes "DoD Disable RPM",
    # "Traction Control RPM Disable", etc. (real mis-joins caught in audit)
    if explained < 1.0:
        return None
    unit_bonus = 0.0
    tu, su = norm_unit(trec.get("unit", "")), norm_unit(srec.get("unit", ""))
    if tu and su:
        unit_bonus = 0.5 if tu == su else -0.75
    # group-box caption that the full name doesn't echo at all is a wrong-group
    # signal (e.g. "...Disable DFCO" vs a "(CFCO)" desc)
    desc_pen = 0.0
    if desc and desc_cov == 0.0:
        desc_pen = -1.75
    elif desc and desc_cov < 0.34:
        desc_pen = -1.0
    # the captures are different tunes but most scalars are untuned, so equal
    # values are strong same-parameter evidence; inequality proves nothing
    val_bonus = 0.0
    try:
        tv, sv = float(trec.get("value")), float(srec.get("value"))
        if tv == sv or (sv and abs(tv - sv) / abs(sv) < 0.001):
            val_bonus = 0.5
    except (TypeError, ValueError):
        pass
    return 2.0 * lab_cov + 1.5 * desc_cov + 0.5 * cat_cov + 2.0 * explained + unit_bonus + desc_pen + val_bonus

def main():
    tune, stock = load(TUNE), load(STOCK)
    stock_by_exact = {}
    for s in stock:
        stock_by_exact.setdefault(s.get("name", ""), []).append(s)

    aliases, ambiguous, unmatched = {}, [], []
    for t in tune:
        tn = t.get("name", "")
        # exact unique name match needs no alias entry (build_full handles it)
        ex = stock_by_exact.get(tn, [])
        if len(ex) == 1:
            continue
        scored = []
        for s in stock:
            sc = score(t, s)
            if sc is not None:
                scored.append((sc, s))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < 3.2:
            unmatched.append(tn)
            continue
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.6:
            ambiguous.append((tn, [(round(sc, 2), s["category"], s["desc"], s["name"]) for sc, s in scored[:3]]))
            continue
        s = scored[0][1]
        aliases[tn] = {"category": s["category"], "desc": s["desc"], "name": s["name"],
                       "score": round(scored[0][0], 2)}

    # identity validation against the same-file UIA capture (when present),
    # loaded through hpt_parse so the capture-corrections overlay applies
    if os.path.exists(TUNE_UIA):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from hpt_parse import load_scalar_jsonl
        uia = list(load_scalar_jsonl(TUNE_UIA).values())
        ui = {(o.get("category", ""), o.get("desc", ""), o.get("name", "")): o for o in uia}
        dn = {t.get("name", ""): t for t in tune}
        dropped = 0
        for tn in list(aliases):
            v = aliases[tn]
            u = ui.get((v["category"], v["desc"], v["name"]))
            if u is None or tn not in dn:
                continue
            try:
                dv, uv = float(dn[tn]["value"]), float(u["value"])
            except (TypeError, ValueError):
                continue
            if dv != uv and not (uv and abs(dv - uv) / abs(uv) < 0.001):
                print(f"  identity REJECT: {tn} (desktop={dv} != uia={uv} at {v['name']!r})")
                del aliases[tn]
                unmatched.append(tn)
                dropped += 1
        print(f"identity validation vs tune24_uia: {dropped} alias(es) rejected")
    else:
        print("WARNING: tune24_uia_read absent - aliases are NOT identity-validated")

    # collision check: two tune24 names claiming the same stock scalar means at
    # least one join is wrong — keep only a clear winner, else drop both
    by_triple = {}
    for tn, v in aliases.items():
        by_triple.setdefault((v["category"], v["desc"], v["name"]), []).append((v["score"], tn))
    collisions = 0
    for triple, claimants in by_triple.items():
        if len(claimants) < 2:
            continue
        claimants.sort(reverse=True)
        drop = claimants[1:] if claimants[0][0] - claimants[1][0] >= 0.6 else claimants
        for _, tn in drop:
            del aliases[tn]
            collisions += 1
            ambiguous.append((tn, [("collision on", *triple)]))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(aliases, f, indent=1, sort_keys=True)
    print(f"tune24 scalars: {len(tune)}  exact-name joins: "
          f"{sum(1 for t in tune if len(stock_by_exact.get(t.get('name',''), []))==1)}")
    print(f"aliases written: {len(aliases)} -> {OUT}")
    print(f"ambiguous (skipped): {len(ambiguous)}  unmatched: {len(unmatched)}")
    if "--verbose" in sys.argv:
        print("\n-- ambiguous --")
        for tn, cands in ambiguous:
            print(" ", tn)
            for c in cands:
                print("     ", c)
        print("\n-- unmatched --")
        for tn in unmatched:
            print(" ", tn)

if __name__ == "__main__":
    main()
