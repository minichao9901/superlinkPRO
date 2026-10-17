# Recover an akaLinkPro / HPM5301EVKLite probe that disappeared from USB.
#
# The probe hangs off one of the external hubs; power-cycling that hub
# re-enumerates it (and, because the board is USB-powered, resets the MCU too).
# Only a hub with no *present* children is cycled, so nothing else is disturbed.
#
# Usage: powershell -File script_test\probe_usb_recover.ps1
$ErrorActionPreference = 'Stop'

function Get-ProbeHub {
    # Preferred: walk up from the probe's own (possibly stale) registry entry.
    $probe = Get-PnpDevice | Where-Object { $_.InstanceId -like 'USB\VID_0D28&PID_0204\*' -and $_.InstanceId -notlike '*MI_*' } |
             Select-Object -First 1
    if ($probe) {
        $parent = (Get-PnpDeviceProperty -InstanceId $probe.InstanceId -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data
        if ($parent -and $parent -like 'USB\*') { return $parent }
    }
    return $null
}

function Get-HubChildren($hub) {
    $kids = @()
    foreach ($d in (Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\*' })) {
        $p = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data
        if ($p -eq $hub) { $kids += $d }
    }
    return $kids
}

$present = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like '*VID_0D28&PID_0204*' }
if ($present) {
    Write-Output "probe already present: $($present[0].FriendlyName)"
    exit 0
}

$hub = Get-ProbeHub
if (-not $hub) {
    Write-Output "probe hub not identified; nothing to do"
    exit 1
}

$kids = Get-HubChildren $hub
if ($kids.Count -gt 0) {
    Write-Output "hub $hub still has $($kids.Count) child device(s); refusing to cycle it:"
    $kids | ForEach-Object { Write-Output "  $($_.FriendlyName)" }
    exit 1
}

Write-Output "power-cycling $hub ..."
Disable-PnpDevice -InstanceId $hub -Confirm:$false
Start-Sleep -Seconds 3
Enable-PnpDevice -InstanceId $hub -Confirm:$false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    $p = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like '*VID_0D28&PID_0204*' }
    if ($p) {
        Write-Output "probe back after $($i + 1)s: $(($p | Where-Object { $_.FriendlyName -like '*CMSIS-DAP*' -or $_.FriendlyName -like '*大容量*' } | Select-Object -First 1).FriendlyName)"
        exit 0
    }
}
Write-Output "probe did not come back"
exit 1
