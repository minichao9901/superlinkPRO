param([int]$Port=8766)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Output "ETM 页面：http://127.0.0.1:$Port"
& python -X utf8 (Join-Path $PSScriptRoot 'server.py') --port $Port
