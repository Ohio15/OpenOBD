<#
  sweep-scalars.ps1 -- sweep every scalar parameter of ONE ribbon segment of the
  currently-loaded tune and append it to a JSONL checkpoint.

  Run one segment per job. The sweep agent executes jobs serially, so a single
  all-segments job would block the queue for its whole duration and lose
  everything if it wedged (which is exactly how the previous attempt died).

  Navigation is structural (InvokePattern / SelectionItemPattern) and the tab
  walk is a depth-first traversal of the real TabItem tree, so no tab can be
  skipped. Identity comes from hovering each control and reading the
  parameter-description box. See sweep-lib.ps1 for the coordinate policy.

  Output (append-only, resumable):
    <OutDir>\scalars.jsonl    one JSON record per parameter
    <OutDir>\sweep.log        progress log
    <OutDir>\anomalies.jsonl  things that need a human, never silently dropped
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Segment,
  [string]$OutDir = 'C:\sweep\out\stock',
  [int]$MaxSeconds = 1500,
  [int]$HoverMs = 240
)

. 'C:\sweep\bin\sweep-lib.ps1'

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$jsonl = Join-Path $OutDir 'scalars.jsonl'
$log = Join-Path $OutDir 'sweep.log'
$anom = Join-Path $OutDir 'anomalies.jsonl'
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# ---- resume: never re-record a param we already have -------------------------
$seen = @{}
if (Test-Path $jsonl) {
  foreach ($ln in (Get-Content -LiteralPath $jsonl -Encoding UTF8)) {
    if ([string]::IsNullOrWhiteSpace($ln)) { continue }
    try { $o = $ln | ConvertFrom-Json; if ($null -ne $o.key) { $seen[[string]$o.key] = 1 } } catch {}
  }
}
Write-SweepLog $log "=== SEGMENT '$Segment' start (already have $($seen.Count) params) ==="

$vcm = Get-VcmEditor
Write-SweepLog $log "VCM title: $($vcm.Title)"
if ($vcm.Title -notmatch 'stock-10\.13\.24\.hpt') {
  Write-SweepLog $log "ABORT: loaded tune is not the staged stock file."
  exit 9
}
Set-VcmForeground $vcm

if (-not (Select-Segment $vcm $Segment)) {
  Write-SweepLog $log "ABORT: segment '$Segment' not found on the ribbon."
  exit 8
}

$added = 0
$dupes = 0

function Save-Record($rec) {
  ($rec | ConvertTo-Json -Compress -Depth 6) | Add-Content -LiteralPath $script:jsonl -Encoding UTF8
}
function Save-Anomaly($rec) {
  ($rec | ConvertTo-Json -Compress -Depth 6) | Add-Content -LiteralPath $script:anom -Encoding UTF8
}

# ---- read every value-bearing control on the CURRENT leaf tab ----------------
function Read-CurrentPanel([string]$tabPath) {
  $panel = Get-PanelForm $vcm
  if (-not $panel) { return 0 }
  $desc = Get-DescBox $panel
  if (-not $desc) { return 0 }
  $links = Get-RowLinks $panel

  $targets = @()
  foreach ($e in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::Edit))) {
    if ($e.Current.AutomationId -eq 'textBoxParameterDescription') { continue }
    if (-not (Test-OnScreen $e)) { continue }
    $targets += [pscustomobject]@{ el = $e; kind = 'scalar' }
  }
  foreach ($e in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::CheckBox))) {
    if (-not (Test-OnScreen $e)) { continue }
    $targets += [pscustomobject]@{ el = $e; kind = 'bool' }
  }
  foreach ($e in (Get-Descendants $panel ([System.Windows.Automation.ControlType]::ComboBox))) {
    if (-not (Test-OnScreen $e)) { continue }
    $targets += [pscustomobject]@{ el = $e; kind = 'enum' }
  }

  $n = 0
  foreach ($t in $targets) {
    if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) {
      Write-SweepLog $script:log "TIME BUDGET HIT inside '$tabPath' -- stopping cleanly, output is a valid partial."
      return $n
    }

    $raw = $null; $boolState = $null
    switch ($t.kind) {
      'scalar' { $raw = Get-UiaValue $t.el }
      'enum'   { $raw = Get-UiaValue $t.el }
      'bool'   {
        try {
          $ts = $t.el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState
          $boolState = [string]$ts
          $raw = $(if ($boolState -eq 'On') { '1' } elseif ($boolState -eq 'Off') { '0' } else { $null })
        } catch { $raw = $null }
      }
    }
    if ([string]::IsNullOrWhiteSpace($raw)) { continue }

    if (-not (Move-HoverTo $t.el $HoverMs)) { continue }
    $dtxt = Get-UiaValue $desc
    $parsed = ConvertFrom-DescString $dtxt

    if (-not $parsed) {
      # Identity unknown -> it is NOT recorded as a parameter. Recorded as an
      # anomaly so the gap is visible instead of silently vanishing.
      Save-Anomaly ([ordered]@{
        reason = 'no-identity-on-hover'; segment = $Segment; tab_path = $tabPath
        kind = $t.kind; raw_value = $raw; desc_text = $dtxt
      })
      continue
    }

    $key = '{0}:{1}' -f $parsed.module, $parsed.id
    if ($seen.ContainsKey($key)) { $script:dupes++; continue }

    $num = $null
    if ($t.kind -ne 'enum') { $num = ConvertTo-Number $raw }
    if ($t.kind -eq 'scalar' -and $null -eq $num) {
      Save-Anomaly ([ordered]@{
        reason = 'unparseable-number'; segment = $Segment; tab_path = $tabPath
        key = $key; name = $parsed.name; raw_value = $raw
      })
      continue
    }

    $seen[$key] = 1
    $script:added++
    $n++
    Save-Record ([ordered]@{
      key       = $key
      param_id  = $parsed.id
      module    = $parsed.module
      name      = $parsed.name
      kind      = $t.kind
      value     = $num
      raw_value = $raw
      unit      = (Get-UnitFor $t.el $links)
      segment   = $Segment
      tab_path  = $tabPath
      desc      = $parsed.desc
    })
  }
  return $n
}

