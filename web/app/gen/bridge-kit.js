/**
 * 「工程生成」页 → **本地桥安装包**（`rtt-bridge-kit.zip`）。
 *
 * 干什么：把 `bridge/` 那套（桥本体 + 启动器 + 配置）变成**一次下载、双击即用**的东西 ——
 * 用户不需要仓库、不需要懂 Node、不需要知道 OpenOCD 装在哪：
 *
 *   rtt-bridge-kit/
 *     start-bridge.bat    双击这个：找 Node（没有就下便携版）→ 预检 → 起桥 → 打印网页地址
 *     check-tools.bat     只跑预检（`--doctor`）：端口 / OpenOCD / scripts / J-Link 逐个查
 *     get-node.ps1        start-bridge.bat 发现没 Node 时调它（下载 + sha256 校验 + 解压到 node/）
 *     start-bridge.sh     macOS / Linux 版（LF 换行）
 *     rtt-bridge.mjs      桥本体（**与仓库里那份逐字节一致**，见 bridge-src.js 的哈希对账）
 *     bridge.config.json  页面上填的参数（目标 / 工具路径 / J-Link 默认值）
 *     README-bridge.txt   三步上手 + 故障排查表 + 换机器改哪两行
 *
 * 三条设计纪律：
 *   1. **不写死路径**：`bridge.config.json` 里留空 = 桥自己去常见位置探测；填了就用你填的。
 *   2. **报错要自解释**：预检把"找过哪些路径、该改哪一行"打出来（`--doctor` 的实现见 bridge）。
 *   3. **纯函数**：不碰 DOM、不 fetch —— Node 里能直接跑（`bridge-kit.test.mjs` 就是拿它做对账）。
 */
import { toBytes } from './model.js';
import { BRIDGE_SRC, BRIDGE_SHA256 } from './bridge-src.js';

export const KIT_DIR = 'rtt-bridge-kit';

/** 默认值：与 bridge/bridge.config.json 保持一致的只有 jlink.device / speed（那是真机标定过的） */
export const KIT_DEFAULTS = {
  on: true,
  port: 17321,
  token: '',
  target: 'stm32f103',
  customCfgs: 'interface/cmsis-dap.cfg, target/stm32f1x.cfg',
  speed: 4000,
  openocd: '',            // 空 = 自动探测
  scripts: '',            // 空 = 跟着 openocd.exe 推断
  jlinkDevice: 'STM32F103C8',
  jlinkSpeed: 50000,      // 50 MHz：本机 J-Link PRO + F103 实测值（低了只会更慢）
  nodeVersion: '22.14.0',
  mirror: 1,              // 1 = 国内镜像优先（npmmirror），0 = 官方优先
  autoNode: true,         // 没装 Node 时自动下便携版
};

const TARGET_PRESETS = {
  stm32f103: { cfgs: ['interface/cmsis-dap.cfg', 'target/stm32f1x.cfg'], pre: ['cmsis-dap backend usb_bulk'], speed: 4000, jlinkDevice: 'STM32F103C8', note: 'STM32F103 中容量（CMSIS-DAP + SWD）' },
  stm32h7b0: { cfgs: ['interface/cmsis-dap.cfg', 'target/stm32h7x.cfg'], pre: ['cmsis-dap backend usb_bulk'], speed: 4000, jlinkDevice: 'STM32H7B0VB', note: 'STM32H7B0（H7A3/7B3 同一个 target cfg）' },
};

const splitList = s => String(s || '').split(/[,\s;]+/).map(x => x.trim()).filter(Boolean);

