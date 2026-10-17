# 从 KingstVIS 采集五路 trace，保存 CSV 和 KVData，再做物理层分析。
param([double]$Seconds = 0.01, [long]$Rate = 500000000, [string]$Out)
$ErrorActionPreference = 'Stop'
if ($Seconds -le 0 -or $Rate -le 0) { throw '采集时间、采样率必须大于零。' }
if (-not $Out) {
    $traceStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Out = Join-Path $PSScriptRoot "captures\trace-$traceStamp.csv"
}
$traceOutputPath = [System.IO.Path]::GetFullPath($Out)
if (Test-Path -LiteralPath $traceOutputPath) { throw "文件已存在：$traceOutputPath" }
New-Item -ItemType Directory -Path (Split-Path -Parent $traceOutputPath) -Force | Out-Null
& python -X utf8 "$PSScriptRoot\tools\la\kingst_la.py" capture --rate $Rate --time $Seconds `
    --channels 0 1 2 3 4 --threshold 1.65 --out $traceOutputPath --stats
if ($LASTEXITCODE -ne 0) { throw '采集失败，请检查 KingstVIS、Socket API 和设备状态。' }
& python -X utf8 "$PSScriptRoot\tools\trace_port.py" $traceOutputPath
if ($LASTEXITCODE -ne 0) { throw '波形分析失败；原始采集文件已保留。' }
