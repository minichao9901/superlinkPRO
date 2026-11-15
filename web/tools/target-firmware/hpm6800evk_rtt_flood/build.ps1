# Build the HPM6800EVK RTT flood firmware (HPM6880, RISC-V) with the HPM SDK env.
#
#   powershell -File script_test\hpm6800evk_rtt_flood\build.ps1 [-BuildType flash_xip]
#
# Output: build\<build_type>\output\demo.elf, or build-seq with -Sequence.
param([string]$BuildType = 'flash_xip', [switch]$Sequence)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sdkEnv = if ($env:HPM_SDK_ENV_DIR) { $env:HPM_SDK_ENV_DIR } else { 'E:\sdk_env_v1.11.0' }

$env:PATH = "$sdkEnv\tools\python3;$sdkEnv\tools\cmake\bin;$sdkEnv\tools\ninja;$env:PATH"
$env:HPM_SDK_BASE = "$sdkEnv\hpm_sdk"
$env:GNURISCV_TOOLCHAIN_PATH = "$sdkEnv\toolchains\rv32imac_zicsr_zifencei_multilib_b_ext-win"
$env:HPM_SDK_TOOLCHAIN_VARIANT = 'gcc'

$broot = if ($Sequence) { 'build-seq' } else { 'build' }
$sequenceSetting = if ($Sequence) { 'ON' } else { 'OFF' }
$bdir = Join-Path $here "$broot\$BuildType"
Write-Output "building $buildType -> $bdir"
# ⚠️ 参数必须**加引号**：PowerShell 7 把以 `-` 开头的裸 token 当参数名，里面的 $buildType 不做变量展开
#    （原样传给 cmake，SDK 会报 invalid HPM_BUILD_TYPE: $buildtype）。Windows PowerShell 5.1 会展开，
#    所以这个坑只在 pwsh 下出现 —— scope 那份早就这么修了，flood 这份一直没修，等于编不出来。
& cmake -G Ninja "-DBOARD=hpm6800evk" "-DHPM_BUILD_TYPE=$BuildType" "-DCMAKE_BUILD_TYPE=debug" "-DSLPRO_RTT_SEQUENCE=$sequenceSetting" -B $bdir -S $here
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& cmake --build $bdir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$elf = Join-Path $bdir 'output\demo.elf'      # SDK 统一把可执行文件叫 demo.elf
if (Test-Path $elf) {
    $nm = "$sdkEnv\toolchains\rv32imac_zicsr_zifencei_multilib_b_ext-win\bin\riscv32-unknown-elf-nm.exe"
    Write-Output ""
    Write-Output "RTT control block:"
    & $nm -S $elf | Select-String '_SEGGER_RTT'
    # 📦 复制到目录根：仓库里"给用户直接下载"的那份就是它（与其它靶子同约定）
    $pub = Join-Path $here $(if ($Sequence) { 'fw-seq.elf' } else { 'fw.elf' })
    Copy-Item -Force $elf $pub
    Write-Output ("入库： {0} ({1} KB)" -f $pub, [int]((Get-Item $pub).Length / 1024))
}
