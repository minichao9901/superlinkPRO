@echo off
rem ============================================================
rem  kill-web.bat —— 一键清掉 web-serial-rtt-tools 的本地进程
rem
rem  这个工具会起两个驻留进程：
rem    · 静态服务：python -m http.server 8899
rem    · CDP 浏览器：chrome --remote-debugging-port=9333 --user-data-dir=...\edge-rtt-tools-test
rem
rem  为什么要连杀 3 轮：
rem    · Chrome 被强杀/崩溃后会留下**同 profile 的僵尸实例**，它们仍然占着探针的
rem      WebHID/WebUSB 接口 -> 页面一直报 Unable to claim interface；
rem    · 端口也可能被上一条命令残留的 python / chrome 占着，杀一轮不一定清净。
rem
rem  安全说明：只按 --user-data-dir 里的 profile 名匹配 Chrome，
rem            **不会碰日常用的 Chrome**（那个走默认 profile）。
rem
rem  用法：双击即可；命令行加 -q 跳过结尾的暂停。
rem
rem  本文件必须存成 ANSI/GBK(936)：存 UTF-8 时中文注释会被 cmd 按本地代码页
rem  读成乱码字节，足以把命令拆断（踩过：报 'ORTS' is not recognized）。
rem  .gitattributes 里已标 *.bat -text，别让 git 动它的换行。
rem ============================================================
chcp 936 >nul
setlocal
set "PROFILE=edge-rtt-tools-test"
set "PORTS=8899 9333"

echo.
echo ========== 清理 web-serial-rtt-tools 本地进程 ==========
echo.

for /l %%R in (1,1,3) do (
  echo --- 第 %%R 轮 ---

  rem ① 占着端口的监听进程（静态服务 / CDP）
  for %%P in (%PORTS%) do (
    for /f "tokens=5" %%A in ('netstat -aon ^| findstr ":%%P" ^| findstr "LISTENING"') do (
      if not "%%A"=="0" if not "%%A"=="4" (
        echo   [端口 %%P] 杀 PID %%A
        taskkill /F /PID %%A >nul 2>&1
      )
    )
  )

  rem ② 测试 profile 的 Chrome（含僵尸实例，它们才是占着探针接口的元凶）
  powershell -NoProfile -Command "$c=@(Get-CimInstance Win32_Process -Filter 'Name=''chrome.exe''' | Where-Object { $_.CommandLine -like '*%PROFILE%*' }); if ($c.Count) { $c | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('  [Chrome] 杀 ' + $c.Count + ' 个测试 profile 实例') }"

  rem ③ 卡死不放的本项目诊断脚本（node tmp\*.mjs 常因等 READY 死等）
  powershell -NoProfile -Command "$c=@(Get-CimInstance Win32_Process -Filter 'Name=''node.exe''' | Where-Object { $_.CommandLine -like '*web-serial-rtt-tools*' }); if ($c.Count) { $c | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Host ('  [node] 杀 ' + $c.Count + ' 个本项目脚本') }"

  powershell -NoProfile -Command "Start-Sleep -Milliseconds 600"
)

echo.
echo ---------- 复查 ----------
netstat -aon | findstr "LISTENING" | findstr ":8899 :9333"
if errorlevel 1 echo   8899 / 9333 端口已释放
powershell -NoProfile -Command "$c=@(Get-CimInstance Win32_Process -Filter 'Name=''chrome.exe''' | Where-Object { $_.CommandLine -like '*%PROFILE%*' }); Write-Host ('  残留测试 profile Chrome: ' + $c.Count + ' 个'); Write-Host ('  日常用的 Chrome: ' + @(Get-CimInstance Win32_Process -Filter 'Name=''chrome.exe''' | Where-Object { $_.CommandLine -notlike '*%PROFILE%*' }).Count + ' 个（没动）')"

echo.
echo 清理完毕。浏览器窗口若还留着，手动关掉即可。
echo 下次要用：make page-prep
echo.
if not "%~1"=="-q" pause