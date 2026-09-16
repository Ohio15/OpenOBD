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
# Every node is reached by REPLAYING the whole path from the ribbon segment down,
# rather than by inheriting whatever state the previous sibling left behind.
#
# The inherited-state version failed live with "tab-vanished" on Engine>Engine,
# Engine>Supercharger and Trans>Upshift: selecting one tab causes VCM Editor to
# rebuild the panel, which invalidates sibling TabItem elements captured before
# the navigation. Re-deriving the path makes each read deterministic and costs
# only ~1s per node.
# Paths are arrays of INTEGER INDICES, not names. VCM Editor has duplicate tab
# names on one row (two "General" tabs in both Engine and Trans), so a name is
# not a unique address -- see Select-TabItemByIndex in sweep-lib.ps1.
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

# Guards against the two ways this walk can go wrong. Both were hit live:
#   - a path visited twice => infinite recursion (the OS panel spun for 20min);
#   - an unbounded node count => a job that never returns and wedges the queue,
#     which is serial, so it blocks every later segment too.
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
  if ($script:nodeCount -gt $script:MAX_NODES) {
    Write-SweepLog $log "NODE CAP hit at [$sig] -- stopping cleanly."
    return
  }

  if (-not (Set-TabPath $idxPath)) {
    Save-Anomaly ([ordered]@{ reason = 'tab-path-unreachable'; segment = $Segment; idx_path = $sig })
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
    $got = Read-CurrentPanel $label
    Write-SweepLog $log ("  leaf [{0}] rows={1} +{2} (total {3})" -f $label, $tabs.Count, $got, $seen.Count)
    return
  }

  $row = $tabs[$idxPath.Count]
  $count = 0
  $names = New-Object System.Collections.Generic.List[string]
  try {
    foreach ($it in @($row.FindAll([System.Windows.Automation.TreeScope]::Children,
                      (New-TypeCondition ([System.Windows.Automation.ControlType]::TabItem))))) {
      $count++
      $names.Add([string]$it.Current.Name)
    }
  } catch {
    Save-Anomaly ([ordered]@{ reason = 'tabitem-enum-failed'; segment = $Segment
                              idx_path = $sig; error = $_.Exception.Message })
  }

  if ($count -eq 0) {
    $got = Read-CurrentPanel $label
    Write-SweepLog $log ("  leaf [{0}] rows={1} no-items +{2} (total {3})" -f $label, $tabs.Count, $got, $seen.Count)
    return
  }

  Write-SweepLog $log ("  depth {0} [{1}] {2} tabs: {3}" -f $idxPath.Count, $label, $count, ($names -join ' | '))
  for ($k = 0; $k -lt $count; $k++) {
    if ($sw.Elapsed.TotalSeconds -gt $MaxSeconds) { return }
    $child = New-Object System.Collections.Generic.List[int]
    foreach ($q in $idxPath) { $child.Add([int]$q) }
    $child.Add([int]$k)
    Invoke-TabWalk $child.ToArray()
  }
}

try {
  Invoke-TabWalk @()
} catch {
  Write-SweepLog $log "FATAL in tab walk: $($_.Exception.Message)"
}

Write-SweepLog $log "=== SEGMENT '$Segment' done: +$added new, $dupes dupes, $($seen.Count) total, $([int]$sw.Elapsed.TotalSeconds)s ==="
exit 0
