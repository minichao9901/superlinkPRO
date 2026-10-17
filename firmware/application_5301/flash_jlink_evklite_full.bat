@echo off
@REM ---------------------------------------------------------------------------
@REM First-flash for HPM5301EVKLite on a blank board: bootloader + APP.
@REM
@REM Connect the J-Link to the EVKLite J5 20-pin JTAG header (JTAG mode).
@REM   1. Builds the DFU bootloader (flash_xip, 0x80000000) and the APP
@REM      (flash_dfu, 0x80020000)
@REM   2. Programs both images in a single J-Link session
@REM   3. Resets and runs; the bootloader validates the APP and jumps to it.
@REM ---------------------------------------------------------------------------
setlocal

@if not defined HPM_SDK_ENV_DIR set "HPM_SDK_ENV_DIR=E:\sdk_env_v1.11.0"
@if not defined JLINK_EXE set "JLINK_EXE=C:\Program Files\SEGGER\JLink_V882\JLink.exe"
@if not exist "%JLINK_EXE%" set "JLINK_EXE=C:\Program Files\SEGGER\JLink\JLink.exe"
@if not exist "%JLINK_EXE%" set "JLINK_EXE=C:\Program Files (x86)\SEGGER\JLink\JLink.exe"
@if not exist "%JLINK_EXE%" (
    echo [ERROR] JLink.exe not found. Set JLINK_EXE to the full path and retry.
    exit /b 1
)

@set "APPPROJ=%~dp0"
@set "BOOTPROJ=%APPPROJ%..\bootloader_dfu\"
@set "BOOTHEX=%BOOTPROJ%build_xip_evklite\output\akaLinkPro_Boot_pack.hex"
@set "APPHEX=%APPPROJ%build_dfu_evklite\output\akaLinkPro_App_pack.hex"

@REM --- 1. Build bootloader ------------------------------------------------------
pushd "%BOOTPROJ%"
call build_xip_evklite.bat
if errorlevel 1 (
    echo [ERROR] Bootloader build failed.
    popd
    exit /b 1
)
popd
if not exist "%BOOTHEX%" (
    echo [ERROR] Output not found: %BOOTHEX%
    exit /b 1
)

@REM --- 2. Build app -------------------------------------------------------------
call "%APPPROJ%build_dfu_evklite.bat"
if errorlevel 1 (
    echo [ERROR] APP build failed.
    exit /b 1
)
if not exist "%APPHEX%" (
    echo [ERROR] Output not found: %APPHEX%
    exit /b 1
)

ping -n 2 127.0.0.1 >nul

@REM --- 3. Generate J-Link command script ----------------------------------------
@set "JLCMD=%TEMP%\evklite_full_flash.jlink"
> "%JLCMD%" echo device HPM5301xEGx
>>"%JLCMD%" echo si JTAG
>>"%JLCMD%" echo jtagconf -1 -1
>>"%JLCMD%" echo speed 4000
>>"%JLCMD%" echo connect
>>"%JLCMD%" echo Sleep 200
>>"%JLCMD%" echo loadfile "%BOOTHEX%"
>>"%JLCMD%" echo Sleep 200
>>"%JLCMD%" echo loadfile "%APPHEX%"
>>"%JLCMD%" echo Sleep 200
>>"%JLCMD%" echo r
>>"%JLCMD%" echo Sleep 300
>>"%JLCMD%" echo go
>>"%JLCMD%" echo Sleep 200
>>"%JLCMD%" echo Exit

@REM --- 4. Flash ----------------------------------------------------------------
echo.
echo Flashing bootloader + APP via J-Link (J5, JTAG)
"%JLINK_EXE%" -NoGui 1 -ExitOnError 1 -CommanderScript "%JLCMD%"
if errorlevel 1 (
    echo [ERROR] J-Link flash failed.
    exit /b 1
)

ping -n 3 127.0.0.1 >nul

echo.
echo [OK] Bootloader + APP flashed. Plug the USB0 OTG Type-C port: the board
echo      should enumerate as a CMSIS-DAP device (VID_0D28 PID_0204).
endlocal
