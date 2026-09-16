<#
  sweep-lib.ps1 -- UIA helpers for the offline VCM Editor calibration sweep.

  COORDINATE POLICY (read this before changing anything)
  -----------------------------------------------------
  The inherited harness (OpenOBD tools/{extract,master_sweep,scalar_sweep}.ps1)
  navigated by hardcoded GLOBAL screen pixels measured on Ron's desktop:
  the segment ribbon at y=93 on a guessed x-list, then a blind scan
  x=18..885 in 44px steps at y=184 hoping to land on each sub-tab.

  That is unsound for three independent reasons, all verified on this host:

    1. STATE-DEPENDENT. The ribbon's own y moves with window state. Measured
       live on this VM: segment row at y=100 with a restored 900x600 panel,
       and at y=79..107 with the panel maximized. A pixel that is correct in
       one state is wrong in the other, and nothing reports the difference.

    2. SILENTLY LOSSY. A 44px step can step straight over a narrow tab. That
       tab is never visited and its parameters are never recorded -- which is
       indistinguishable, in the output, from "that tab has no parameters".

    3. NO INTEGRITY SIGNAL. A mis-aimed click lands on a neighbouring control
       and reads a real-but-wrong number. That is far worse than reading
       nothing, because it looks like data.

  This library therefore hardcodes NO coordinates. Every position is read at
  runtime from the control's own UIA BoundingRectangle, and navigation uses
  InvokePattern / SelectionItemPattern, which need no coordinates at all.

  The ONLY place a screen coordinate is still used is Move-HoverTo, because
  VCM Editor populates its parameter-description box on mouse-enter and on
  nothing else -- SetFocus() was tested on this host and leaves the box empty
  (probe_focus.ps1, 2026-09-16). That coordinate is the centre of the target
  element's own live BoundingRectangle, so it tracks the element.

  READ-ONLY GUARANTEE: parameter identity is resolved by HOVER (SetCursorPos
  only). No mouse button is ever pressed on a parameter Edit, so the scalar
  sweep cannot modify the loaded tune. The staged stock .hpt is the only
  authentic pre-tuning read in existence and must never be written.
#>

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes

if (-not ('SweepNative' -as [type])) {
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public class SweepNative {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint dx,uint dy,uint d,IntPtr e);
  public const uint LEFTDOWN=2, LEFTUP=4, RIGHTDOWN=8, RIGHTUP=16;
}
"@
}
[SweepNative]::SetProcessDPIAware() | Out-Null

$script:AE = [System.Windows.Automation.AutomationElement]
$script:DESC_AID = 'textBoxParameterDescription'
$script:PANEL_AID = 'ParameterGroupForm'

function New-AidCondition([string]$aid) {
  New-Object System.Windows.Automation.PropertyCondition($script:AE::AutomationIdProperty, $aid)
}

function New-TypeCondition($ct) {
  New-Object System.Windows.Automation.PropertyCondition($script:AE::ControlTypeProperty, $ct)
}

function Get-Descendants($root, $ct) {
  if (-not $root) { return @() }
  try {
    return @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-TypeCondition $ct)))
  } catch { return @() }
}

function Get-UiaValue($el) {
  if (-not $el) { return $null }
  try { return $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value }
  catch { return $null }
}

function Test-OnScreen($el) {
  try {
    if ($el.Current.IsOffscreen) { return $false }
    $r = $el.Current.BoundingRectangle
    return ($r.Width -gt 0 -and $r.Height -gt 0 -and -not [double]::IsInfinity($r.X))
  } catch { return $false }
}

function Get-VcmEditor {
  $p = Get-Process 'VCM Editor' -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $p) { throw 'VCM Editor is not running.' }
  if ($p.MainWindowHandle -eq 0) { throw 'VCM Editor has no main window (modal dialog up?).' }
  $root = $script:AE::FromHandle([IntPtr]$p.MainWindowHandle)
  if (-not $root) { throw 'UIA could not attach to the VCM Editor main window.' }
  [pscustomobject]@{
    Proc = $p; Hwnd = [IntPtr]$p.MainWindowHandle; Root = $root; Title = $root.Current.Name
  }
}

function Set-VcmForeground($vcm) {
  [SweepNative]::SetForegroundWindow($vcm.Hwnd) | Out-Null
  Start-Sleep -Milliseconds 250
}

# Select a top ribbon segment (OS/Engine/Trans/...) via InvokePattern. No coordinates.
function Select-Segment($vcm, [string]$name) {
  $bar = $vcm.Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-AidCondition 'toolStripEdit'))
  if (-not $bar) { throw 'Segment toolbar (toolStripEdit) not found.' }
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  $e = $walker.GetFirstChild($bar)
  while ($e) {
    if ($e.Current.Name -eq $name) {
      $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
      Start-Sleep -Milliseconds 1100
      return $true
    }
    $e = $walker.GetNextSibling($e)
  }
  return $false
}

function Get-SegmentNames($vcm) {
  $bar = $vcm.Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-AidCondition 'toolStripEdit'))
  if (-not $bar) { return @() }
  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
  $out = @()
  $e = $walker.GetFirstChild($bar)
  while ($e) {
    $n = $e.Current.Name
    if ($n -and $e.Current.ControlType.ProgrammaticName -notmatch 'Separator') { $out += $n }
    $e = $walker.GetNextSibling($e)
  }
  return $out
}

