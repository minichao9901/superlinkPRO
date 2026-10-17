# Merge panel_init/parts/part*.json -> panel_init.json + 基础校验
# 用法: pwsh -File panel_init/merge.ps1 [工作目录]
param([string]$Root = (Get-Location).Path)

$partsDir = Join-Path $Root 'panel_init\parts'
if (-not (Test-Path $partsDir)) { Write-Error "parts dir not found: $partsDir"; exit 1 }

$parts = Get-ChildItem -Path $partsDir -Filter 'part*.json' | Sort-Object Name
if ($parts.Count -eq 0) { Write-Error "no part files found"; exit 1 }

$drivers = [System.Collections.Generic.List[object]]::new()
$missing = @()
foreach ($p in $parts) {
    try {
        $arr = Get-Content -Raw -Path $p.FullName | ConvertFrom-Json
        if ($arr -isnot [array]) { Write-Error "$($p.Name): NOT an array"; exit 1 }
        foreach ($d in $arr) { $drivers.Add($d) }
    } catch {
        Write-Error "$($p.Name): JSON parse failed -> $($_.Exception.Message)"; exit 1
    }
    Write-Host ("{0}: {1} entries" -f $p.Name, $arr.Count)
}

# 排序 + id 唯一性检查
$drivers = $drivers | Sort-Object id
$ids = $drivers | ForEach-Object { $_.id }
$dups = $ids | Group-Object | Where-Object Count -gt 1
if ($dups) { Write-Error ("duplicate ids: " + (($dups | ForEach-Object Name) -join ', ')); exit 1 }

# 基础字段校验
$issues = [System.Collections.Generic.List[string]]::new()
$required = @('id','registered_name','ic','file','pixel_align','interface_options','init_sequences','display_on_sequence','display_off_sequence','notes')
foreach ($d in $drivers) {
    foreach ($f in $required) {
        if ($null -eq $d.PSObject.Properties[$f]) { $issues.Add("$($d.id): missing field $f") }
    }
    foreach ($s in $d.init_sequences) {
        foreach ($st in $s.steps) {
            if ($null -eq $st.PSObject.Properties['type']) { $issues.Add("$($d.id)/$($s.label): step missing type") }
            elseif ($st.type -eq 'cmd') {
                if ($null -eq $st.PSObject.Properties['cmd']) { $issues.Add("$($d.id): cmd step without cmd") }
                if ($null -eq $st.PSObject.Properties['data']) { $issues.Add("$($d.id): cmd step without data") }
            }
        }
    }
}

$totalSteps = ($drivers | ForEach-Object { $_.init_sequences | ForEach-Object { $_.steps.Count } } | Measure-Object -Sum).Sum
$noInit = @($drivers | Where-Object { $_.init_sequences.Count -eq 0 } | ForEach-Object id)

# 把 cmd/data 转为 0xNN hex 字符串（字节域），其余字段保持十进制
# cmd 按值宽度自适应：<=0xFF 用 2 位（如 0x11），>0xFF 用 4 位（如 FT2308 的 0x2900）
function Convert-StepsToHex($steps) {
    foreach ($st in $steps) {
        if ($st.type -eq 'cmd' -and $null -ne $st.PSObject.Properties['cmd']) {
            $st.cmd = '0x{0:x2}' -f [int]$st.cmd
            $dta = @($st.data)
            for ($i = 0; $i -lt $dta.Count; $i++) { $dta[$i] = '0x{0:x2}' -f [int]$dta[$i] }
            $st.data = $dta
        }
    }
    return $steps
}

foreach ($d in $drivers) {
    foreach ($s in $d.init_sequences) { $s.steps = Convert-StepsToHex $s.steps }
    $d.display_on_sequence = Convert-StepsToHex $d.display_on_sequence
    $d.display_off_sequence = Convert-StepsToHex $d.display_off_sequence
}

$archive = [ordered]@{
    meta = [ordered]@{
        title = 'SF32 LCD Panel Init Archive'
        version = '1.0'
        generated = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        source_tree = 'SiFli-SDK-main/customer/peripherals'
        description = 'Extracted from LCD_DRIVER_EXPORT2 panel drivers; see panel_init/SPEC.md for field semantics.'
        driver_count = $drivers.Count
        init_sequence_count = ($drivers | ForEach-Object { $_.init_sequences.Count } | Measure-Object -Sum).Sum
        total_steps = $totalSteps
        drivers_without_init = $noInit
    }
    drivers = @($drivers)
}

$out = Join-Path $Root 'panel_init.json'
$archive | ConvertTo-Json -Depth 100 | Set-Content -Path $out -Encoding UTF8

Write-Host '---------------------------------------'
Write-Host ("drivers: {0}" -f $drivers.Count)
Write-Host ("init_sequences: {0}" -f $archive.meta.init_sequence_count)
Write-Host ("total steps: {0}" -f $totalSteps)
Write-Host ("schema issues: {0}" -f $issues.Count)
$issues | ForEach-Object { Write-Host ("  ISSUE: {0}" -f $_) }
Write-Host ("written: {0}" -f $out)
Write-Host ("sizes(MB): {0:N2}" -f ((Get-Item $out).Length / 1MB))
