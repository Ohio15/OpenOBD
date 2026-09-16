<#
  sweep-tables.ps1 -- sweep every TABLE of ONE ribbon segment of the loaded tune.

  MECHANISM (all of it verified live on this host before being written)
  --------------------------------------------------------------------
  * A table launcher is a WinForms Static label (UIA Text, AutomationId
    "label1") with NO value Edit on its row. A label WITH an adjacent Edit is a
    scalar's caption and is handled by sweep-scalars.ps1.
  * HOVERING the launcher populates the parameter-description box with
    "[ECM] 12621 - IAT Spark Advance Correction - Add: ...", giving module,
    param_id and name without touching anything.
  * DOUBLE-click opens the table. A single click does nothing -- verified.
  * The opened window hosts a C1.Win.FlexGrid.C1FlexGrid exposed to UIA as a
    Table whose cells are DataItem elements named "Column N Row M" carrying a
    ValuePattern. So the grid is read STRUCTURALLY: no clipboard, no context
    menu, no Ctrl+A/Ctrl+C. That matters because keystrokes aimed at a grid can
    edit it, and this tune is the only authentic pre-tuning read in existence.
  * Row 0 is the X axis, Column 0 is the Y axis, cell (0,0) is a corner label.

  INTEGRITY: the grid's own GridPattern RowCount/ColumnCount is compared against
  the number of cells actually read. Any shortfall (cell virtualisation, a
  scrolled grid) is recorded as an anomaly and the table is marked incomplete
  rather than emitted as if it were whole.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Segment,
  [string]$OutDir = 'C:\sweep\out\stock',
  [int]$MaxSeconds = 2400,
  [int]$HoverMs = 300
)

. 'C:\sweep\bin\sweep-lib.ps1'

