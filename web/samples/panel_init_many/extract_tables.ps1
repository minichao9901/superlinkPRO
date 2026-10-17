# Mechanical fallback extractor: parse init tables / config structs / resolution from driver .c files.
# Usage: pwsh -File panel_init/extract_tables.ps1 [Root]
param([string]$Root = (Get-Location).Path)

$sdk = Join-Path $Root 'SiFli-SDK-main'
$dumpDir = Join-Path $Root 'panel_init\dumps'
if (-not (Test-Path $dumpDir)) { New-Item -ItemType Directory -Path $dumpDir | Out-Null }

# id -> relative file path (all 44; part01.json already done but harmless to re-dump)
$files = @{
  'atk7016'='customer/peripherals/atk7016/atk7016.c'
  'ek79001'='customer/peripherals/ek79001/ek79001.c'
  'ek79202'='customer/peripherals/ek79202/ek79202.c'
  'jd9165a'='customer/peripherals/jd9165a/jd9165a.c'
  'qx_4300j40r38'='customer/peripherals/qx_4300j40r38/qx_4300j40r38.c'
  'htm_h070a20'='customer/peripherals/htm_h070a20/htm_h070a20.c'
  'nv3052c'='customer/peripherals/dpi_nv3052c/nv3052c.c'
  'jdi387a'='customer/peripherals/jdi387a/jdi387a.c'
  'ls013b7dd02'='customer/peripherals/ls013b7dd02/ls013b7dd02.c'
  'st7789v'='customer/peripherals/st7789v/st7789v.c'
  'st7789h2'='customer/peripherals/st7789h2/st7789h2.c'
  'st7789_dbi'='customer/peripherals/ST7789_DBI/st7789_dbi.c'
  'st7789_gmt024_08_spi8p'='customer/peripherals/st7789_gmt024_08_spi8p/st7789_gmt024_08_spi8p.c'
  'st7789_gmt024_08_spi8p_rot90'='customer/peripherals/st7789_gmt024_08_spi8p_rot90/st7789_gmt024_08_spi8p_rot.c'
  'gc9a01'='customer/peripherals/gc9a01/gc9a01.c'
  'st7701s'='customer/peripherals/st7701s/st7701s.c'
  'st77903'='customer/peripherals/st77903/st77903.c'
  'st7797'='customer/peripherals/st7797/st7797.c'
  'st77916'='customer/peripherals/st77916/st77916.c'
  'st77922'='customer/peripherals/st77922/st77922.c'
  'gc9307'='customer/peripherals/gc9307/gc9307.c'
  'gc9c01'='customer/peripherals/gc9c01/gc9c01.c'
  'gc9b71'='customer/peripherals/gc9b71/gc9b71.c'
  'gc9b72'='customer/peripherals/gc9b72/gc9b72.c'
  'ili9327'='customer/peripherals/ili9327/ili9327.c'
  'ili8688e'='customer/peripherals/ili8688e/ili8688e.c'
  'ft2308'='customer/peripherals/ft2308/ft2308.c'
  'jd9851'='customer/peripherals/jd9851/jd9851.c'
  'jd9365da'='customer/peripherals/jd9365da/jd9365da.c'
  'nv3041a'='customer/peripherals/nv3041a/nv3041a.c'
  'nv3051f1'='customer/peripherals/nv3051f1/nv3051f1.c'
  'rm6d010'='customer/peripherals/rm6d010/RM6D010.c'
  'rm69090'='customer/peripherals/rm69090/rm69090.c'
  'rm690C0'='customer/peripherals/rm690C0/rm690C0.c'
  'rm69330'='customer/peripherals/rm69330/rm69330.c'
  'sh8601'='customer/peripherals/sh8601/sh8601.c'
  'sh8601a'='customer/peripherals/sh8601a/sh8601a.c'
  'spd2012'='customer/peripherals/spd2012/spd2012.c'
  'axs15231b'='customer/peripherals/axs15231b/axs15231b.c'
  'axs15231e'='customer/peripherals/axs15231e/axs15231e.c'
  'xm80240'='customer/peripherals/xm80240/xm80240.c'
  'icn3311'='customer/peripherals/icn3311/icn3311.c'
  'icn3311b'='customer/peripherals/icn3311b/icn3311b.c'
  'lcd_icn3311'='customer/peripherals/lcd_icn3311/icn3311.c'
}

function ToNum([string]$s) {
  $t = $s.Trim()
  if ($t -match '^0[xX][0-9A-Fa-f]+$') { return [Convert]::ToInt64($t.Substring(2), 16) }
  return [long]$t
}

function Get-Block([string]$text, [int]$start) {
  # $start = index of '{'. Return substring through matching '}'.
  $depth = 0
  for ($i = $start; $i -lt $text.Length; $i++) {
    $ch = $text[$i]
    if ($ch -eq '{') { $depth++ }
    elseif ($ch -eq '}') {
      $depth--
      if ($depth -eq 0) { return $text.Substring($start, $i - $start + 1) }
    }
  }
  return $null
}

