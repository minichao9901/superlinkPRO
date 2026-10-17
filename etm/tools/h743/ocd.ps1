# 通用 OpenOCD 读写助手（默认末尾 resume）。命令用 ';;' 分隔，避免 pwsh -File 的数组绑定坑：
#   pwsh -NoProfile -File tmp\ocd.ps1 -Cmds 'mdw 0x20003000 10;;reg pc'
param(
    [string]$Cmds = '',
    [switch]$NoResume,
    [int]$SpeedKhz = 1000
)
$ocd = 'E:\Share\env-windows\rtt-debugger-support-package-0.4.0\bin\openocd.exe'
$scr = 'E:\Share\env-windows\rtt-debugger-support-package-0.4.0\bin\scripts'
$a = @('-s', $scr, '-f', 'interface/cmsis-dap.cfg', '-c', 'transport select swd',
       '-f', 'target/stm32h7x.cfg', '-c', "adapter speed $SpeedKhz", '-c', 'init', '-c', 'halt')
foreach ($c in ($Cmds -split ';;')) { if ($c.Trim()) { $a += @('-c', $c.Trim()) } }
if (-not $NoResume) { $a += @('-c', 'resume') }
$a += @('-c', 'shutdown')
$raw = & $ocd @a 2>&1 | Out-String
$raw -split "`r?`n" | Where-Object {
    $_ -and $_ -notmatch 'DEPRECATED|Licensed|bugs.html|Open On-Chip|^Info : CMSIS|^Info : SWCLK|^Info : clock|^Info : Using|^Info : SWD|gdb|^Info : Listening|^Info : starting'
} | ForEach-Object { $_.TrimEnd() }