function Get-PanelForm($vcm) {
  $vcm.Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-AidCondition $script:PANEL_AID))
}

function Get-DescBox($panel) {
  if (-not $panel) { return $null }
  $panel.FindFirst([System.Windows.Automation.TreeScope]::Descendants, (New-AidCondition $script:DESC_AID))
}

# Hover the centre of an element's OWN live rectangle. Never presses a button.
function Move-HoverTo($el, [int]$settleMs = 260) {
  try {
    $r = $el.Current.BoundingRectangle
    if ([double]::IsInfinity($r.X) -or $r.Width -le 0) { return $false }
    $cx = [int]($r.X + $r.Width / 2)
    $cy = [int]($r.Y + $r.Height / 2)
    [SweepNative]::SetCursorPos($cx, $cy) | Out-Null
    Start-Sleep -Milliseconds $settleMs
    return $true
  } catch { return $false }
}

# '[ECM] 9054 - Driven Tire Circumference: Rolling circumference of ...'
function ConvertFrom-DescString([string]$s) {
  if ([string]::IsNullOrWhiteSpace($s)) { return $null }
  $m = [regex]::Match($s, '^\s*\[(?<mod>[A-Za-z0-9]+)\]\s+(?<id>\d+)\s*-\s*(?<name>[^:]+?)(?::\s*(?<d>[\s\S]*))?$')
  if (-not $m.Success) { return $null }
  [pscustomobject]@{
    module = $m.Groups['mod'].Value
    id     = [int]$m.Groups['id'].Value
    name   = $m.Groups['name'].Value.Trim()
    desc   = $m.Groups['d'].Value.Trim()
  }
}

# VCM renders thousands separators ('2,475'). Invariant parse, comma-stripped.
function ConvertTo-Number([string]$raw) {
  if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
  $t = $raw.Trim() -replace ',', ''
  $d = 0.0
  $ok = [double]::TryParse($t, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$d)
  if ($ok) { return $d }
  return $null
}

# Units render as Hyperlink controls immediately right of the value box.
# The bound is deliberately tight: an unbounded "nearest to the right" search
# picks up the units cell of the NEXT COLUMN on the same row. That was observed
# live (probe_hover.ps1) pairing 'mm' onto a pulses-per-rev field.
function Get-UnitFor($editEl, $links, [int]$maxGapPx = 70) {
  try {
    $r = $editEl.Current.BoundingRectangle
    $right = $r.X + $r.Width
    $cand = $links | Where-Object {
      [Math]::Abs($_.y - $r.Y) -lt 12 -and $_.x -ge ($right - 4) -and $_.x -lt ($right + $maxGapPx)
    } | Sort-Object x | Select-Object -First 1
    if ($cand) { return $cand.name }
    return ''
  } catch { return '' }
}

function Get-RowLinks($panel) {
  $out = @()
  foreach ($t in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::Hyperlink))) {
    try {
      $r = $t.Current.BoundingRectangle
      if ([double]::IsInfinity($r.X)) { continue }
      $out += [pscustomobject]@{ x = $r.X; y = $r.Y; name = $t.Current.Name }
    } catch {}
  }
  return $out
}

# Tab rows of the open parameter panel, ordered top-to-bottom (row 0 = outer).
# Returns a (possibly empty) ARRAY. Callers must be able to distinguish
# "no tab rows" from "not built yet" -- see Wait-ForStableTabs.
function Get-VisibleTabs($panel) {
  $acc = New-Object System.Collections.ArrayList
  foreach ($t in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::Tab))) {
    if (-not (Test-OnScreen $t)) { continue }
    $y = 0.0
    try { $y = [double]$t.Current.BoundingRectangle.Y } catch { $y = 0.0 }
    [void]$acc.Add([pscustomobject]@{ el = $t; y = $y })
  }
  $sorted = @($acc | Sort-Object -Property y)
  return ,$sorted
}

# A freshly-swapped panel reports zero Tab children for a few hundred ms while
# WinForms builds them. Reading in that window makes a tabbed panel look flat,
# so only the tab that happened to be selected gets swept and the rest are lost
# with no error -- observed live on Speedo (Calibration read, Limiter silently
# skipped). Poll until the count is STABLE across consecutive reads instead of
# trusting the first answer. A genuinely tab-less panel stabilises at 0.
function Wait-ForStableTabs($panel, [int]$timeoutMs = 6000) {
  $last = -1
  $stableSince = $null
  $deadline = (Get-Date).AddMilliseconds($timeoutMs)
  $cur = @()
  while ((Get-Date) -lt $deadline) {
    $cur = @(Get-VisibleTabs $panel)
    $c = $cur.Count
    if ($c -eq $last) {
      if ($null -eq $stableSince) { $stableSince = Get-Date }
      if (((Get-Date) - $stableSince).TotalMilliseconds -ge 350) { return ,$cur }
    } else {
      $last = $c
      $stableSince = $null
    }
    Start-Sleep -Milliseconds 220
  }
  return ,$cur
}

function Write-SweepLog([string]$path, [string]$msg) {
  $line = '{0}  {1}' -f (Get-Date -Format o), $msg
  Add-Content -LiteralPath $path -Value $line -Encoding UTF8
  Write-Output $msg
}