foreach ($id in ($files.Keys | Sort-Object)) {
  $path = Join-Path $sdk ($files[$id] -replace '/', '\')
  if (-not (Test-Path $path)) { Write-Host "SKIP missing: $id"; continue }
  $src = Get-Content -Raw -Path $path

  $info = [ordered]@{ id = $id; file = $files[$id] }

  # export name / align
  $mEx = [regex]::Match($src, 'LCD_DRIVER_EXPORT2\(\s*(\w+)\s*,')
  $mAl = [regex]::Match($src, 'LCD_DRIVER_EXPORT2\([^;]*?,\s*(\d+)\s*\)\s*;')
  $info.registered_name = if ($mEx.Success) { $mEx.Groups[1].Value } else { $null }
  $info.pixel_align = if ($mAl.Success) { [int]$mAl.Groups[1].Value } else { $null }

  # resolution
  $mW = [regex]::Match($src, '#define\s+\w*(?:THE_LCD|LCD_IC)_PIXEL_(?:WIDTH)\s+\(?\s*(?:\(uint16_t\)\s*)?(\d+)')
  $mH = [regex]::Match($src, '#define\s+\w*(?:THE_LCD|LCD_IC)_PIXEL_(?:HEIGHT)\s+\(?\s*(?:\(uint16_t\)\s*)?(\d+)')
  $info.res_w = if ($mW.Success) { [int]$mW.Groups[1].Value } else { $null }
  $info.res_h = if ($mH.Success) { [int]$mH.Groups[1].Value } else { $null }

  # LCDC config structs
  $configs = @()
  foreach ($mc in [regex]::Matches($src, 'static\s+(?:const\s+)?LCDC_InitTypeDef\s+(\w+)\s*=\s*\{')) {
    $block = Get-Block $src $src.IndexOf('{', $mc.Index)
    if (-not $block) { continue }
    $cfg = [ordered]@{ name = $mc.Groups[1].Value }
    $mi = [regex]::Match($block, '\.lcd_itf\s*=\s*(\w+)'); $cfg.itf = if ($mi.Success) { $mi.Groups[1].Value } else { $null }
    $mf = [regex]::Match($block, '\.freq\s*=\s*(\d+)'); $cfg.freq = if ($mf.Success) { [double]$mf.Groups[1].Value } else { $null }
    $mc2 = [regex]::Match($block, '\.color_mode\s*=\s*(\w+)'); $cfg.color_mode = if ($mc2.Success) { $mc2.Groups[1].Value } else { $null }
    # dpi / timing fields
    $timing = [ordered]@{}
    foreach ($tk in @('PCLK_polarity','DE_polarity','VS_polarity','HS_polarity','PCLK_force_on','VS_width','HS_width','VBP','VAH','VFP','HBP','HAW','HFP','interrupt_line_num')) {
      $mt = [regex]::Match($block, "\.$tk\s*=\s*(\d+)")
      if ($mt.Success) { $timing[$tk] = [int]$mt.Groups[1].Value }
    }
    if ($timing.Count -gt 0) { $cfg.timing = $timing }
    $configs += $cfg
  }
  $info.configs = $configs

  # init tables: static const uint8_t name[][X] = { ... };
  $tables = @()
  foreach ($mtb in [regex]::Matches($src, 'static\s+const\s+uint8_t\s+(\w+)\s*\[\s*\]\s*\[\s*\w+\s*\]\s*=\s*\{')) {
    $block = Get-Block $src $src.IndexOf('{', $mtb.Index)
    if (-not $block) { continue }
    $rows = @()
    foreach ($mr in [regex]::Matches($block, '\{\s*(0x[0-9A-Fa-f]+|\d+)\s*,\s*(\d+)\s*,\s*((?:0x[0-9A-Fa-f]+|\d+)(?:\s*,\s*(?:0x[0-9A-Fa-f]+|\d+))*)\s*\}')) {
      $cmd = [int](ToNum $mr.Groups[1].Value)
      $len = [int]$mr.Groups[2].Value
      $bytes = [System.Collections.Generic.List[int]]::new()
      foreach ($b in $mr.Groups[3].Value -split ',') { $bytes.Add([int](ToNum $b)) }
      if ($bytes.Count -gt $len) { $bytes = $bytes.GetRange(0, $len) }
      $rows += ,@($cmd, $bytes)
    }
    $tables += [ordered]@{ name = $mtb.Groups[1].Value; row_count = $rows.Count; rows = $rows }
  }
  $info.tables = $tables

  # rough inline-write count (diagnostic)
  $info.lcd_writereg_count = ([regex]::Matches($src, 'LCD_WriteReg\s*\(')).Count
  $info.hal_delay_count = ([regex]::Matches($src, 'LCD_DRIVER_DELAY_MS|HAL_Delay\b')).Count
  $info.hal_delayus_count = ([regex]::Matches($src, 'HAL_Delay_us')).Count

  $outPath = Join-Path $dumpDir ($id + '.json')
  $info | ConvertTo-Json -Depth 100 | Set-Content -Path $outPath -Encoding UTF8
  Write-Host ("{0}: export={1} res={2}x{3} configs={4} tables={5} writereg={6} delayms={7}" -f $id, $info.registered_name, $info.res_w, $info.res_h, $configs.Count, $tables.Count, $info.lcd_writereg_count, $info.hal_delay_count)
}
Write-Host 'DONE'
