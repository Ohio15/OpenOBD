Add-Type -TypeDefinition @"
using System;using System.Text;using System.Runtime.InteropServices;using System.Collections.Generic;
public class H{
[DllImport("user32.dll")]public static extern bool SetProcessDPIAware();
[DllImport("user32.dll")]public static extern bool SetCursorPos(int x,int y);
[DllImport("user32.dll")]public static extern void mouse_event(uint f,uint dx,uint dy,uint d,IntPtr e);
[DllImport("user32.dll")]public static extern bool EnumWindows(EnumProc p,IntPtr l);
[DllImport("user32.dll")]public static extern bool EnumChildWindows(IntPtr h,EnumProc p,IntPtr l);
[DllImport("user32.dll")]public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
[DllImport("user32.dll")]public static extern int GetClassName(IntPtr h,StringBuilder s,int n);
[DllImport("user32.dll")]public static extern void GetWindowThreadProcessId(IntPtr h,out uint pid);
[DllImport("user32.dll")]public static extern bool GetWindowRect(IntPtr h,out RECT r);
[DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")]public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")]public static extern bool ShowWindow(IntPtr h,int n);
[DllImport("user32.dll")]public static extern IntPtr SendMessageW(IntPtr h,uint m,IntPtr w,IntPtr l);`r`n[DllImport("user32.dll")]public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int ht,bool r);
public delegate bool EnumProc(IntPtr h,IntPtr l);
public const uint LD=2,LU=4,RD=8,RU=16;
public static uint TPID; public static List<IntPtr> acc=new List<IntPtr>(); public static IntPtr big; public static int bigA;
static bool TopCb(IntPtr h,IntPtr l){uint p;GetWindowThreadProcessId(h,out p);if(p==TPID&&IsWindowVisible(h)){acc.Add(h);EnumChildWindows(h,new EnumProc(ChildCb),IntPtr.Zero);}return true;}
static bool ChildCb(IntPtr h,IntPtr l){acc.Add(h);return true;}
public static List<IntPtr> AllWins(uint pid){TPID=pid;acc=new List<IntPtr>();EnumWindows(new EnumProc(TopCb),IntPtr.Zero);return acc;}
static bool BigCb(IntPtr h,IntPtr l){uint p;GetWindowThreadProcessId(h,out p);if(p==TPID){EnumChildWindows(h,new EnumProc(BigKid),IntPtr.Zero);}return true;}
static bool BigKid(IntPtr h,IntPtr l){if(IsWindowVisible(h)){var c=new StringBuilder(64);GetClassName(h,c,64);if(c.ToString().Contains(".Window.")){RECT r;GetWindowRect(h,out r);int w=r.Right-r.Left,ht=r.Bottom-r.Top;if(w>500&&ht>300&&w*ht>bigA){bigA=w*ht;big=h;}}}return true;}
public static IntPtr Panel(uint pid){TPID=pid;big=IntPtr.Zero;bigA=0;EnumWindows(new EnumProc(BigCb),IntPtr.Zero);return big;}
public static void Max(IntPtr h){ShowWindow(h,3);} public static void Restore(IntPtr h){ShowWindow(h,9);} public static void Place(IntPtr h){ShowWindow(h,9);MoveWindow(h,2,111,900,600,true);}
public static string T(IntPtr h){var sb=new StringBuilder(256);GetWindowText(h,sb,256);return sb.ToString();}
public static bool Vis(IntPtr h){return IsWindowVisible(h);}
public static RECT R(IntPtr h){RECT r;GetWindowRect(h,out r);return r;}
public static void Close(IntPtr h){SendMessageW(h,0x0010,IntPtr.Zero,IntPtr.Zero);}
public static void LClick(int x,int y){SetCursorPos(x,y);System.Threading.Thread.Sleep(35);mouse_event(LD,0,0,0,IntPtr.Zero);mouse_event(LU,0,0,0,IntPtr.Zero);}
public static void RClick(int x,int y){SetCursorPos(x,y);System.Threading.Thread.Sleep(35);mouse_event(RD,0,0,0,IntPtr.Zero);mouse_event(RU,0,0,0,IntPtr.Zero);}
[DllImport("user32.dll")]public static extern int GetWindowLong(IntPtr h,int i);
public static int Style(IntPtr h){return GetWindowLong(h,-16);}
}
public struct RECT{public int Left,Top,Right,Bottom;}
"@
[H]::SetProcessDPIAware()|Out-Null
Add-Type -AssemblyName System.Windows.Forms
$global:OUT="C:\Users\ohio_\hpt_extract\stock_read"
New-Item -ItemType Directory -Force -Path "$OUT\raw" | Out-Null
$ed=Get-Process 'VCM Editor' | Select-Object -First 1
$global:EDPID=$ed.Id; $global:MAIN=$ed.MainWindowHandle
$global:TABS='General|Idle|Airflow|Fuel|Spark|Torque Model|Advance|Retard|Dwell|Knock Sensors|Favorites|OS|Engine|Engine Diag|Trans|Trans Diag|Fuel Sys|System|Speedo|Oxygen Sensors|Open Loop / Base|Power Enrich|Temperature Con|Cutoff, DFCO|Lean / Fuel-Saving|Transient|Flex Fuel'
$global:UNITS='°|rpm|mph|kph|km/h|kPa|MPa|psi|bar|°F|°C|K|lb·ft|lb-ft|N m|N·m|Nm|g|g/s|lb/min|kg/h|%|V|mV|s|ms|Hz|rad|°/s|in|inHg|mm|cc|L|gal|A|:1|rpm / 1000|°ATDC|°BTDC|counts'
function Get-Tables { $res=@(); foreach($h in [H]::AllWins([uint32]$EDPID)){ $t=[H]::T($h); if($t -match '^\[(ECM|TCM|E38|T43|ECU)\]\s+\d+'){ $res += @{h=$h;cap=$t} } }; return $res }
function Close-Popups { $n=0; while($n -lt 10){ $ts=Get-Tables; if($ts.Count -eq 0){break}; foreach($t in $ts){ [H]::Close($t.h) }; Start-Sleep -Milliseconds 200; $n++ } }
function Get-SI { $n=0; foreach($h in [H]::AllWins([uint32]$EDPID)){ if(-not [H]::Vis($h)){continue}; $t=[H]::T($h); if($t -match '^(rad|MPa|N m|N·m|Nm|°/s|km/h|kph)$'){ $n++ } }; return $n }
function Extract-Button([int]$x,[int]$y){
  Close-Popups
  [H]::SetForegroundWindow($MAIN)|Out-Null; Start-Sleep -Milliseconds 55
  [System.Windows.Forms.SendKeys]::SendWait("{ESC}"); Start-Sleep -Milliseconds 35
  [H]::LClick($x,$y); Start-Sleep -Milliseconds 400
  $ts=Get-Tables
  $tw=$ts | Where-Object { $_.cap -notmatch 'Axis$' } | Select-Object -First 1
  if(-not $tw){ if($ts.Count -gt 0){ Close-Popups; return "onlyAxis" }; return "noTable" }
  $m=[regex]::Match($tw.cap,'^\[(?<mod>\w+)\]\s+(?<id>\d+)\s*-\s*(?<name>.*)$')
  $id=$m.Groups['id'].Value;$mod=$m.Groups['mod'].Value;$name=$m.Groups['name'].Value
  if(Test-Path "$OUT\raw\$id.tsv"){ Close-Popups; return "dup id=$id" }
  $r=[H]::R($tw.h); $cx=[int](($r.Left+$r.Right)/2); $cy=[int](($r.Top+$r.Bottom)/2)
  [H]::LClick($cx,$cy); Start-Sleep -Milliseconds 130
  [System.Windows.Forms.SendKeys]::SendWait("^a"); Start-Sleep -Milliseconds 130
  [H]::RClick($cx,$cy); Start-Sleep -Milliseconds 420
  [System.Windows.Forms.SendKeys]::SendWait("{DOWN}{DOWN}{ENTER}"); Start-Sleep -Milliseconds 470
  $clip=Get-Clipboard -Raw
  Close-Popups
  if(-not $clip -or $clip.Length -lt 5){ return "emptyClip id=$id" }
  $clip | Set-Content -Encoding UTF8 "$OUT\raw\$id.tsv"
  "$id`t$mod`t$name`t$(($clip -split "`n").Count)" | Add-Content -Encoding UTF8 "$OUT\manifest.tsv"
  return "OK id=$id $name"
}
function Get-ButtonCenters {
  $cs=@()
  foreach($h in [H]::AllWins([uint32]$EDPID)){
    if(-not [H]::Vis($h)){continue}
    $t=[H]::T($h)
    if($t -notmatch '[A-Za-z]'){continue}
    if(([H]::Style($h) -band 0x00010000) -ne 0){continue}
    if($t -match '^\[(ECM|TCM)'){continue}
    if($t -match "^($TABS)$"){continue}
    if($t -match "^($UNITS)$"){continue}
    $r=[H]::R($h); $cx=[int](($r.Left+$r.Right)/2); $cy=[int](($r.Top+$r.Bottom)/2)
    if($cx -ge 5 -and $cx -le 1912 -and $cy -ge 190 -and $cy -le 1035){ $cs += [pscustomobject]@{x=$cx;y=$cy} }
  }
  $cs | Sort-Object {"{0:0000},{1:0000}" -f $_.x,$_.y} -Unique
}
function Ensure-Max { for($i=0;$i -lt 4;$i++){ $pan=[H]::Panel([uint32]$EDPID); if($pan -eq [IntPtr]::Zero){ Start-Sleep -Milliseconds 500; continue }; $r=[H]::R($pan); if(($r.Bottom-$r.Top) -gt 820){ return $true }; [H]::Max($pan); Start-Sleep -Milliseconds 700 }; return $false }
function Nav([int]$cx,[int]$cy,[int]$sx,[int]$sy,[int]$tx,[int]$ty){
  [H]::SetForegroundWindow($MAIN)|Out-Null; Start-Sleep -Milliseconds 400
  [H]::LClick($cx,$cy); Start-Sleep -Milliseconds 900
  if($sx -gt 0){ [H]::LClick($sx,$sy); Start-Sleep -Milliseconds 700 }
  if($tx -gt 0){ [H]::LClick($tx,$ty); Start-Sleep -Milliseconds 700 }
}
function Extract-Panel-Rects([string]$tag){
  $log="$OUT\progress.log"
  $mx=Ensure-Max
  $si0=Get-SI
  $before=@(Get-ChildItem "$OUT\raw" -Filter *.tsv).Count
  $cs=Get-ButtonCenters
  "[$tag] maximized=$mx SI-before=$si0 elements=$($cs.Count)" | Add-Content -Encoding UTF8 $log
  foreach($c in $cs){ $r=Extract-Button $c.x $c.y; if($r -like 'OK*'){ "[$tag] $r" | Add-Content -Encoding UTF8 $log } }
  $after=@(Get-ChildItem "$OUT\raw" -Filter *.tsv).Count
  $si1=Get-SI
  "[$tag] DONE +$($after-$before) new (total $after) SI-after=$si1" | Add-Content -Encoding UTF8 $log
  if($si1 -gt 0){ "[$tag] *** UNIT-SENTINEL ALERT: SI units detected ($si1) ***" | Add-Content -Encoding UTF8 $log }
}



function Get-Row2Tabs {
  $t=@()
  foreach($h in [H]::AllWins([uint32]$EDPID)){ if(-not [H]::Vis($h)){continue}; $txt=[H]::T($h); if($txt -notmatch '[A-Za-z]'){continue}; $r=[H]::R($h); $cy=[int](($r.Top+$r.Bottom)/2); $cx=[int](($r.Left+$r.Right)/2); if($cy -ge 171 -and $cy -le 187 -and $cx -gt 18 -and $cx -lt 900){ $t += [pscustomobject]@{x=$cx;y=$cy;name=($txt -replace '[^A-Za-z0-9]','')} } }
  $t | Sort-Object x -Unique
}


function Get-PanelSig { return ((Get-ButtonCenters | ForEach-Object { "$($_.x)_$($_.y)" }) -join ',') }
