# The OBDX Pro GT by USB VID/PID (0483:5740); never a guessed COM number.
$gt = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPDeviceID -like 'USB\VID_0483&PID_5740\*' } | ForEach-Object { if ($_.Name -match '\((COM\d+)\)') { $Matches[1] } })
if ($gt.Count -ne 1) { throw "Expected exactly one OBDX Pro GT (USB 0483:5740), found $($gt.Count): $($gt -join ', ')" }
$port = New-Object System.IO.Ports.SerialPort($gt[0], 115200)
$port.ReadTimeout = 500
$port.Open(); Start-Sleep -Milliseconds 200
function Cmd($c,$w){ try{$port.DiscardInBuffer()}catch{}; $port.Write($c+"`r"); Start-Sleep -Milliseconds $w; $r=""; try{ while($port.BytesToRead -gt 0){ $r+=[char]$port.ReadChar(); if($r[-1] -eq [char]62){break} } }catch{}; return ($r -replace '[\r\n]','').Trim() }
foreach($c in @("ATZ","ATE0","ATL0","ATS0","ATH0","ATSP6","ATAT0","ATST14","ATSH7E0")){ Cmd $c 600 | Out-Null }
$out = "D:\Projects\OpenOBD\tools\ecm_dids.tsv"
Set-Content -Encoding ASCII $out "# did`thexdata"
$log = "D:\Projects\OpenOBD\tools\ecm_dids.log"
Set-Content -Encoding ASCII $log ("SCAN START " + (Get-Date -Format o))
$found = 0
foreach($d in 0x1000..0x1FFF){
  $did = "{0:X4}" -f $d
  $resp = Cmd ("22"+$did) 90
  $hex = ($resp -replace '[^0-9A-Fa-f]','').ToUpper()
  $hdr = "62" + $did
  $idx = $hex.IndexOf($hdr)
  if($idx -ge 0){
    $data = $hex.Substring($idx + $hdr.Length)
    Add-Content -Encoding ASCII $out ($did + "`t" + $data)
    $found++
  }
  if(($d -band 0xFF) -eq 0xFF){ Add-Content -Encoding ASCII $log ("...through " + $did + " found=" + $found) }
}
Add-Content -Encoding ASCII $log ("SCAN DONE found=" + $found + " " + (Get-Date -Format o))
$port.Close()