Add-Type -TypeDefinition @"
using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public class TblWin{
 public delegate bool EP(IntPtr h,IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EP cb,IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p,EP cb,IntPtr l);
 [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll")] public static extern void GetWindowThreadProcessId(IntPtr h,out uint pid);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern IntPtr SendMessageW(IntPtr h,uint m,IntPtr w,IntPtr l);
 static uint TP; static List<IntPtr> acc;
 static void Grab(IntPtr h){ if(!IsWindowVisible(h))return; var sb=new StringBuilder(400); GetWindowText(h,sb,400);
   if(System.Text.RegularExpressions.Regex.IsMatch(sb.ToString(),@"^\[(ECM|TCM|E38|T43|ECU)\]\s+\d+")) acc.Add(h); }
 static bool Ch(IntPtr h,IntPtr l){ Grab(h); return true; }
 static bool Top(IntPtr h,IntPtr l){ uint p; GetWindowThreadProcessId(h,out p); if(p==TP){ Grab(h); EnumChildWindows(h,Ch,IntPtr.Zero);} return true; }
 public static IntPtr[] Handles(uint pid){ TP=pid; acc=new List<IntPtr>(); EnumWindows(Top,IntPtr.Zero); return acc.ToArray(); }
 public static string Title(IntPtr h){ var sb=new StringBuilder(400); GetWindowText(h,sb,400); return sb.ToString(); }
 public static void Close(IntPtr h){ SendMessageW(h,0x0010,IntPtr.Zero,IntPtr.Zero); }
}
"@

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$jsonl = Join-Path $OutDir 'tables.jsonl'
$log = Join-Path $OutDir 'tables.log'
$anom = Join-Path $OutDir 'anomalies.jsonl'
$sw = [System.Diagnostics.Stopwatch]::StartNew()

$seen = @{}
if (Test-Path $jsonl) {
  foreach ($ln in (Get-Content -LiteralPath $jsonl -Encoding UTF8)) {
    if ([string]::IsNullOrWhiteSpace($ln)) { continue }
    try { $o = $ln | ConvertFrom-Json; if ($null -ne $o.key) { $seen[[string]$o.key] = 1 } } catch {}
  }
}
Write-SweepLog $log "=== TABLES segment '$Segment' start (have $($seen.Count)) ==="

$vcm = Get-VcmEditor
if ($vcm.Title -notmatch 'stock-10\.13\.24\.hpt') {
  Write-SweepLog $log 'ABORT: loaded tune is not the staged stock file.'
  exit 9
}
$procId = [uint32]$vcm.Proc.Id
Set-VcmForeground $vcm

$added = 0; $skippedScalarLabels = 0; $noOpen = 0

function Save-Table($rec) { ($rec | ConvertTo-Json -Compress -Depth 8) | Add-Content -LiteralPath $script:jsonl -Encoding UTF8 }
function Save-Anomaly($rec) { ($rec | ConvertTo-Json -Compress -Depth 8) | Add-Content -LiteralPath $script:anom -Encoding UTF8 }

function Close-AllTableWindows {
  foreach ($h in [TblWin]::Handles($procId)) { [TblWin]::Close($h) }
  Start-Sleep -Milliseconds 350
}

# Read the C1FlexGrid in an open table window, structurally.
function Read-GridFromWindow([IntPtr]$hwnd) {
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
  if (-not $root) { return $null }
  $grid = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
          (New-TypeCondition ([System.Windows.Automation.ControlType]::Table)))
  if (-not $grid) { return $null }

  $declaredRows = -1; $declaredCols = -1
  try {
    $gp = $grid.GetCurrentPattern([System.Windows.Automation.GridPattern]::Pattern)
    $declaredRows = $gp.Current.RowCount; $declaredCols = $gp.Current.ColumnCount
  } catch {}

  $cells = @($grid.FindAll([System.Windows.Automation.TreeScope]::Descendants,
             (New-TypeCondition ([System.Windows.Automation.ControlType]::DataItem))))
  $map = @{}; $maxR = -1; $maxC = -1
  foreach ($c in $cells) {
    $m = [regex]::Match([string]$c.Current.Name, '^Column\s+(\d+)\s+Row\s+(\d+)$')
    if (-not $m.Success) { continue }
    $ci = [int]$m.Groups[1].Value; $ri = [int]$m.Groups[2].Value
    $map["$ri,$ci"] = [string](Get-UiaValue $c)
    if ($ri -gt $maxR) { $maxR = $ri }
    if ($ci -gt $maxC) { $maxC = $ci }
  }
  if ($maxR -lt 0 -or $maxC -lt 0) { return $null }

  $rows = New-Object System.Collections.ArrayList
  for ($r = 0; $r -le $maxR; $r++) {
    $row = New-Object System.Collections.ArrayList
    for ($c = 0; $c -le $maxC; $c++) {
      $k = "$r,$c"
      # A cell that was never realised is recorded as $null, never as 0.
      if ($map.ContainsKey($k)) { [void]$row.Add($map[$k]) } else { [void]$row.Add($null) }
    }
    [void]$rows.Add($row.ToArray())
  }
  return [pscustomobject]@{
    rows = $rows.ToArray(); nRows = ($maxR + 1); nCols = ($maxC + 1)
    declaredRows = $declaredRows; declaredCols = $declaredCols
    cellsRead = $map.Count
  }
}

