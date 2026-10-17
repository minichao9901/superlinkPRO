<#
  STM32H743 · J-Scope 采样靶子固件编译脚本（不需要 make，也不需要 Keil）

    pwsh -File build.ps1                # 默认：flash 版 → build\fw.elf（并复制一份到本目录 fw.elf）
    pwsh -File build.ps1 -DCache        # ★ "H7 D-cache 干扰"实验版：cache 开、无 MPU → build-dcache\fw.elf
    pwsh -File build.ps1 -NonCache      # ★★ 推荐做法：cache 开 + MPU 把 AXI SRAM 配成非缓存 → build-noncache\fw.elf
    pwsh -File build.ps1 -Clean

  依赖：arm-none-eabi-gcc 在 PATH 里（本机在 E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin）

  ⚠️ 用户定调：**H743 走 flash 版，不做纯 RAM 运行版**（2026-10）。
     早先那个 `-Ram` 分支与 `ld/stm32h743_ram.ld` 已删除 —— 需要时从 git 历史里取。
     写不进 flash 时先查连接侧的复位方式（见 README 第 5 节第 5 条），别往"搬到 RAM 跑"上绕。

  三个版本为什么要分开（都是**同一份源码**，只差两个宏）：
    · 默认版    —— .data/.bss 在 AXI SRAM、栈在 DTCM、D-Cache 关（AHB-AP 读到的是真内存）。
    · -DCache   —— D-Cache 开、**没有 MPU**：CPU 的写停在 cache 行里，探针读物理内存 ⇒
                   变量被"冻住"（这正是要演示的坑）。
    · -NonCache —— D-Cache 开 + MPU 把 AXI SRAM(0x24000000,512KB) 配成 Normal/Non-cacheable：
                   既开了 cache，探针又能读到实时值。**这是 H7 上的推荐做法。**
    两份实验产物**故意不覆盖**默认的 fw.elf（默认产物才是仓库给用户下载的那一份）。
#>
param([switch]$Clean, [switch]$DCache, [switch]$NonCache)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$outName = 'build'
if ($DCache)  { $outName = 'build-dcache' }
if ($NonCache) { $outName = 'build-noncache' }
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
  (Join-Path $root 'src\startup.c')
)
$elf = Join-Path $build 'fw.elf'

# 参数一律加引号并用数组 splat：
# PowerShell 会把 -specs=nano.specs 按点号拆成两段（"-specs=nano" + ".specs"），
# 直接导致 "cannot read spec file 'nano'"。这是兄弟例程第一版踩的坑。
#
# -gdwarf-4 是**故意写死的**：scope 页面第一版的 DWARF 解析器只吃 DWARF 4
# （本机 GCC 10.3 默认也是 4，但 GCC 11+ 会默认切到 5 —— 显式写死免得工具链一升级就解析不出来）。
$cflags = @(
  '-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard',
  '-Os', '-g3', '-gdwarf-4',
  '-ffunction-sections', '-fdata-sections', '-fno-common',
  '-Wall', '-Wextra', '-Wno-unused-parameter',
  "-I$root\src",
  "-T$ldpath",
  '-nostartfiles', '-specs=nano.specs', '-specs=nosys.specs',
  '-Wl,--gc-sections', "-Wl,-Map=$build\fw.map"
)
if ($DCache -or $NonCache) { $cflags += '-DDCACHE_ON=1' }
if ($NonCache) { $cflags += '-DNONCACHE_MPU=1' }

& $gcc @cflags @sources -o $elf
if ($LASTEXITCODE -ne 0) { throw "编译失败 (exit $LASTEXITCODE)" }

$binout = Join-Path $build 'fw.bin'
$hexout = Join-Path $build 'fw.hex'
& $objcopy -O binary $elf $binout
& $objcopy -O ihex   $elf $hexout
& $size $elf

Write-Output ""
Write-Output ("版本    ： flash 版")
if ($NonCache) {
  Write-Output ("D-Cache ： **开**（-DDCACHE_ON=1）+ **MPU 把 AXI SRAM 配成非缓存**（-DNONCACHE_MPU=1）—— 推荐做法")
} elseif ($DCache) {
  Write-Output ("D-Cache ： **开**（-DDCACHE_ON=1），**没有 MPU** —— 这是 H7 D-cache 干扰实验版")
} else {
  Write-Output ("D-Cache ： 关（默认；AHB-AP 读到的是真内存）")
}
Write-Output ("链接脚本： {0}" -f $ldpath)

# 把被采样变量的地址打印出来 —— 手填地址/排障时不用再开 nm。
# 本固件的硬要求：**所有 g_* 都必须是 0x24xxxxxx（AXI SRAM）**，DTCM(0x20000000) 探针读不到。
Write-Output ""
Write-Output "被采样变量（nm 实测地址/大小；必须全部落在 0x24xxxxxx = AXI SRAM）："
& $nm -S --size-sort $elf | Select-String -Pattern '\sg_' |
  ForEach-Object { "  " + $_.Line.Trim() }

# 编译成功后额外把 ELF 复制一份到本目录根 —— 这是本仓库给用户下载的约定
# （hpm6800evk_rtt_flood/fw.elf、stm32h743_rtt_speed/fw.elf 都是这么放的；
#   而 build*/ 被 .gitignore 忽略，所以只有根上这一份会进仓库）。
# 两个实验版**故意不覆盖**：默认版才是"发货的那一份"。
if (-not $DCache -and -not $NonCache){
  Copy-Item $elf (Join-Path $root 'fw.elf') -Force
  Write-Output ""
  Write-Output ("已复制给用户下载： {0}" -f (Join-Path $root 'fw.elf'))
} else {
  Write-Output ""
  Write-Output "（本变体产物留在构建目录里，不覆盖仓库根上那份默认 fw.elf）"
}
Write-Output ""
Write-Output ("产物： {0}" -f $elf)
Write-Output ("       {0} ({1} B)" -f $binout, (Get-Item $binout).Length)
