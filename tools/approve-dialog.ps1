<#
approve-dialog.ps1 -- a UAC-style approval popup for an elevated-risk agent action.

WHY. The closed truck loop must not hard-stop on an elevated action (Ron,
2026-09-17 / 2026-10-03). Instead a desktop popup -- topmost, like UAC -- shows
the pending action and offers Allow / Reject; the click is the decision.

SECURITY MODEL (why this is a separate script, not agent code). Like UAC, the
prompt is meant to be raised by the TRUSTED layer (the hazard-guard PreToolUse
hook), not by the agent, so the agent cannot skip it or click it. The guard runs
this script synchronously and gates on the EXIT CODE:
    0  = ALLOW      (Ron clicked Allow)
    2  = REJECT     (Ron clicked Reject)
    3  = TIMEOUT    (no click within -TimeoutSeconds; treated as reject)
    4  = CLOSED     (window closed without a choice; treated as reject)
Only exit 0 permits the action; the default is always deny.

If -DecisionFile is given, a JSON record of the decision is written there too
(for an async/audit path). The authoritative signal for the guard is the exit
code, which the guard reads from its own synchronous child -- no key needed,
because the trusted guard both raises the prompt and reads the result.

-SelfTest validates argument handling and the decision-record write WITHOUT
showing any UI (so CI / a syntax check never pops a window).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$Action,
    [string]$Detail = "",
    [ValidateSet("read", "clear", "write")] [string]$Risk = "read",
    [int]$TimeoutSeconds = 120,
    [string]$RequestId = "",
    [string]$DecisionFile = "",
    [switch]$SelfTest
)

$ExitAllow = 0; $ExitReject = 2; $ExitTimeout = 3; $ExitClosed = 4

function Write-Decision([string]$decision, [string]$approver) {
    if (-not $DecisionFile) { return }
    $rec = [ordered]@{
        request_id = $RequestId
        action     = $Action
        risk       = $Risk
        decision   = $decision
        decided_at = [Math]::Round((Get-Date -UFormat %s), 3)
        approver   = $approver
    }
    try {
        $dir = Split-Path -Parent $DecisionFile
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        ($rec | ConvertTo-Json -Compress) | Add-Content -Path $DecisionFile -Encoding ascii
    } catch {
        Write-Error "could not write decision file: $_"
    }
}

if ($SelfTest) {
    # exercise the non-UI paths only
    if (-not $Action) { Write-Error "Action required"; exit 10 }
    Write-Decision -decision "selftest" -approver "selftest"
    Write-Output "selftest ok: action=$Action risk=$Risk timeout=$TimeoutSeconds"
    exit $ExitAllow
}

# FAIL SAFE: any error raising or running the dialog must deny, never allow.
# (The guard also treats any non-zero exit as deny; this makes the script itself
# honour that even on an unhandled exception.)
trap {
    try { Write-Decision -decision "deny" -approver "error" } catch {}
    Write-Error "approval dialog error: $_"
    Write-Output "ERROR"
    exit $ExitReject
}

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

# risk -> accent colour (read calm, clear amber, write red)
$accent = switch ($Risk) { "write" { "#C62828" } "clear" { "#EF6C00" } default { "#1565C0" } }

$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Approval required" Width="520" SizeToContent="Height"
        WindowStartupLocation="CenterScreen" Topmost="True" ResizeMode="NoResize"
        WindowStyle="ToolWindow" Background="#1E1E1E">
  <Border Padding="18">
    <StackPanel>
      <TextBlock Text="&#x26A0; Elevated action pending approval" FontSize="16"
                 FontWeight="Bold" Foreground="$accent" Margin="0,0,0,10"/>
      <TextBlock x:Name="ActionText" FontSize="14" Foreground="#FFFFFF"
                 FontWeight="SemiBold" TextWrapping="Wrap"/>
      <TextBlock x:Name="DetailText" FontSize="12" Foreground="#CFCFCF"
                 TextWrapping="Wrap" Margin="0,6,0,0"/>
      <TextBlock x:Name="RiskText" FontSize="12" Foreground="#9E9E9E" Margin="0,10,0,0"/>
      <TextBlock x:Name="CountdownText" FontSize="12" Foreground="#9E9E9E" Margin="0,2,0,14"/>
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <Button x:Name="RejectBtn" Content="Reject" Width="110" Height="34"
                Margin="0,0,10,0" Background="#424242" Foreground="White" BorderThickness="0"/>
        <Button x:Name="AllowBtn" Content="Allow" Width="110" Height="34"
                Background="$accent" Foreground="White" BorderThickness="0" FontWeight="Bold"/>
      </StackPanel>
    </StackPanel>
  </Border>
</Window>
"@

$reader = New-Object System.Xml.XmlNodeReader ([xml]$xaml)
$win = [Windows.Markup.XamlReader]::Load($reader)

$win.FindName("ActionText").Text = $Action
$win.FindName("DetailText").Text = $Detail
$win.FindName("RiskText").Text   = "Risk: $Risk    Request: $RequestId"
$allowBtn  = $win.FindName("AllowBtn")
$rejectBtn = $win.FindName("RejectBtn")
$countdown = $win.FindName("CountdownText")

$script:choice = $null
$script:remaining = $TimeoutSeconds
$countdown.Text = "Auto-reject in $script:remaining s"

$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromSeconds(1)
$timer.Add_Tick({
    $script:remaining--
    if ($script:remaining -le 0) {
        $timer.Stop(); $script:choice = "timeout"; $win.Close()
    } else {
        $countdown.Text = "Auto-reject in $script:remaining s"
    }
})

$allowBtn.Add_Click({  $timer.Stop(); $script:choice = "allow";  $win.Close() })
$rejectBtn.Add_Click({ $timer.Stop(); $script:choice = "reject"; $win.Close() })

$win.Add_Loaded({ $win.Activate() | Out-Null })
$timer.Start()
$win.ShowDialog() | Out-Null

switch ($script:choice) {
    "allow"   { Write-Decision -decision "approve" -approver $env:USERNAME; Write-Output "ALLOW";  exit $ExitAllow }
    "reject"  { Write-Decision -decision "deny"    -approver $env:USERNAME; Write-Output "REJECT"; exit $ExitReject }
    "timeout" { Write-Decision -decision "deny"    -approver "timeout";     Write-Output "TIMEOUT"; exit $ExitTimeout }
    default   { Write-Decision -decision "deny"    -approver "closed";      Write-Output "CLOSED";  exit $ExitClosed }
}