/** 页面参数 → `bridge.config.json` 的内容（对象形态，方便测试直接断言） */
export function kitConfig(p = {}, when = new Date()){
  const o = { ...KIT_DEFAULTS, ...p };
  const custom = splitList(o.customCfgs);
  const targets = {
    stm32f103: { ...TARGET_PRESETS.stm32f103, speed: Number(o.speed) || 4000 },
    stm32h7b0: { ...TARGET_PRESETS.stm32h7b0 },
  };
  if (o.target === 'custom' && custom.length){
    targets.custom = {
      cfgs: custom,
      pre: String(custom[0]).includes('cmsis-dap') ? ['cmsis-dap backend usb_bulk'] : [],
      speed: Number(o.speed) || 0,
      jlinkDevice: o.jlinkDevice,
      note: '网页上自定义的 cfg 列表',
    };
  }
  return {
    openocd: o.openocd || '',
    scripts: o.scripts || '',
    jlink: {
      exe: '', gdbserver: '', rttLogger: '',
      device: o.jlinkDevice || KIT_DEFAULTS.jlinkDevice,
      speed: Number(o.jlinkSpeed) || KIT_DEFAULTS.jlinkSpeed,
      rttPort: 19021, gdbPort: 2331,
      note: 'exe/gdbserver/rttLogger 留空 = 自动找（PATH → C:/Program Files/SEGGER/JLink_*/ 里版本号最大的那份）。'
          + 'device 是给 J-Link 的器件名；speed 单位 kHz（50000 = 50MHz，本机 J-Link PRO + F103 实测可用）。',
    },
    targets,
    _生成信息: {
      生成时间: when.toISOString().replace(/\.\d+Z$/, 'Z'),
      桥本体_sha256: BRIDGE_SHA256,
      说明: '改 openocd / scripts / jlink.* 就能适配别的机器；留空 = 自动探测。改完跑 check-tools.bat 复核。',
    },
  };
}

/** bat 里的可调开关（生成时写死成字面量，用户也能自己改） */
const batKnobs = o => [
  `set "NODE_VER=${o.nodeVersion}"        rem 便携 Node 版本（只在自动下载时用）`,
  `set "MIRROR=${o.mirror ? 1 : 0}"                 rem 1 = 国内镜像优先（npmmirror），0 = 官方优先`,
  `set "AUTO_NODE=${o.autoNode ? 1 : 0}"              rem 1 = 本机没 Node 时自动下便携版到 node\\`,
  `set "PORT=${Number(o.port) || KIT_DEFAULTS.port}"`,
  `set "TARGET=${o.target === 'custom' ? 'custom' : (o.target || 'stm32f103')}"`,
].join('\r\n');

function batScript(o, when){
  return `@echo off
rem ============================================================
rem  rtt-bridge 本地桥（J-Link / OpenOCD）—— 双击即用
rem  由网页「工程生成」生成 · ${when.toISOString().slice(0, 10)}
rem  参数原样透传，例：start-bridge.bat --port 17322 --attach
rem ============================================================
setlocal EnableExtensions
title rtt-bridge 本地桥
cd /d "%~dp0"

rem ---------- 可调开关（改完存盘即可，不用重下）----------
set "NODE_DIR=%~dp0node"
${batKnobs(o)}
set "BRIDGE=%~dp0rtt-bridge.mjs"

echo.
echo [rtt-bridge] 本地桥启动器
echo   目录 : %~dp0
echo   目标 : %TARGET%   端口 : %PORT%
echo.

rem ---------- 1) 找 Node：便携版 → 系统 PATH ----------
set "NODE_EXE="
if exist "%NODE_DIR%\\node.exe" set "NODE_EXE=%NODE_DIR%\\node.exe"
if not defined NODE_EXE for %%I in (node.exe) do if not "%%~$PATH:I"=="" set "NODE_EXE=%%~$PATH:I"
if not defined NODE_EXE (
  echo [!] 本机没找到 Node.js（桥要 Node 18+）
  if "%AUTO_NODE%"=="1" (
    echo [i] 自动下载便携版到 "%NODE_DIR%"（约 30 MB，只下这一次）
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-node.ps1" -Version %NODE_VER% -Mirror %MIRROR% -Dest "%NODE_DIR%"
    if exist "%NODE_DIR%\\node.exe" ( set "NODE_EXE=%NODE_DIR%\\node.exe" ) else ( goto :fail )
  ) else (
    echo     1^) 装一个 Node 18+： https://nodejs.org/
    echo     2^) 或把便携版解压到 "%NODE_DIR%"（目录里有 node.exe 就行）
    goto :fail
  )
)
echo [i] Node 版本：
"%NODE_EXE%" -v
set "PATH=%NODE_DIR%;%PATH%"

rem ---------- 2) 预检（端口 / OpenOCD / scripts / J-Link）----------
echo.
"%NODE_EXE%" "%BRIDGE%" --doctor --port %PORT% --target %TARGET%
if errorlevel 1 (
  echo.
  echo [!] 预检里有带 X 的项 —— 照着上面的提示改 bridge.config.json（或下面这行的参数），改完再双击本文件。
  echo     只想看预检：双击 check-tools.bat
  echo     仍然继续启动（预检只是提示，桥自己还会再报一次错）...
  echo.
)

rem ---------- 3) 起桥（参数：命令行给的 > 上面的开关）----------
set "ARGS=--port %PORT% --target %TARGET%"
if not "%~1"=="" set "ARGS=%*"
echo [i] 启动： rtt-bridge.mjs %ARGS%
echo     网页从 http://127.0.0.1:%PORT%/ 打开（同源，最省事）；
echo     也可以直接用线上页面 https://minichao9901.github.io/web-serial-rtt-tools/
echo     ### 退出请按 Ctrl+C（会把 OpenOCD / J-Link 子进程一起收掉）###
echo.
"%NODE_EXE%" "%BRIDGE%" %ARGS%
echo.
echo [桥已退出] 按任意键关闭窗口
pause >nul
exit /b 0

:fail
echo.
echo [x] 起不来：先解决上面 [!] 那几条，再双击本文件。
pause >nul
exit /b 1
`.replace(/\n/g, '\r\n');
}