function Read-TablesOnPanel([string]$tabPath) {
  $panel = Get-PanelForm $vcm
  if (-not $panel) { return 0 }
  $desc = Get-DescBox $panel
  if (-not $desc) { return 0 }

  $edits = @()
  foreach ($e in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::Edit))) {
    if ($e.Current.AutomationId -eq 'textBoxParameterDescription') { continue }
    if (-not (Test-OnScreen $e)) { continue }
    $r = $e.Current.BoundingRectangle
    $edits += [pscustomobject]@{ x = $r.X; cy = ($r.Y + $r.Height / 2) }
  }

  $cands = @()
  foreach ($t in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::Text))) {
    if ($t.Current.AutomationId -ne 'label1') { continue }
    if (-not (Test-OnScreen $t)) { continue }
    $r = $t.Current.BoundingRectangle
    $cy = $r.Y + $r.Height / 2
    $near = @($edits | Where-Object {
      [Math]::Abs($_.cy - $cy) -lt 9 -and $_.x -ge ($r.X - 4) -and $_.x -lt ($r.X + $r.Width + 90)
    })
    if ($near.Count -gt 0) { $script:skippedScalarLabels++; continue }
    $cands += $t
  }

  $n = 0
  foreach ($lab in $cands) {
    if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) {
      Write-SweepLog $script:log "TIME BUDGET HIT in '$tabPath' -- stopping cleanly, output is a valid partial."
      return $n
    }
    if (-not (Move-HoverTo $lab $HoverMs)) { continue }
    $parsed = ConvertFrom-DescString (Get-UiaValue $desc)
    if (-not $parsed) { continue }
    $key = '{0}:{1}' -f $parsed.module, $parsed.id
    if ($seen.ContainsKey($key)) { continue }

    Close-AllTableWindows
    $r = $lab.Current.BoundingRectangle
    $cx = [int]($r.X + $r.Width / 2); $cy2 = [int]($r.Y + $r.Height / 2)
    [SweepNative]::SetCursorPos($cx, $cy2) | Out-Null
    Start-Sleep -Milliseconds 140
    [SweepNative]::mouse_event([SweepNative]::LEFTDOWN, 0, 0, 0, [IntPtr]::Zero)
    [SweepNative]::mouse_event([SweepNative]::LEFTUP, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 80
    [SweepNative]::mouse_event([SweepNative]::LEFTDOWN, 0, 0, 0, [IntPtr]::Zero)
    [SweepNative]::mouse_event([SweepNative]::LEFTUP, 0, 0, 0, [IntPtr]::Zero)

    $hs = @()
    for ($w = 0; $w -lt 14; $w++) {
      Start-Sleep -Milliseconds 220
      $hs = @([TblWin]::Handles($procId))
      if ($hs.Count -gt 0) { break }
    }
    if ($hs.Count -eq 0) {
      # A launcher candidate that opened nothing. Almost always a caption that
      # is not a table at all, but it is recorded so the difference between
      # "not a table" and "a table we failed to open" stays auditable.
      $script:noOpen++
      Save-Anomaly ([ordered]@{ reason = 'label-opened-no-window'; segment = $Segment
                                tab_path = $tabPath; label = $lab.Current.Name
                                hover_key = $key; hover_name = $parsed.name })
      continue
    }

    $hwnd = $hs[0]
    $title = [TblWin]::Title($hwnd)
    $tparsed = ConvertFrom-DescString $title
    if ($tparsed) {
      # Trust the WINDOW title over the hover text: it is emitted by the table
      # that actually opened, so it cannot disagree with what we are reading.
      $key = '{0}:{1}' -f $tparsed.module, $tparsed.id
      if ($seen.ContainsKey($key)) { Close-AllTableWindows; continue }
    }

    $grid = $null
    try { $grid = Read-GridFromWindow $hwnd } catch {
      Save-Anomaly ([ordered]@{ reason = 'grid-read-failed'; key = $key; title = $title
                                error = $_.Exception.Message })
    }
    Close-AllTableWindows

    if (-not $grid) {
      Save-Anomaly ([ordered]@{ reason = 'no-grid-in-table-window'; key = $key; title = $title })
      continue
    }

    $expected = -1
    if ($grid.declaredRows -gt 0 -and $grid.declaredCols -gt 0) { $expected = $grid.declaredRows * $grid.declaredCols }
    $complete = ($expected -lt 0) -or ($grid.cellsRead -ge $expected)
    if (-not $complete) {
      Save-Anomaly ([ordered]@{ reason = 'grid-incomplete'; key = $key; title = $title
                                cells_read = $grid.cellsRead; declared_rows = $grid.declaredRows
                                declared_cols = $grid.declaredCols })
    }

    $seen[$key] = 1; $script:added++; $n++
    Save-Table ([ordered]@{
      key        = $key
      param_id   = $(if ($tparsed) { $tparsed.id } else { $parsed.id })
      module     = $(if ($tparsed) { $tparsed.module } else { $parsed.module })
      name       = $(if ($tparsed) { $tparsed.name } else { $parsed.name })
      desc       = $parsed.desc
      segment    = $Segment
      tab_path   = $tabPath
      label      = $lab.Current.Name
      window     = $title
      n_rows     = $grid.nRows
      n_cols     = $grid.nCols
      declared_rows = $grid.declaredRows
      declared_cols = $grid.declaredCols
      cells_read = $grid.cellsRead
      complete   = $complete
      grid       = $grid.rows
    })
  }
  return $n
}

