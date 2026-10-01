import os
import re
base = os.path.dirname(os.path.abspath(__file__))
snap = base + r"\snap_ttemp.tsv"
lines = open(snap).read().splitlines()
ctx = {}
for l in lines:
    if l.startswith("# ctx"):
        for k,v in re.findall(r"'(\w+)': '([0-9A-Fa-f]+)'", l): ctx[k]=v
d = {}
for l in lines:
    if l.startswith("#") or not l.strip(): continue
    k,_,v = l.partition("\t"); d[k]=v.strip()
ect_b = int(ctx.get("ect","0"),16); iat_b = int(ctx.get("iat","0"),16)
print("live: coolant byte=%d (%dF)  iat byte=%d (%dF)" % (ect_b,(ect_b-40)*9//5+32, iat_b,(iat_b-40)*9//5+32))
print("target trans temp = 194F (byte 130 @ -40C, or 0xC2 direct)")
print("\n== DIDs with a byte decoding to ~194F (192-196) via -40C scaling ==")
for did,h in sorted(d.items()):
    b=[int(h[i:i+2],16) for i in range(0,len(h),2)]
    for i,x in enumerate(b):
        f=(x-40)*9/5+32
        if 192<=f<=196:
            tag=" <== equals coolant" if x==ect_b else (" (iat)" if x==iat_b else "")
            print("  %s byte%d=%d -> %.0fF%s" % (did,i,x,f,tag))
print("\n== DIDs with a raw byte == 194 (0xC2, direct F) ==")
for did,h in sorted(d.items()):
    b=[int(h[i:i+2],16) for i in range(0,len(h),2)]
    for i,x in enumerate(b):
        if x==194: print("  %s byte%d=194" % (did,i))
