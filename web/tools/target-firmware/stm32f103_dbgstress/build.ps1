<#
  F103 调试器压力测试靶子固件编译脚本（不需要 make 也不需要 Keil）

    pwsh -File build.ps1              # 默认：DWARF 4 → build\fw.elf（ZE）
    pwsh -File build.ps1 -Board cb    # F103CB → build-cb\fw.elf
    pwsh -File build.ps1 -Dwarf5      # 同源码、换成 DWARF 5 → build-dw5\fw.elf
    pwsh -File build.ps1 -Clean

  依赖：arm-none-eabi-gcc 在 PATH 里（本机在 E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin）

  这是 `../stm32h743_dbgstress/` 的 **F103ZE 版**：源码逐字相同（多文件是故意的 ——
  `b engine.c:NN` 这种跨文件断点才有意义），只换编译目标和链接脚本：

    · `-mcpu=cortex-m3 -mthumb`（H7 那份是 cortex-m7 + 硬浮点）
    · **没有 FPU**：`double` 走软件浮点（soft-float 是默认值，不用额外开关）
    · 链接脚本 `ld\stm32f103ze.ld`：512KB flash / 64KB SRAM（H7 那份变量必须放 AXI SRAM）

  产物保证带 **DWARF + .symtab**（-g3 且不 strip）—— 页面的符号/行号/类型全靠它。
#>
param([ValidateSet('Os', 'Og')][string]$Optimization = 'Os', [switch]$Clean, [switch]$Dwarf5, [ValidateSet('ze', 'cb')][string]$Board = 'ze')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$outName = if ($Board -eq 'cb') { if ($Dwarf5) { 'build-cb-dw5' } else { 'build-cb' } }
          elseif ($Dwarf5) { 'build-dw5' } else { 'build' }
if ($Optimization -ne 'Os') { $outName += '-' + $Optimization.ToLower() }
$build = Join-Path $root $outName
$ldpath = Join-Path $root ('ld\stm32f103' + $Board + '.ld')
if (-not (Test-Path $ldpath)) { throw "找不到链接脚本 $ldpath" }

$gcc = (Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue).Source
if (-not $gcc){
  $guess = 'E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe'
  if (Test-Path $guess){ $gcc = $guess } else { throw 'arm-none-eabi-gcc 不在 PATH 里' }
}
$bin = Split-Path -Parent $gcc
$objcopy = Join-Path $bin 'arm-none-eabi-objcopy.exe'
$size    = Join-Path $bin 'arm-none-eabi-size.exe'
$nm      = Join-Path $bin 'arm-none-eabi-nm.exe'

if ($Clean -and (Test-Path $build)) { Remove-Item $build -Recurse -Force }
New-Item -ItemType Directory -Force -Path $build | Out-Null

$sources = @(
  (Join-Path $root 'src\main.c'),
  (Join-Path $root 'src\engine.c'),
  (Join-Path $root 'src\model.c'),
  (Join-Path $root 'src\startup.c'),
  (Join-Path $root '..\common\dbg_frames.c')
)
$elf = Join-Path $build 'fw.elf'

# 参数一律加引号并用数组 splat（PowerShell 会把 -specs=nano.specs 按点号拆成两段）
$cflags = @(
  '-mcpu=cortex-m3', '-mthumb',
  ('-' + $Optimization), '-g3', '-fasynchronous-unwind-tables',
  '-ffunction-sections', '-fdata-sections', '-fno-common',
  '-Wall', '-Wextra', '-Wno-unused-parameter',
  "-I$root\src",
  "-T$ldpath",
  '-nostartfiles', '-specs=nano.specs', '-specs=nosys.specs',
  '-Wl,--gc-sections', "-Wl,-Map=$build\fw.map"
)
# 两个 DWARF 版本都要能跑：真 ELF 里 4/5 都有（GCC 11+ 默认 5），解析器两边都得认
$cflags += if ($Dwarf5) { '-gdwarf-5' } else { '-gdwarf-4' }

& $gcc @cflags @sources -o $elf
if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit $LASTEXITCODE)" }

# Bind the oracle to the exact ELF, compiler flags and shared-source content.
$sourceHashes = @{}
foreach ($source in $sources) { $sourceHashes[(Split-Path -Leaf $source)] = (Get-FileHash $source -Algorithm SHA256).Hash.ToLower() }
$buildInfo = @{
  schema = 1; board = $(if ($Board -eq 'cb') { 'f103cb' } else { 'f103ze' }); optimization = $Optimization
  dwarf = $(if ($Dwarf5) { 5 } else { 4 })
  elfSha256 = (Get-FileHash $elf -Algorithm SHA256).Hash.ToLower()
  compiler = ((& $gcc --version | Select-Object -First 1) -join '')
  flags = $cflags; sources = $sourceHashes
}
$buildInfo | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $build 'build-info.json')

$binout = Join-Path $build 'fw.bin'
$hexout = Join-Path $build 'fw.hex'
& $objcopy -O binary $elf $binout
& $objcopy -O ihex   $elf $hexout
& $size $elf

Write-Output ""
Write-Output ("board   ： {0}" -f $Board)
Write-Output ("DWARF   ： {0}" -f ($(if ($Dwarf5) { '5' } else { '4' })))
Write-Output ("产物    ： {0}" -f $elf)
Write-Output ("          {0} ({1} B)" -f $binout, (Get-Item $binout).Length)
Write-Output ""
Write-Output "关键符号（压测脚本要按这些地址/名字断言）："
& $nm -S --size-sort $elf | Select-String -Pattern '\s(g_\w+|engine_\w+|deep_l\d|is_even|is_odd|fn_\w+|model_\w+|SysTick_Handler|PendSV_Handler)$' |
  ForEach-Object { "  " + $_.Line.Trim() }
Write-Output ""

# 默认版额外复制一份到本目录根 —— 与其它靶子同一套约定（build*/ 被 .gitignore 忽略）
if (-not $Dwarf5 -and $Board -eq 'ze'){
  Copy-Item $elf (Join-Path $root 'fw.elf') -Force
  Write-Output ("已复制给用户下载： {0}" -f (Join-Path $root 'fw.elf'))
} else {
  Write-Output '（DWARF 5 变体只留在构建目录里，不覆盖根上那份默认 fw.elf）'
}
Write-Output ""