function Set-TabPath([int[]]$idxPath) {
  if (-not (Select-Segment $vcm $Segment)) { return $false }
  $panel = Get-PanelForm $vcm
  if (-not $panel) { return $false }
  Set-PanelMaximized $panel | Out-Null
  for ($i = 0; $i -lt $idxPath.Count; $i++) {
    $panel = Get-PanelForm $vcm
    if (-not $panel) { return $false }
    $tabs = @(Wait-ForStableTabs $panel)
    if ($tabs.Count -le $i) { return $false }
    if (-not (Select-TabItemByIndex $tabs[$i] $idxPath[$i])) { return $false }
  }
  return $true
}

function Get-PathLabel([int[]]$idxPath) {
  $parts = New-Object System.Collections.Generic.List[string]
  $parts.Add($Segment)
  $panel = Get-PanelForm $vcm
  if ($panel) {
    for ($i = 0; $i -lt $idxPath.Count; $i++) {
      $tabs = @(Get-VisibleTabs $panel)
      if ($tabs.Count -le $i) { $parts.Add("?$($idxPath[$i])"); continue }
      $parts.Add((Get-TabItemNameAt $tabs[$i] $idxPath[$i]))
    }
  }
  return ($parts -join ' > ')
}

$script:visited = @{}
$script:nodeCount = 0
$script:MAX_NODES = 400

function Invoke-TabWalk([int[]]$idxPath) {
  if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) { return }
  if ($idxPath.Count -gt 4) { return }
  $sig = ($idxPath -join ',')
  if ($script:visited.ContainsKey($sig)) { return }
  $script:visited[$sig] = 1
  $script:nodeCount++
  if ($script:nodeCount -gt $script:MAX_NODES) { Write-SweepLog $log "NODE CAP at [$sig]"; return }

  if (-not (Set-TabPath $idxPath)) {
    Save-Anomaly ([ordered]@{ reason = 'tab-path-unreachable'; segment = $Segment; idx_path = $sig; phase = 'tables' })
    Write-SweepLog $log "  UNREACHABLE [$Segment idx=$sig]"
    return
  }

  $panel = Get-PanelForm $vcm
  if (-not $panel) { return }
  Set-PanelMaximized $panel | Out-Null
  $panel = Get-PanelForm $vcm
  $tabs = @(Wait-ForStableTabs $panel)
  $label = Get-PathLabel $idxPath

  if ($tabs.Count -le $idxPath.Count) {
    $got = Read-TablesOnPanel $label
    Write-SweepLog $log ("  leaf [{0}] +{1} tables (total {2})" -f $label, $got, $seen.Count)
    return
  }

  $row = $tabs[$idxPath.Count]
  $count = 0
  try {
    foreach ($it in @($row.FindAll([System.Windows.Automation.TreeScope]::Children,
                      (New-TypeCondition ([System.Windows.Automation.ControlType]::TabItem))))) { $count++ }
  } catch {}

  if ($count -eq 0) {
    $got = Read-TablesOnPanel $label
    Write-SweepLog $log ("  leaf [{0}] +{1} tables (total {2})" -f $label, $got, $seen.Count)
    return
  }

  for ($k = 0; $k -lt $count; $k++) {
    if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) { return }
    $child = New-Object System.Collections.Generic.List[int]
    foreach ($q in $idxPath) { $child.Add([int]$q) }
    $child.Add([int]$k)
    Invoke-TabWalk $child.ToArray()
  }
}

try { Invoke-TabWalk @() } catch { Write-SweepLog $log "FATAL: $($_.Exception.Message)" }
Close-AllTableWindows
Write-SweepLog $log "=== TABLES '$Segment' done: +$added, total $($seen.Count), scalar-labels-skipped $skippedScalarLabels, no-window $noOpen, $([int]$sw.Elapsed.TotalSeconds)s ==="
exit 0
