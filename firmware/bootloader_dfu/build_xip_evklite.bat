@echo off
@REM ---------------------------------------------------------------------------
@REM DFU bootloader build for HPM5301EVKLite (BOARD=hpm5301evklite).
@REM
@REM flash_xip layout, linked at 0x80000000, info block @0x8001F000.
@REM Output goes to ./build_xip_evklite so the akaLinkPro artifacts in
@REM ./build_xip are not touched.
@REM ---------------------------------------------------------------------------
@if not defined HPM_SDK_ENV_DIR set "HPM_SDK_ENV_DIR=E:\sdk_env_v1.11.0"

@set "HPM_SDK_BASE=%HPM_SDK_ENV_DIR%\hpm_sdk"
@set "GNURISCV_TOOLCHAIN_PATH=%HPM_SDK_ENV_DIR%\toolchains\rv32imac_zicsr_zifencei_multilib_b_ext-win"
@set "HPM_SDK_TOOLCHAIN_VARIANT=gcc"
@set "PYTHON_EXECUTABLE=%HPM_SDK_ENV_DIR%\tools\python3\python.exe"
@set "PATH=%HPM_SDK_ENV_DIR%\tools\python3;%HPM_SDK_ENV_DIR%\tools\cmake\bin;%HPM_SDK_ENV_DIR%\tools\ninja;%PATH%"
@set "BOARD=hpm5301evklite"

@cmake -GNinja -DBOARD=%BOARD% -DHPM_BUILD_TYPE=flash_xip -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_BUILD_TYPE=Release -B=./build_xip_evklite .
@cmake --build ./build_xip_evklite

@echo.
@echo [build_xip_evklite] Bootloader image (flash this): build_xip_evklite\output\akaLinkPro_Boot_pack.hex (linked at 0x80000000, info block @0x8001F000)
@echo [build_xip_evklite] raw (no info block): build_xip_evklite\output\akaLinkPro_Boot.hex / .bin
