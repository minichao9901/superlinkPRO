# 统一的外部工具调用封装（gcc / objcopy / size / nm / objdump）。
#
# 为什么不用 `& $exe @args`：在本机 pwsh 7 里，`& <arm-none-eabi-*.exe>` 会报
# "无法在管道中间执行"（实测，与路径写法无关）；Start-Process 调同一个 exe 正常。
# 参数用数组传，避免空格/引号再被二次解析。
function Invoke-Tool {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Args = @(),
        [string]$StdoutFile = $null
    )
    # Start-Process -ArgumentList 直接把字符串拼成命令行，不会转义参数内部的双引号。
    # 所以这里手动转义：内部 " -> \" ，整体加引号（含空格或引号的参数）。
    # 例：-DRTT_CB_ATTR=__attribute__((section(".rtt_cb"),aligned(4)))
    $quoted = foreach ($a in $Args) {
        if ($a -match '[\s"]' -or $a -eq '') {
            '"' + ($a -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
        } else { $a }
    }
    $p = @{
        FilePath     = $Exe
        ArgumentList = $quoted
        Wait         = $true
        PassThru     = $true
        NoNewWindow  = $true
    }
    $tmpErr = [System.IO.Path]::GetTempFileName()
    $p.RedirectStandardError = $tmpErr
    if ($StdoutFile) {
        $tmpOut = $StdoutFile
    } else {
        $tmpOut = [System.IO.Path]::GetTempFileName()
    }
    $p.RedirectStandardOutput = $tmpOut
    $proc = Start-Process @p
    $err = Get-Content -LiteralPath $tmpErr -Raw -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmpErr -Force -ErrorAction SilentlyContinue
    if (-not $StdoutFile) {
        $out = Get-Content -LiteralPath $tmpOut -Raw -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $tmpOut -Force -ErrorAction SilentlyContinue
    }
    if ($err) { [Console]::Error.Write($err) }
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; Stdout = $out }
}
Export-ModuleMember -Function Invoke-Tool
