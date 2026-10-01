import json, os, sys, glob, re
HERE = os.environ.get("OPENOBD_HPT_EXTRACT", os.path.join(os.path.expanduser("~"), "hpt_extract"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from openobd.calspec import Axis, Table, Scalar, Calibration
TUNE = os.path.join(HERE, "tune24_read"); STOCK = os.path.join(HERE, "stock_read")

# friendly segment names for the T-tag / S-tag prefixes in progress.log
SEGMAP = {"Airflow":"Engine",
  "OS":"OS","EngDiag":"Engine Diagnostics","Trans":"Transmission",
  "TransDiag":"Trans Diagnostics","FuelSys":"Fuel System","System":"System","Speedo":"Speedometer"}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hpt_parse import isnum, num, parse_tsv, load_manifest, load_dir, load_scalar_jsonl

def load_segmap():
    seg={}; lp=os.path.join(TUNE,"progress.log")
    if not os.path.exists(lp): return seg
    for ln in open(lp,encoding="utf-8"):
        m=re.search(r"\[(?:T\d+-x\d+|cur\d*|S(\w+?)-[^\]]*)\].*?OK id=(\d+)", ln)
        if m:
            if m.group(1): seg[m.group(2)]=m.group(1)
            else:
                mm=re.search(r"OK id=(\d+)", ln); 
                if mm: seg.setdefault(mm.group(1),"Airflow")
    return seg

tune,failt=load_dir(TUNE); stock,_=load_dir(STOCK); idseg=load_segmap()
print(f"parsed tune24={len(tune)} (failed {len(failt)}: {failt}) stock={len(stock)}")
cal=Calibration(metadata={"vehicle":"2010 Chevrolet Silverado 1500 5.3L","engine":"5.3L LMG V8 (E38 ECM / T43 TCM)",
  "trans":"6L80","vin":"3GCRKTE35AG150432","axle":"4.11","tires":'35"',
  "base_tune":"#24 - Claudes Edit 8.7.26","source":"VCM Editor Copy-with-Axis full sweep"})
n2=nstk=ns=0
for tid,t in sorted(tune.items(),key=lambda kv:int(kv[0])):
    cat=SEGMAP.get(idseg.get(tid,""), t["module"] or "Misc")
    x=t["x"]; grid=t["grid"]; nx=len(x)
    xa=Axis(label="",unit=t["xu"],values=x)
    note=f"[{t['module']}] id {tid}"
    if t["xcols"]: note+=" | cols: "+", ".join(t["xcols"])
    ya=None
    if len(grid)>1:
        if all(v is not None for v in t["ynum"]): ya=Axis(label="",unit=t["yu"],values=t["ynum"])
        else:
            ya=Axis(label="",unit="",values=[float(i) for i in range(len(grid))])
            note+=" | rows: "+", ".join([(t["ytext"][i] if t["ytext"][i] else str(i)) for i in range(len(grid))])
    if nx==1 and len(grid)==1 and len(grid[0])==1:
        s=stock.get(tid); stkv=(s["grid"][0][0] if s and s["grid"] and s["grid"][0] else None)
        cal.scalars.append(Scalar(name=t["name"],value=grid[0][0],unit=t["cu"],stock_value=stkv,param_id=int(tid),category=cat)); ns+=1; continue
    sv=None; s=stock.get(tid)
    if s and s["grid"] and len(s["grid"])==len(grid) and all(len(a)==len(b) for a,b in zip(s["grid"],grid)): sv=s["grid"]; nstk+=1
    cal.tables.append(Table(name=t["name"],x_axis=xa,y_axis=ya,values=grid,unit=t["cu"],stock_values=sv,param_id=int(tid),category=cat,note=note)); n2+=1
# ---- inline scalars from UIA hover sweep ----
SCSEG={"Engine":"Engine","OS":"OS","EngDiag":"Engine Diagnostics","Trans":"Transmission",
  "TransDiag":"Trans Diagnostics","FuelSys":"Fuel System","System":"System","Speedo":"Speedometer"}
tune_sc=load_scalar_jsonl(os.path.join(TUNE,"scalars.jsonl"))
stock_sc=load_scalar_jsonl(os.path.join(STOCK,"scalars.jsonl"))
# name index for stock captures without HPT ids (UIA harvester): join by name
# when the name is unambiguous in the stock capture
stock_by_name={}
stock_by_triple={}
for o in stock_sc.values():
    stock_by_name.setdefault(o.get("name",""), []).append(o)
    stock_by_triple[(o.get("category",""),o.get("desc",""),o.get("name",""))]=o
# alias map: tune24 full panel-title name -> stock UIA (category, desc, label)
# triple, built by tools/build_alias_map.py (browser labels are abbreviated
# on-screen captions, so exact-name joins alone hit only ~20/369)
_ap=os.path.join(ROOT,"data","tune24_scalar_aliases.json")
aliases=json.load(open(_ap,encoding="utf-8")) if os.path.exists(_ap) else {}
nsc=nsstk=nali=0
for sid,o in tune_sc.items():
    cat=SCSEG.get(o.get("category",""), o.get("module","") or "Misc")
    stk=stock_sc.get(sid)
    if stk is None:
        al=aliases.get(o.get("name",""))
        if al:
            stk=stock_by_triple.get((al["category"],al["desc"],al["name"]))
            if stk is not None: nali+=1
    if stk is None:
        cands=stock_by_name.get(o.get("name",""), [])
        if len(cands)==1: stk=cands[0]
    stkv=float(stk["value"]) if stk and stk.get("value") is not None else None
    if stkv is not None: nsstk+=1
    pid=sid if isinstance(sid,int) else None
    cal.scalars.append(Scalar(name=o.get("name","") or f"id {sid}", value=float(o.get("value",0)),
        unit=o.get("unit","") or "", stock_value=stkv, param_id=pid, category=cat,
        note=o.get("desc","") or ""))
    nsc+=1
print(f"  inline scalars added: {nsc} (stock baseline on {nsstk}, {nali} via alias map)")
# ---- end inline scalars ----
errs=cal.validate(); print(f"validation errors: {len(errs)}")
for e in errs[:20]: print("  ",e)
out=os.path.join(ROOT,"data","2010_silverado_full.cal.json"); cal.save(out)
print(f"wrote {out}"); print(f"  tables={n2} (stock baseline={nstk}) scalars={ns}")
cats={}
for t in cal.tables: cats[t.category]=cats.get(t.category,0)+1
for s in cal.scalars: cats[s.category]=cats.get(s.category,0)+1
print("  categories:",cats)

