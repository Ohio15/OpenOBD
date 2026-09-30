import os, sys, time, serial
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openobd.gt import ObdxGt, describe_no_gt
PORT = ObdxGt.autodetect()
if not PORT: sys.exit(describe_no_gt())
out = sys.argv[1]
DIDS = [l.split("\t")[0] for l in open(r"D:\Projects\OpenOBD\tools\ecm_dids.tsv")
        if l.strip() and not l.startswith("#") and len(l.split("\t")[0]) == 4]
s = serial.Serial(PORT, 115200, timeout=0.4); time.sleep(0.2)
def cmd(c, w=0.05):
    s.reset_input_buffer(); s.write((c+"\r").encode()); time.sleep(w)
    end=time.time()+0.8; buf=b""
    while time.time()<end:
        n=s.in_waiting
        if n:
            buf+=s.read(n)
            if b">" in buf: break
        else: time.sleep(0.004)
    return buf.decode(errors="ignore")
def hx(resp, hdr):
    t="".join(ch for ch in resp.upper() if ch in "0123456789ABCDEF"); i=t.find(hdr)
    if i<0: return None
    r=t[i+len(hdr):]; r=r[:len(r)-len(r)%2]; return r
for c in ("ATZ","ATE0","ATL0","ATS0","ATH0","ATSP6","ATAT0","ATST20","ATSH7E0"): cmd(c,0.4)
res={}
for did in DIDS:
    b=hx(cmd("22"+did,0.05),"62"+did)
    if b is not None: res[did]=b
# also stamp a few mode-01 knowns for context
def m(pid): 
    t=hx(cmd("01"+pid,0.06),"41"+pid); return t
ctx={"rpm":m("0C"),"ect":m("05"),"iat":m("0F"),"map":m("0B"),"tps":m("11"),"vss":m("0D")}
with open(out,"w") as f:
    f.write("# ctx " + str(ctx) + "\n")
    for k,v in res.items(): f.write(f"{k}\t{v}\n")
print("wrote", len(res), "DIDs ->", out, "| ctx:", ctx)
s.close()
