# Restart the akaLinkPro (HPM5301EVKLite DAP) USB composite device:
# forces re-enumeration, which resets the firmware's DAP/USB engine.
$dev = Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -like 'USB\VID_0D28&PID_0204*' -and $_.InstanceId -notlike '*MI_*'
} | Select-Object -First 1

if (-not $dev) {
    Write-Output "akaLinkPro device not found"
    exit 1
}
Write-Output "Restarting: $($dev.InstanceId)"
Disable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false
Start-Sleep -Seconds 2
Enable-PnpDevice -InstanceId $dev.InstanceId -Confirm:$false
Start-Sleep -Seconds 3
$dev2 = Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -like 'USB\VID_0D28&PID_0204*' -and $_.InstanceId -notlike '*MI_*'
} | Select-Object -First 1
Write-Output "After restart: $($dev2.Status) $($dev2.FriendlyName)"