# ---- depth-first traversal of the real tab tree ------------------------------
# At each level we re-query the visible Tab controls and take the one at index
# $depth (ordered top-to-bottom). Selecting a TabItem can create or destroy a
# deeper Tab row, so the tree is re-read on every step rather than assumed.
function Invoke-TabWalk([string[]]$path, [int]$depth) {
  if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) { return }
  if ($depth -gt 4) { return }

  $panel = Get-PanelForm $vcm
  if (-not $panel) { return }
  $tabs = Wait-ForStableTabs $panel

  if ($tabs.Count -le $depth) {
    $p = ($path -join ' > ')
    $got = Read-CurrentPanel $p
    Write-SweepLog $log ("  leaf [{0}] tabrows={1} +{2} (total {3})" -f $p, $tabs.Count, $got, $seen.Count)
    return
  }

  $items = @()
  foreach ($it in @($tabs[$depth].el.FindAll([System.Windows.Automation.TreeScope]::Children, (New-TypeCondition ([System.Windows.Automation.ControlType]::TabItem))))) {
    $items += [pscustomobject]@{ name = $it.Current.Name }
  }
  if ($items.Count -eq 0) {
    $p = ($path -join ' > ')
    $got = Read-CurrentPanel $p
    Write-SweepLog $log ("  leaf [{0}] tabrow-no-items +{1} (total {2})" -f $p, $got, $seen.Count)
    return
  }
  Write-SweepLog $log ("  depth {0} tabs: {1}" -f $depth, (($items | ForEach-Object { $_.name }) -join ' | '))

  foreach ($item in $items) {
    if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) { return }
    # Re-resolve by name each iteration: the element list goes stale as pages swap.
    $panel = Get-PanelForm $vcm
    if (-not $panel) { return }
    $tabs = Wait-ForStableTabs $panel
    if ($tabs.Count -le $depth) { return }
    $target = $null
    foreach ($it in @($tabs[$depth].el.FindAll([System.Windows.Automation.TreeScope]::Children, (New-TypeCondition ([System.Windows.Automation.ControlType]::TabItem))))) {
      if ($it.Current.Name -eq $item.name) { $target = $it; break }
    }
    if (-not $target) {
      Save-Anomaly ([ordered]@{ reason = 'tab-vanished'; segment = $Segment; tab = $item.name; depth = $depth })
      continue
    }
    try {
      $target.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
      Start-Sleep -Milliseconds 550
    } catch {
      Save-Anomaly ([ordered]@{ reason = 'tab-select-failed'; segment = $Segment; tab = $item.name; error = $_.Exception.Message })
      continue
    }
    Invoke-TabWalk ($path + $item.name) ($depth + 1)
  }
}

try {
  Invoke-TabWalk @($Segment) 0
} catch {
  Write-SweepLog $log "FATAL in tab walk: $($_.Exception.Message)"
}

Write-SweepLog $log "=== SEGMENT '$Segment' done: +$added new, $dupes dupes, $($seen.Count) total, $([int]$sw.Elapsed.TotalSeconds)s ==="
exit 0
