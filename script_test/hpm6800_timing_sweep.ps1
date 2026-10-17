# Sweep the JTAG DMI timing knobs and report throughput + integrity per point.
#
# Each point is a full build + DFU flash + self-check + read benchmark, so a point
# costs ~2 minutes.
#
# ⚠ 自检（hpm6800_selfcheck.py）是**门禁**：这里的失败模式极隐蔽 —— 请求被移成
#   全 0 时基准照跑、rderr/wderr 全 0、idcode/dtmcs 也还是对的，只有自检会报
#   "读回全 0"。所以看速率之前先看 PASS/FAIL，FAIL 的速率一律不算数。
#
# 默认表跑的是**边界**（现行值 + 每项各降一档），用来复核回归基线表里那几条判据；
# 想找新余量就在最前面加更激进的行。
#
# 用法: powershell -File script_test\hpm6800_timing_sweep.ps1
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# cap_high, tck_low, nav_low, nav_high   预期
$points = @(
    @(1, 4, 6, 4),   # 现行默认 —— PASS，~1505 KB/s（有效 TCK ~20.7 MHz）
    @(0, 4, 6, 4),   # 采样点少 1 拍 —— FAIL（上升沿到 lw 需 >= 9 拍）
    @(1, 2, 6, 4),   # 移位相位 TCK 低电平太窄 —— FAIL，校验和 0x11D9A168
    @(1, 4, 4, 4),   # TMS 建立不够 —— FAIL，读回全 0
    @(1, 4, 6, 2)    # 导航高电平压到 2 —— 探边界用，可能仍 PASS
)

foreach ($p in $points) {
    $cap = $p[0]; $low = $p[1]; $nl = $p[2]; $nh = $p[3]
    Write-Output ""
    Write-Output "======== cap=$cap tck_low=$low nav=$nl/$nh ========"
    $env:DMI_CAP_HIGH_NOP = "$cap"
    $env:DMI_TCK_LOW_NOP = "$low"
    $env:DMI_NAV_LOW_NOP = "$nl"
    $env:DMI_NAV_HIGH_NOP = "$nh"

    python -u script_test\hpm6800_flash_probe.py 2>&1 | Select-String -Pattern 'probe is back|Traceback|RuntimeError' | ForEach-Object { "  $($_.Line.Trim())" }
    Start-Sleep -Seconds 2
    python -u script_test\hpm6800_probe.py set-mode 1 2>&1 | Out-Null
    python -u script_test\hpm6800_selfcheck.py 2>&1 | Select-String 'checksum|RESULT' | ForEach-Object { "  $($_.Line.Trim())" }
    python -u script_test\hpm6800_riscv.py open 2>&1 | Out-Null
    python -u script_test\hpm6800_riscv.py rbench 0x1200000 1024 50 2>&1 | Select-String 'rate =|moved=' | ForEach-Object { "  R $($_.Line.Trim())" }
}
Write-Output ""
Write-Output "sweep done (注意：只有 RESULT: PASS 的那几行速率才算数)"