function checkBat(o){
  return `@echo off
rem 只跑环境预检（不启服务、不碰探针）：端口 / OpenOCD / scripts / J-Link 逐个查
setlocal EnableExtensions
title rtt-bridge 预检
cd /d "%~dp0"
set "NODE_DIR=%~dp0node"
set "NODE_EXE="
if exist "%NODE_DIR%\\node.exe" set "NODE_EXE=%NODE_DIR%\\node.exe"
if not defined NODE_EXE for %%I in (node.exe) do if not "%%~$PATH:I"=="" set "NODE_EXE=%%~$PATH:I"
if not defined NODE_EXE (
  echo [!] 没找到 Node.js（便携版也不在 "%NODE_DIR%"）—— 先双击 start-bridge.bat，它会自动装
  pause
  exit /b 1
)
"%NODE_EXE%" "%~dp0rtt-bridge.mjs" --doctor --port ${Number(o.port) || KIT_DEFAULTS.port} --target ${o.target === 'custom' ? 'custom' : (o.target || 'stm32f103')} %*
echo.
pause
`.replace(/\n/g, '\r\n');
}

/** 便携 Node：下载 → sha256 校验（拿同一目录的 SHASUMS256.txt）→ 解压到 node\ */
function psScript(o){
  return `<#
  便携 Node 下载器（只在 start-bridge.bat 发现本机没有 Node 时被调用）
  · 绿色：只往 -Dest 里放，不动系统 PATH、不写注册表
  · 校验：从同一个分发目录取 SHASUMS256.txt 验 sha256（拿不到就跳过并说明）
  用法： powershell -ExecutionPolicy Bypass -File get-node.ps1 -Version ${o.nodeVersion} -Mirror 1 -Dest .\\node
#>
param(
  [string]$Version = '${o.nodeVersion}',
  [int]$Mirror = ${o.mirror ? 1 : 0},
  [string]$Dest = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not $Dest) { $Dest = Join-Path $PSScriptRoot 'node' }

$arch = if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
$name = "node-v$Version-win-$arch"
if ($Mirror -eq 1) {
  $bases = @("https://registry.npmmirror.com/-/binary/node/v$Version", "https://nodejs.org/dist/v$Version")
} else {
  $bases = @("https://nodejs.org/dist/v$Version", "https://registry.npmmirror.com/-/binary/node/v$Version")
}
$zip = Join-Path $env:TEMP "$($name).zip"

$ok = $false
foreach ($b in $bases) {
  try {
    Write-Host "  -> $($b)/$($name).zip"
    Invoke-WebRequest -Uri "$($b)/$($name).zip" -OutFile $zip -UseBasicParsing -TimeoutSec 900
    try {
      $sums = (Invoke-WebRequest -Uri "$($b)/SHASUMS256.txt" -UseBasicParsing -TimeoutSec 60).Content
      $line = ($sums -split "\`n" | Where-Object { $_ -match [regex]::Escape("$($name).zip") } | Select-Object -First 1)
      $want = ($line -split '\\s+')[0]
      if ($want) {
        $want = $want.Trim().ToLower()
        $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($want -ne $got) { Write-Host "  [!] sha256 不一致（期望 $want / 实际 $got）—— 换下一个源"; Remove-Item $zip -Force; continue }
        Write-Host "  [i] sha256 校验通过"
      }
    } catch { Write-Host "  [!] 没取到 SHASUMS256.txt，跳过校验：$($_.Exception.Message)" }
    $ok = $true
    break
  } catch {
    Write-Host "  [!] 下载失败：$($_.Exception.Message)"
  }
}
if (-not $ok) {
  Write-Host ''
  Write-Host '[x] 便携版没下成。两条路：'
  Write-Host '    1) 手动装 Node 18+ ： https://nodejs.org/  （装完重开这个窗口）'
  Write-Host "    2) 手动下载 zip： https://nodejs.org/dist/v$Version/$($name).zip"
  Write-Host "       解压后把里面的 node.exe 等文件放进： $Dest"
  exit 1
}

$tmp = Join-Path $env:TEMP "node-portable-$([guid]::NewGuid().ToString('N'))"
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Join-Path $tmp $name
if (-not (Test-Path (Join-Path $src 'node.exe'))) {
  Write-Host "[x] 解压后没找到 node.exe（压缩包结构变了？）：$src"
  exit 1
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
Copy-Item (Join-Path $src '*') $Dest -Recurse -Force
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Write-Host "[i] 便携 Node 就绪： $Dest\\node.exe"
exit 0
`.replace(/\n/g, '\r\n');
}

