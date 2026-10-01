import os
def load(p):
    d = {}
    for l in open(p):
        if l.startswith("#") or not l.strip():
            continue
        k, _, v = l.partition("\t")
        d[k] = v.strip()
    return d
base = os.path.dirname(os.path.abspath(__file__))
P = load(base + r"\snap_park.tsv")
D = load(base + r"\snap_drive.tsv")
N = load(base + r"\snap_neutral.tsv")
R = load(base + r"\snap_reverse.tsv")
def val(h):
    try: return int(h, 16)
    except: return h
cands = ["130F","1175","1188","1148","1190","1410","1564","11F3","1200","11F8","1124"]
print("DID     Park   Rev  Neut Drive   distinct")
for c in cands:
    vs = [val(P.get(c,"")), val(R.get(c,"")), val(N.get(c,"")), val(D.get(c,""))]
    print("%-6s %5s %5s %5s %5s   %d" % (c, vs[0], vs[1], vs[2], vs[3], len(set(vs))))
print()
print("== single-byte DIDs with 4 DISTINCT values across P/R/N/D (range enum) ==")
common = set(P) & set(D) & set(N) & set(R)
for k in sorted(common):
    vs = [val(P[k]), val(R[k]), val(N[k]), val(D[k])]
    if (len(set(vs)) == 4 and all(isinstance(x, int) for x in vs)
            and len(P[k]) <= 2 and max(vs) <= 20):
        print("  %s: P=%d R=%d N=%d D=%d" % (k, vs[0], vs[1], vs[2], vs[3]))
