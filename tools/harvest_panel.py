"""Local autonomous table harvester for VCM Editor.
Runs entirely on this machine: opens each table button, maximizes, selects all,
Copy-with-Axis via keyboard, reads clipboard, ingests, closes. Bridge-free loop.
Usage: python harvest_panel.py buttons.json
  buttons.json = [{"x":..,"y":..,"cat":"Engine/Airflow/..","unit":"","xunit":""}, ...]
"""
import ctypes, time, json, sys, os, re, subprocess
from ctypes import wintypes
HERE = os.path.dirname(os.path.abspath(__file__))
u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32
try: u32.SetProcessDPIAware()
except Exception: pass
ENV = dict(os.environ); ENV.pop("PYTHONHOME", None)
PY = sys.executable

VK_CTRL=0x11; VK_A=0x41; VK_DOWN=0x28; VK_RETURN=0x0D

def move(x,y): u32.SetCursorPos(int(x),int(y))
def _btn(down,up,dbl):
    u32.mouse_event(down,0,0,0,0); u32.mouse_event(up,0,0,0,0)
    if dbl:
        time.sleep(0.06); u32.mouse_event(down,0,0,0,0); u32.mouse_event(up,0,0,0,0)
def click(x,y,dbl=False,right=False):
    move(x,y); time.sleep(0.07)
    if right: _btn(0x08,0x10,False)
    else: _btn(0x02,0x04,dbl)
def key(vk):
    u32.keybd_event(vk,0,0,0); time.sleep(0.02); u32.keybd_event(vk,0,2,0)
def combo(mods,vk):
    for m in mods: u32.keybd_event(m,0,0,0)
    u32.keybd_event(vk,0,0,0); u32.keybd_event(vk,0,2,0)
    for m in reversed(mods): u32.keybd_event(m,0,2,0)

EP = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
def titles():
    out={}
    def child(h,l):
        n=u32.GetWindowTextLengthW(h)
        if n>0:
            b=ctypes.create_unicode_buffer(n+1); u32.GetWindowTextW(h,b,n+1)
            t=b.value
            if re.search(r"\[(ECM|TCM)\]", t): out[h]=t
        return True
    cp=EP(child)
    def top(h,l):
        child(h,l); u32.EnumChildWindows(h, cp, 0); return True
    u32.EnumWindows(EP(top), 0)
    return out
def tables_nonaxis():
    return {h:t for h,t in titles().items() if not re.search(r"Axis\s*$", t)}
def close_all():
    for h in titles().keys(): u32.PostMessageW(h,0x0010,0,0)

CF=13
def get_clip():
    if not u32.OpenClipboard(0): return ""
    try:
        h=u32.GetClipboardData(CF)
        if not h: return ""
        p=k32.GlobalLock(h)
        try: return ctypes.c_wchar_p(p).value or ""
        finally: k32.GlobalUnlock(h)
    finally: u32.CloseClipboard()

def ingest(clip,name,pid,cat,unit="",xunit=""):
    a=[PY, os.path.join(HERE,"ingest.py"), "--name",name, "--pid",str(pid), "--cat",cat]
    if unit: a+=["--unit",unit]
    if xunit: a+=["--xunit",xunit]
    r=subprocess.run(a, input=clip, text=True, capture_output=True, env=ENV)
    return (r.stdout+r.stderr).strip()

def harvest(x,y,cat,unit="",xunit=""):
    close_all(); time.sleep(0.35)
    click(x,y,dbl=True); time.sleep(0.7)
    tb=tables_nonaxis()
    if len(tb)!=1:
        close_all(); return "SKIP(%d win) @(%d,%d): %s" % (len(tb),x,y," | ".join(tb.values())[:80])
    hwnd,title=list(tb.items())[0]
    m=re.match(r"^\[(ECM|TCM)\]\s*(\d+)\s*-\s*(.+)$", title)
    if not m:
        u32.PostMessageW(hwnd,0x0010,0,0); return "SKIP(parse): "+title
    pid=m.group(2); name=m.group(3).strip()
    u32.ShowWindow(hwnd,3); time.sleep(0.3)
    click(150,258); time.sleep(0.12)
    combo([VK_CTRL],VK_A); time.sleep(0.12)
    click(150,258,right=True); time.sleep(0.25)
    key(VK_DOWN); time.sleep(0.06); key(VK_DOWN); time.sleep(0.06); key(VK_RETURN); time.sleep(0.2)
    out=ingest(get_clip(),name,pid,cat,unit,xunit)
    u32.PostMessageW(hwnd,0x0010,0,0); time.sleep(0.25)
    return "OK "+out

def main():
    btns=json.load(open(sys.argv[1], encoding="utf-8"))
    n=int(sys.argv[2]) if len(sys.argv)>2 else len(btns)
    for i,b in enumerate(btns[:n]):
        print("[%d/%d]" % (i+1,n), harvest(b["x"],b["y"],b["cat"],b.get("unit",""),b.get("xunit","")), flush=True)
    close_all()

if __name__=="__main__":
    main()