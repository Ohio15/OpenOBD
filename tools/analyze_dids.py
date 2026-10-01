import os
"""Correlate ECM mode-22 DIDs against known live OBD values to identify them."""
import sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import serial
from openobd.gt import ObdxGt, describe_no_gt
PORT = ObdxGt.autodetect()
if not PORT: sys.exit(describe_no_gt())

DIDS = []
for ln in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecm_dids.tsv"), encoding="ascii"):
    ln = ln.strip()
    if not ln or ln.startswith("#"): continue
    p = ln.split("\t")
    if len(p) >= 1 and len(p[0]) == 4:
        DIDS.append(p[0])

s = serial.Serial(PORT, 115200, timeout=0.4)
time.sleep(0.2)
def cmd(c, w=0.06):
    s.reset_input_buffer(); s.write((c+"\r").encode()); time.sleep(w)
    end=time.time()+1.0; buf=b""
    while time.time()<end:
        n=s.in_waiting
        if n:
            buf+=s.read(n)
            if b">" in buf: break
        else: time.sleep(0.005)
    return buf.decode(errors="ignore")
def hx(resp, hdr):
    t="".join(ch for ch in resp.upper() if ch in "0123456789ABCDEF")
    i=t.find(hdr)
    if i<0: return None
    r=t[i+len(hdr):]; r=r[:len(r)-len(r)%2]
    return [int(r[j:j+2],16) for j in range(0,len(r),2)]
for c in ("ATZ","ATE0","ATL0","ATS0","ATH0","ATSP6","ATAT0","ATST20","ATSH7E0"): cmd(c,0.5)

# known live values from mode 01
def m01(pid):
    return hx(cmd("01"+pid,0.08), "41"+pid)
rpm=m01("0C"); ect=m01("05"); iat=m01("0F"); mapb=m01("0B"); tps=m01("11")
spark=m01("0E"); load=m01("04"); maf=m01("10"); baro=m01("33"); rt=m01("1F")
known={}
if rpm: known["rpm"]=rpm[0]*256+rpm[1]          # raw (=rpm*4)
if ect: known["ect_C"]=ect[0]-40
if iat: known["iat_C"]=iat[0]-40
if mapb: known["map"]=mapb[0]
if tps: known["tps_raw"]=tps[0]
if spark: known["spark_raw"]=spark[0]
if load: known["load_raw"]=load[0]
if maf: known["maf_raw"]=maf[0]*256+maf[1]
if baro: known["baro"]=baro[0]
if rt: known["runtime"]=rt[0]*256+rt[1]
print("KNOWN (mode01):", known)

# snapshot all responding DIDs
snap={}
for did in DIDS:
    b=hx(cmd("22"+did,0.06), "62"+did)
    if b: snap[did]=b

print(f"snapshotted {len(snap)} DIDs")
# match each known value to DIDs
def u16(b,i=0): return b[i]*256+b[i+1] if i+1<len(b) else None
print("\n== MATCHES to known values ==")
for name,val in known.items():
    hits=[]
    for did,b in snap.items():
        for i in range(len(b)):
            if b[i]==val%256 and name in ("map","baro","tps_raw","spark_raw","load_raw","ect_C","iat_C") and b[i]==val:
                hits.append(f"{did}[byte{i}]={b[i]}")
        # word matches
        for i in range(len(b)-1):
            w=u16(b,i)
            if w==val and name in ("rpm","maf_raw","runtime"):
                hits.append(f"{did}[word{i}]={w}")
    if hits: print(f"  {name}={val}: " + ", ".join(hits[:8]))

# temp-like single/first-byte candidates (b-40 in 40..130 C) not equal to ect/iat
print("\n== TEMP-LIKE DIDs (byte-40 = 40..130C), excluding coolant/iat matches ==")
ectb = ect[0] if ect else None; iatb = iat[0] if iat else None
for did,b in sorted(snap.items()):
    for i in range(min(2,len(b))):
        c=b[i]-40
        if 40<=c<=130 and b[i] not in (ectb,iatb):
            print(f"  {did}[byte{i}]={b[i]} -> {c}C / {c*9//5+32}F")
            break

# small-int DIDs (single byte 0..10) -> gear candidates
print("\n== SMALL-INT DIDs (1-2 bytes, value 0..10) -> gear candidates ==")
for did,b in sorted(snap.items()):
    if len(b)<=2 and all(x<=10 for x in b):
        print(f"  {did} = {b}")
s.close()