function shScript(o){
  return `#!/bin/sh
# rtt-bridge 本地桥（macOS / Linux）—— 由网页「工程生成」生成
# 用法： ./start-bridge.sh [参数...]      例： ./start-bridge.sh --port 17322
cd "$(dirname "$0")" || exit 1
if command -v node >/dev/null 2>&1; then
  NODE_BIN=node
elif [ -x "./node/bin/node" ]; then
  NODE_BIN="./node/bin/node"
else
  cat <<'EOF'
[!] 没找到 Node.js（桥需要 Node 18+）。三种装法：
    · macOS ： brew install node
    · Ubuntu： sudo apt install nodejs
    · 免安装： curl -fsSL https://nodejs.org/dist/v${o.nodeVersion}/node-v${o.nodeVersion}-linux-x64.tar.xz | tar -xJ
              然后 mv node-v${o.nodeVersion}-linux-x64 node    （放在本目录即可，脚本会自动用 ./node/bin/node）
EOF
  exit 1
fi
echo "[i] Node $("$NODE_BIN" -v)"
"$NODE_BIN" ./rtt-bridge.mjs --doctor --port ${Number(o.port) || KIT_DEFAULTS.port} --target ${o.target === 'custom' ? 'custom' : (o.target || 'stm32f103')} || true
exec "$NODE_BIN" ./rtt-bridge.mjs "$@"
`.replace(/\r\n/g, '\n');
}

