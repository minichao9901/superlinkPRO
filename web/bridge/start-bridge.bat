@echo off
rem ===========================================================================
rem  UTF-8 bootstrap.  EVERYTHING ABOVE THE GUARD MUST BE ASCII-ONLY.
rem
rem  This file is saved as UTF-8, but a Chinese Windows console defaults to
rem  codepage 936(GBK) -> the Chinese text below shows up as mojibake.  Fix:
rem  switch the console to 65001 and RE-ENTER this script, so the child parses
rem  the file as UTF-8 from its first byte.  (A bare `chcp 65001` line is not
rem  enough: cmd reads batch files in blocks, and switching codepage mid-block
rem  can split a multi-byte character, executing the leftover as a command.)
rem  The env var marks "already re-entered"; %* is forwarded untouched.
rem ===========================================================================
if not "%RTT_TOOLS_UTF8%"=="1" (
  chcp 65001 >nul
  set "RTT_TOOLS_UTF8=1"
  cmd /c ""%~f0" %*"
  exit /b %errorlevel%
)
rem 双击即启动本地桥（零 npm 依赖，只要有 Node 18+）
rem 参数会原样传给 rtt-bridge.mjs，例如： start-bridge.bat --target stm32f103
rem （上面那段英文是"UTF-8 引导"：文件用 UTF-8 存、控制台默认 936，所以先切 65001
rem   再重新进入自己一次；引导段必须全英文，中文从这一行往后才安全。）
setlocal
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo [!] 找不到 node.exe —— 请先安装 Node.js 18 或更高版本： https://nodejs.org/
  pause
  exit /b 1
)
if "%~1"=="" (
  node "%~dp0rtt-bridge.mjs" --target stm32f103
) else (
  node "%~dp0rtt-bridge.mjs" %*
)
echo.
echo [桥已退出] 按任意键关闭窗口
pause >nul
