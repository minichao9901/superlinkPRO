# Cross-check panel_init.json against the original C driver files.
# Checks per driver: registered_name, hor_res/ver_res, interface set.
# Usage: pwsh -File panel_init/verify.ps1 [Root]
param([string]$Root = (Get-Location).Path)

$archive = Get-Content -Raw (Join-Path $Root 'panel_init.json') | ConvertFrom-Json
$sdk = Join-Path $Root 'SiFli-SDK-main'

# LCDC_INTF_* -> normalized name (must mirror SPEC.md)
$ifmap = @{
    'LCDC_INTF_DSI'='dsi'; 'LCDC_INTF_DSI_VIDEO'='dsi_video';
    'LCDC_INTF_SPI_NODCX_1DATA'='spi_nodcx_1data'; 'LCDC_INTF_SPI_NODCX_2DATA'='spi_nodcx_2data';
    'LCDC_INTF_SPI_NODCX_4DATA'='spi_nodcx_4data'; 'LCDC_INTF_SPI_DCX_1DATA'='spi_dcx_1data';
    'LCDC_INTF_SPI_DCX_2DATA'='spi_dcx_2data'; 'LCDC_INTF_SPI_DCX_4DATA'='spi_dcx_4data';
    'LCDC_INTF_SPI_DCX_4DATA_AUX'='spi_dcx_4data_aux'; 'LCDC_INTF_DBI_8BIT_B'='dbi_8bit_b';
    'LCDC_INTF_JDI_PARALLEL'='jdi_parallel'; 'AUTO_SELECTED_DPI_INTFACE'='dpi'
}

$diff = 0
foreach ($d in $archive.drivers) {
    $path = Join-Path $sdk ($d.file -replace '/', '\')
    if (-not (Test-Path $path)) { Write-Host ("MISSING FILE: {0}" -f $d.file); $diff++; continue }
    $src = Get-Content -Raw $path

    # 1. registered_name
    $mName = [regex]::Match($src, 'LCD_DRIVER_EXPORT2\(\s*(\w+)')
    if ($mName.Success -and $mName.Groups[1].Value -ne $d.registered_name) {
        Write-Host ("NAME DIFF {0}: json={1} c={2}" -f $d.id, $d.registered_name, $mName.Groups[1].Value); $diff++
    }

    # 2. resolution: first THE_LCD_PIXEL_* / LCD_IC_PIXEL_* define
    $mW = [regex]::Match($src, '#define\s+\w*(?:THE_LCD|LCD_IC)_PIXEL_(?:WIDTH)\s+\(?\s*(?:\(uint16_t\)\s*)?(\d+)')
    $mH = [regex]::Match($src, '#define\s+\w*(?:THE_LCD|LCD_IC)_PIXEL_(?:HEIGHT)\s+\(?\s*(?:\(uint16_t\)\s*)?(\d+)')
    if ($mW.Success) {
        if ([int]$mW.Groups[1].Value -ne $d.hor_res) { Write-Host ("RES DIFF {0}: json hor={1} c={2}" -f $d.id, $d.hor_res, $mW.Groups[1].Value); $diff++ }
    } elseif ($null -ne $d.hor_res) {
        Write-Host ("RES DIFF {0}: json hor={1} but no width define found" -f $d.id, $d.hor_res); $diff++
    }
    if ($mH.Success) {
        if ([int]$mH.Groups[1].Value -ne $d.ver_res) { Write-Host ("RES DIFF {0}: json ver={1} c={2}" -f $d.id, $d.ver_res, $mH.Groups[1].Value); $diff++ }
    } elseif ($null -ne $d.ver_res) {
        Write-Host ("RES DIFF {0}: json ver={1} but no height define found" -f $d.id, $d.ver_res); $diff++
    }

    # 3. interfaces: unique .lcd_itf values in file (excluding commented lines is best-effort)
    $cInterfaces = @()
    foreach ($ms in [regex]::Matches($src, '(?m)^\s*\.lcd_itf\s*=\s*(LCDC_INTF_\w+|AUTO_SELECTED_DPI_INTFACE|AUTO_SELECTED\w*)\s*,?$')) {
        $raw = $ms.Groups[1].Value
        if ($ifmap.ContainsKey($raw)) { $cInterfaces += $ifmap[$raw] } else { $cInterfaces += $raw }
    }
    $jInterfaces = @($d.interface_options | ForEach-Object { $_.interface } | Sort-Object -Unique)
    $cUnique = @($cInterfaces | Sort-Object -Unique)
    $setDiff = Compare-Object $jInterfaces $cUnique
    if ($setDiff) {
        Write-Host ("IF DIFF {0}: json=[{1}] c=[{2}]" -f $d.id, ($jInterfaces -join ','), ($cUnique -join ',')); $diff++
    }
}

Write-Host '---------------------------------------'
if ($diff -eq 0) { Write-Host 'ALL CHECKS PASSED' } else { Write-Host ("DIFFS: {0}" -f $diff) }