function readme(o, when = new Date()){
  const cfg = kitConfig(o, when);
  const port = Number(o.port) || KIT_DEFAULTS.port;
  return `rtt-bridge 本地桥 · 三步上手
================================================
生成时间：${cfg._生成信息.生成时间}
桥本体 sha256：${BRIDGE_SHA256}
（改了 bridge/rtt-bridge.mjs 就要用 tools/dev/embed-bridge.mjs 重新嵌一次；make test-gen 会对账）

为什么需要它
------------------------------------------------
J-Link 与 OpenOCD 都是**本机程序**，浏览器无权启动进程、也无权开 TCP —— 所以用 J-Link /
OpenOCD 读 RTT 时，需要一个本机小程序当"桥"。零安装的 WebUSB 通路（CMSIS-DAP 探针）**不需要**它。

三步
------------------------------------------------
1. 双击 start-bridge.bat（macOS/Linux： ./start-bridge.sh）
   · 没有 Node？它会问你要不要自动下便携版（约 30 MB，放到本目录的 node\\，绿色、不动系统）
   · 起来之前会跑一次**预检**：端口、OpenOCD、scripts、J-Link 逐个查，并把"该改哪一行"写出来
2. 浏览器打开它打印的地址（http://127.0.0.1:${port}/），
   或者直接用线上页面 https://minichao9901.github.io/web-serial-rtt-tools/（它在白名单里）
3. 页面里：RTT Viewer → 后端选「本地桥 · OpenOCD」或「本地桥 · J-Link」→ 连接

退出：在那个黑窗口里按 Ctrl+C（会把 OpenOCD / J-Link 子进程一起收掉，不留孤儿占着探针）。

只想看环境对不对：双击 check-tools.bat（等价于 node rtt-bridge.mjs --doctor）

换机器 / 路径不对
------------------------------------------------
bridge.config.json 里这三处，**留空 = 自动探测**，填了就按你填的来：

  "openocd": "${cfg.openocd}"        // openocd.exe 的绝对路径
  "scripts": "${cfg.scripts}"        // OpenOCD 的 scripts 目录（里面有 interface/ target/）
  "jlink": { "exe": "", "gdbserver": "", "rttLogger": "", "device": "${cfg.jlink.device}", "speed": ${cfg.jlink.speed} }

自动探测顺序：
  OpenOCD  : %USERPROFILE%\\.espressif\\tools\\openocd-esp32  →  C:\\Program Files\\OpenOCD  →  PATH
  J-Link   : PATH  →  C:\\Program Files\\SEGGER\\JLink_*（版本号最大的那份）
找不到时，预检会把"找过哪些路径"打出来 —— 按提示填绝对路径即可（注意路径里用正斜杠或双反斜杠）。

常见问题
------------------------------------------------
· 端口被占（"被占用 —— 换一个"）：可能已经开着一个桥。关掉它，或 start-bridge.bat --port 17322
· 页面连不上桥：确认桥窗口还开着；页面若是别处打开的（自建站），要加 --allow-origin https://你的站点
· 想让别人连不上你的桥：start-bridge.bat --token 你的口令，页面地址里加 ?token=你的口令
· 目标芯片不在列表：网页上选「自定义 cfg…」填 cfg（相对 scripts 目录的路径），或改
  bridge.config.json 的 targets 段后 --target 你的名字
· 防火墙弹窗：桥只监听 127.0.0.1（本机回环），点"取消/阻止"也能用

参数（原样透传给 rtt-bridge.mjs，见 node rtt-bridge.mjs --help）
------------------------------------------------
  --port 17321          监听端口        --target 名字      目标配置名
  --attach              复用已在跑的 OpenOCD
  --openocd <exe>       OpenOCD 路径    --scripts <dir>    scripts 目录
  --jlink-attach        连已有的 RTT telnet   --jlink-logger <exe>  高速只读模式
  --token <串>          WebSocket 口令     --allow-origin <o>     额外放行的网页来源
  --doctor              只做环境预检
`;
}

/**
 * 生成桥包的全部文件。
 * @param {object} p 页面参数（见 KIT_DEFAULTS）
 * @param {Date} [when] 时间戳（可注入 → 同一个参数产出的字节完全可复现，自测靠它做确定性对账）
 * @returns {{name:string,data:Uint8Array,text:string,bin:false}[]} name 里带 `rtt-bridge-kit/` 这一层
 */
export function bridgeKitFiles(p = {}, when = new Date()){
  const o = { ...KIT_DEFAULTS, ...p };
  if (!o.on) return [];
  const text = (rel, body, nl = 'crlf') => ({ name: `${KIT_DIR}/${rel}`, text: body, data: toBytes(body, nl), bin: false });
  return [
    text('rtt-bridge.mjs', BRIDGE_SRC, 'lf'),                       // 桥本体：与仓库逐字节一致（LF）
    text('bridge.config.json', JSON.stringify(kitConfig(o, when), null, 2) + '\n'),
    text('README-bridge.txt', readme(o, when), 'lf'),
    text('start-bridge.bat', batScript(o, when), 'crlf'),           // .bat 必须 CRLF
    text('check-tools.bat', checkBat(o), 'crlf'),
    text('get-node.ps1', psScript(o), 'crlf'),
    text('start-bridge.sh', shScript(o), 'lf'),                     // .sh 必须 LF
  ];
}

/** 只算桥包的总字节（状态栏显示用） */
export const kitBytes = files => files.reduce((n, f) => n + f.data.length, 0);
