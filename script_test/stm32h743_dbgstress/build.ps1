<#
  调试器压力测试靶子固件编译脚本（STM32H743，不需要 make 也不需要 Keil）

    pwsh -File build.ps1              # 默认：DWARF 4 → build\fw.elf
    pwsh -File build.ps1 -Dwarf5      # 同源码、换成 DWARF 5 → build-dw5\fw.elf（压测解析器）
    pwsh -File build.ps1 -Clean

  依赖：arm-none-eabi-gcc 在 PATH 里（本机在 E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin）

  为什么要有这个靶子：调试器页（#dbg）要对外发布，需要一块**结构足够复杂**的固件来压
  断点 / 代码同步 / 单步（in-out-over）/ 复位重跑 / 结构体树。兄弟例程 scope 与 rtt_speed
  都太"平"（就一个 main + 一个自旋），压不出问题来。

  四个源文件（多文件是故意的：`b engine.c:NN` 这种跨文件断点才有意义）：
    src/main.c     主循环 11 段流水线 + SysTick/PendSV 两个中断
    src/engine.c   调用链：6 层嵌套、递归、互递归、函数指针表、分支循环、内联
    src/model.c    复杂结构体：嵌套/数组/联合/位域/指针链表/const 对象
    src/startup.c  向量表 + FPU 放行 + .data/.bss 初始化

  产物保证带 **DWARF + .symtab**（-g3 且不 strip）—— 页面的符号/行号/类型全靠它。
#>
param([switch]$Clean, [switch]$Dwarf5)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$outName = if ($Dwarf5) { 'build-dw5' } else { 'build' }
$build = Join-Path $root $outName
$ldpath = Join-Path $root 'ld\stm32h743.ld'
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
  (Join-Path $root 'src\startup.c')
)
$elf = Join-Path $build 'fw.elf'

# 参数一律加引号并用数组 splat（PowerShell 会把 -specs=nano.specs 按点号拆成两段）
$cflags = @(
  '-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard',
  '-Os', '-g3',
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

$binout = Join-Path $build 'fw.bin'
$hexout = Join-Path $build 'fw.hex'
& $objcopy -O binary $elf $binout
& $objcopy -O ihex   $elf $hexout
& $size $elf

Write-Output ""
Write-Output ("DWARF   ： {0}" -f ($(if ($Dwarf5) { '5' } else { '4' })))
Write-Output ("产物    ： {0}" -f $elf)
Write-Output ("          {0} ({1} B)" -f $binout, (Get-Item $binout).Length)
Write-Output ""
Write-Output "关键符号（压测脚本要按这些地址/名字断言）："
& $nm -S --size-sort $elf | Select-String -Pattern '\s(g_\w+|engine_\w+|deep_l\d|is_even|is_odd|fn_\w+|model_\w+|SysTick_Handler|PendSV_Handler)$' |
  ForEach-Object { "  " + $_.Line.Trim() }
Write-Output ""

# 默认版额外复制一份到本目录根 —— 本仓库与 akaLinkPro/script_test 的约定
# （两边各留一份 fw.elf 给"直接下载来烧"的人用；build*/ 被 .gitignore 忽略）
if (-not $Dwarf5){
  Copy-Item $elf (Join-Path $root 'fw.elf') -Force
  Write-Output ("已复制给用户下载： {0}" -f (Join-Path $root 'fw.elf'))
} else {
  Write-Output '（DWARF 5 变体只留在构建目录里，不覆盖根上那份默认 fw.elf）'
}
Write-Output ""
