# 后端与实测数据

本文记录三种 RTT 后端**在本机真硬件上量出来的数字和坑**，选后端/排障时看这份。

测试平台：Windows + MicroLink CMSIS-DAP（VID:PID `0D28:0202`，接口0 = CMSIS-DAP v2，bulk EP1 IN / EP2 OUT，512 B/包）
目标：STM32F103（Cortex-M3 r1p1，SWD），跑 `tools/target-firmware/stm32f103` 那份测试固件。

## 一、速度对比（读目标内存，同一个探针同一条 SWD）

| 路径 | 单命令往返 | 连续读吞吐 | 备注 |
|---|---|---|---|
| **WebUSB · CMSIS-DAP** | **0.34 ms** | **127 KB/s**（2 KB 块 16.1 ms；512 B 块 4.6 ms） | Chrome/Edge 直接驱动探针 |
| **本地桥 · OpenOCD（Tcl RPC 6666）** | ~2 ms | **17 KB/s**（4 KB 读 237 ms；16 KB 读 1.3 s；64 KB 读 9.5 s） | 文本十六进制字返回，越大越不划算 |
| 本地桥 · OpenOCD（telnet + `dump_image`） | — | 与上面同量级 | 二进制落文件，省掉文本转换但没量到明显收益 |

**结论**：
- 大流量 RTT（>20 KB/s）用 **WebUSB**；OpenOCD 后端受限于 RPC 的文本通道，读数只有 ~17 KB/s。
- 这也解释了实测现象：目标一次往 4 KB 缓冲灌 8 KB 时，OpenOCD 后端只读到 ~4 KB（其余被固件按 SKIP 丢掉），
  而 WebUSB 后端在同一场景下缓冲水位只打到 ~30%。
- RPC 读**分块要小**：4 KB/次最快，16 KB 开始掉速、64 KB 只剩 6.7 KB/s（所以桥里固定 256 字 = 1 KB/次）。

## 二、OpenOCD 后端（桥）怎么驱动的

ESP-IDF 自带那份 OpenOCD（`~\.espressif\tools\openocd-esp32\v0.12.0-esp32-*`）够用，但要注意：

- **CMSIS-DAP v2 探针必须显式指定后端**：`-c "cmsis-dap backend usb_bulk"`，否则认不到（默认走 TCP 后端）。
- 桥走 **Tcl RPC（6666）** 而不是 telnet（4444）：`read_memory <addr> 32 <n>` 返回**文本十六进制字**
  （`0x47474553 0x52205245 …`），一条命令拿一整块，不用临时文件；
  telnet 那条路要用 `dump_image` 落文件再读回来，代码更绕。
- 目标控制：`halt` / `resume` / `reset run` 直接发。
- 探针的 `rtt server` 在 ESP-IDF 这份 fork 里是残缺的（只有 `rtt server`，没有 `rtt setup/start`），
  所以**RTT 协议是浏览器这边自己实现的**，OpenOCD 只当"读写内存 + 控制目标"的搬运工。

目标配置写在 `bridge/bridge.config.json`：`cfgs` 顺序加载，`pre` 里的命令插在第一个 cfg 之后、
其余 cfg 之前（`cmsis-dap backend usb_bulk` 必须赶在 `target/*.cfg` 里的 `transport select` 之前）。

## 三、J-Link 后端（真机实测：SEGGER J-Link PRO + STM32F103RB，SWD 50MHz）

目标跑用户固件（死循环狂发 `b`，RTT 控制块在 `0x20000000` 一带）。

### 3.1 各条取数路径的实测吞吐（同一块板、同一速率，本轮复测）

| 取数路径 | 吞吐 | 说明 |
|---|---|---|
| `JLinkRTTLogger.exe` 直写文件（官方 CLI 基线） | **1463.4 KB/s** | 与用户 `make do_log` 的 1465.7 KB/s 一致 |
| **经桥到网页（logger 源，页面勾「高速只读」）** | **1463.0 KB/s** | 转发后与官方 CLI 持平，代价是**只读** |
| `JLinkGDBServerCL` 的 RTT telnet（裸读，不过桥） | **565.4 KB/s** | 8192 B/帧、约 71 帧/秒 ≈ 每 14ms 才轮询一次 RTT |
| 同上 + 每 20ms 塞一个 GDB 请求 | 578.9 KB/s | 想"催"它多轮询基本无效 |
| **经桥到网页（GDBServer 源，默认全双工）** | **567.1 KB/s** | = 裸 telnet 的 100.3%，转发层已不构成瓶颈 |
| 不连 GDB 客户端直接读 telnet | 1.0 KB/s | 目标被 server 按在 halt 上，只吐 8 KB 缓冲存量 |

**结论：网页这条 J-Link 通路的天花板在数据源，不在转发层。**
`JLinkGDBServerCL` 的 RTT telnet 自带 ~14ms 的轮询节奏（它的二进制里也没有轮询间隔开关），
想上 MB/s 只能换源 → 用 `JLinkRTTLogger`。

### 3.2 桥里的两种 J-Link 模式

- **全双工（默认）**：桥自启 `JLinkGDBServerCL -RTTTelnetPort 19021` 再连它的 telnet，ch0 收发都行，
  上限 ~565 KB/s。server 起来后目标默认 **halt**，桥会连 GDB 端口补一句 `c` 让它跑
  （不补就只出 4KB 缓冲存量，现象像"RTT 坏了"）。
- **高速只读（页面里勾选）**：桥拉
  `JLinkRTTLogger.exe -Device X -If SWD -Speed N [-RTTAddress 0x…] -RTTChannel n <临时文件>`，
  每 25ms tail 一次转发，实测 ~1463 KB/s。**不能向下发**（`stream.write` 会报错）；
  日志落在 `%TEMP%`，桥停会话时删除（1.4MB/s 下一小时约 5GB，别指望它长期存档）。

### 3.3 转发层（这次改的就是它）

- RTT 流走 **WebSocket 二进制帧**（opcode 0x2），不再 base64 + JSON：老写法体积 +33%，
  且每包一次 `JSON.stringify` + base64 编码 + 一次 write 系统调用。
- 桥端**攒批**：`≥32KB` 或每 `20ms`（先到先发）才发一帧；实测帧均值从 ~4KB 涨到 ~19.7KB。
- 页面 `app/rtt/bridge.js` 设 `binaryType='arraybuffer'`，二进制优先，
  同时保留老的 `{t:'stream.data', data:base64}` 分支兼容旧桥。

### 3.4 其它实测坑（细节都写在 `bridge/rtt-bridge.mjs` 注释里）

- **绝对不要给 server 加 `-singlerun`**：GDB 端口连上再断开会被当成"会话结束"，server 当场退出。
- RTT telnet 口会先吐 server 自己的横幅（`SEGGER J-Link V8.82 …`），必须逐行滤掉。
- J-Link 同一时刻只允许一个持有者 —— 烧录前桥会先停掉 RTT 会话。
- 官方 CLI 可用参数（`JLinkRTTLogger.exe -?`）：`-Device -If -Speed -USB -IP -RTTAddress -RTTSearchRanges -RTTChannel -JLinkScriptFile <OutFilename>`。
- J-Link **没有 WebUSB 通路**（协议不开放），想用 J-Link 就必须起桥。

### 3.4.1 退出清理：Ctrl+C 必须把子进程一起收掉（2026-09-30 补）

`Windows 上父进程退出不会带走子进程` —— 桥一退，`openocd.exe` / `JLinkGDBServerCL.exe` 会变成
**孤儿继续占着探针**，之后网页再连就是"被占 / 连不上"（README 里那条"首连失败十有八九是探针被占"，
一大半根因就在这）。老代码只有"网页断开 WebSocket"那条路会 `backend.stop()`，Ctrl+C 走默认退出、什么都不做。

现在：`SIGINT / SIGTERM / SIGHUP` 三个信号都挂上 → `cleanupAndExit()`（`stop()` 后端 → 关闭 WS（1001）
告诉网页 → 250 ms 让 taskkill 落地 → 退出），`process.on('exit')` 再兜一次底；两个后端的 `stop()`
统一用 `taskkill /PID x /T /F`（连子孙一起收；非 Windows 上退回 `child.kill()`），J-Link 还会删掉
logger 的 `%TEMP%` 日志。横幅里也写明了这一点。

自测：`node tools/selftest/bridge-lifecycle.test.mjs`（离线，进 `make test`）—— 15 项：
`cleanupAndExit()` 只清一次 + 真的调 `exit(0)`、两个后端的 `stop()` **用真子进程**验"确实退出"、
logger 临时文件被删、以及"直接跑桥时横幅确实讲了退出行为"（证明主路径挂上了钩子）。
⚠️ Windows 上没法给子进程发真 Ctrl+C（SIGINT 只能由控制台产生），所以是"单测处理函数 + 真收真子进程"
两条合起来覆盖，这点在测试文件里注明了。
⚠️ 反过来也记一笔：**用 X 关黑窗口 / `taskkill /F` 强杀控制台，桥自身可能来不及清理** ——
实测 `taskkill /T /F` 打 `cmd.exe` 时它先干掉父进程、再枚举子进程就找不到 node 了（node 会活下来）。
所以横幅和 README 都写着"退出请按 Ctrl+C"。

### 3.4.2 环境预检 `--doctor` 与「桥包」（2026-09-30）

`node bridge/rtt-bridge.mjs --doctor`（只读：不占端口、不起服务、不碰探针）把这几项逐个查一遍并把
"找过哪些路径 / 该改哪一行"打出来：Node 版本、静态根有没有网页副本、监听端口是否空闲、
OpenOCD + scripts 目录（顺带核对 `interface/cmsis-dap.cfg` / `target/stm32f1x.cfg`）、当前目标的 cfgs、
J-Link 三件套（`JLinkGDBServerCL.exe` / `JLink.exe` / `JLinkRTTLogger.exe`）。
退出码 0 = 全就绪；>0 = 有 ❌ 项。

「工程生成」页最下面「本地桥」那一栏能**一键生成整套桥包**（`app/gen/bridge-kit.js` + 嵌进去的桥源码
`app/gen/bridge-src.js`）：7 个文件，解压双击 `start-bridge.bat` 即用，**不依赖仓库**、**路径留空自动探测**、
**没 Node 自动下便携版**（npmmirror 优先 + `SHASUMS256.txt` 校验）。设计与口径见 README 的
「工程生成 → 顺带生成本地桥安装包」。自测：`make test-gen`（`bridge-kit.test.mjs` 50 项，含"嵌进去的桥
与仓库那份逐字节一致"的哈希对账）+ `make test-gen-page`（CDP：13 个文件、改参数立刻生效、ZIP 里带那 7 个）。

### 3.5 烧录（JLink.exe Commander）

`flash(backend=jlink)` 用官方 `JLink.exe`：`erase → loadfile → verify → r`，烧完不自动重启 RTT。
验收口径：经桥烧 `tools/target-firmware/stm32f103/build/fw.elf`（153624 B 的 ELF）→
`tmp/jlink-accept.mjs check` 读回与 `objcopy` 出的镜像**逐字节比对**（本次 4676 B 全等 ✅）。

### 3.6 真页面实测（CDP 驱动，`tmp/page-rtt-jlink-probe.mjs` / `tmp/page-rtt-diag.mjs`）

前提：`python -m http.server 8899` 供页面 + `bridge/rtt-bridge.mjs --port 17321` + 带 CDP(9333) 的浏览器。

| 页面里选的模式 | 10 秒收到 | 速率 | 样本 |
|---|---|---|---|
| 勾「高速只读」（JLinkRTTLogger 源） | 13.5 MB（另一次 15.2 MB） | **1318~1485 KB/s** | `bbbb…` ✅ |
| 不勾（GDB server 全双工流） | 4.60 MB | **448 KB/s** | `bbbb…` ✅ |

**量页面有没有收到数据，只有 `window.__tools.rtt.stats.bytes` 算数**（配 `term.buffer.active.length` 看显示）。
三个看着像、其实不成立的指标（第一版验收脚本就栽在这上面，误判成"页面一个字节都没收到"）：

- ✗ `#r-term` 的 `textContent.length`：**空 xterm 就有 5 万多字符**（一堆 `&nbsp;` 单元格），
  而且 DOM 渲染器复用固定的行节点、内容原地覆盖 → 长度几乎不涨。
- ✗ `__tools.rtt.rxBytes` / `totalBytes`：**这两个属性不存在** → 恒为 0。
- ✗ `__tools.rtt.running`：它只表示"**内存轮询循环**在跑"（WebUSB/OpenOCD 那条路）。
  J-Link 流模式是桥**推**数据，本来就没有轮询循环 → `running:false` 是正常的，跟收没收到数据无关。

另外两点页面行为，容易再次误判：

- **速率 >100 KB/s 时页面会自动停渲染**（`_highspeedGate`，`app/rtt/view.js`，WebUSB 时代就有的省 CPU 设计）：
  于是终端"冻住"，但字节照收、统计照涨、记录到文件照写。现在停渲染的那一刻会在终端里写一行说明
  （否则盯终端的人只会看到画面凭空不动）。
- 页面默认流模式（GDB server 源）稳定在 ~450 KB/s，比同模式下 Node 客户端测到的 ~565 KB/s 低 ~20%
  （浏览器侧接收/调度开销，未深挖）；换 logger 源后页面反而能跑到 1.3~1.5 MB/s。

## 四、WebUSB 后端的坑（都是真机踩出来的）

按重要性排序，全部写在 `app/rtt/dap-webusb.js` 的注释里：

1. **SWD 激活序列必须主机自己发**：`DAP_Connect` 只做引脚初始化。少发 88 位激活序列
   （`9E E7` + 64 个 1 + 8 个 0）时，SWJ-DP 还停在 JTAG 模式，之后所有传输一律 `NO ACK(0x07)`。
   **必须一次发完 88 位**：拆成 16/64/8 三次、或把末尾空闲写成 `0xFF`，都会让这个探针的 SWJ 引擎
   进入"传输全 NO ACK"的状态（本机实测，最后是靠工作区里验证过的裸客户端 `tools\la\cmsis_dap_raw.py` 定的写法）。
   顺序也要照它：`Connect → SWJ_Clock → SWD_Configure → SWJ_Sequence(88) → TransferConfigure`。
2. **线复位之后的第一个 SWD 包必须是「读 DP IDCODE」**（ARM SWD 协议的激活步骤）。
   少了这一笔，后面任何访问（哪怕是写 DP SELECT）都返回 `NO ACK(0x07)` —— 而包头完全正确、
   探针也照常回响应，极具误导性。定位手段：`tools\selftest\debug-sweep.mjs`（在页面上做参数扫描）。
3. **激活序列之后不要再设时钟**：实测那样也会把链路弄成一路 NO ACK。
4. **清掉上一场会话残留在 IN 端点里的响应**（`resync()`）：响应的回显不唯一，
   残留的 `DAP_Transfer` 响应回显同样是 `0x05`，光靠"按回显匹配"分不出来，
   会被当成自己这条命令的响应 → 初始化看着正常、下行命令却乱套。
   做法是连发 N 条 `DAP_Disconnect`（回显 `0x03` 很罕见）再把 N 条响应全读掉；
   ⚠️ **不要用"短超时读一下"去 flush** —— WebUSB 没有取消接口，被弃置的 `transferIn` 会偷走下一条响应。
   （同一个探针用 libusb/pyusb 一直是好的：libusb 认领接口时会自己 clear_halt 冲掉这些数据。）
5. **DAP_SWJ_Sequence 的位计数是 1 字节**（0 = 256），不是 2 字节；写错会让数据整体错位一格。
6. **DAP_Transfer 请求布局**：`[命令, DAP索引, 传输条数, (请求字节 + 4 字节数据)×N]`。
   漏掉"索引+条数"两个字节 → 固件把请求字节当条数 → 响应 `count=0 / ACK=0`。
7. **一整包发送**：命令补齐到整包（512 B）再发，和验证过的裸客户端一致。
8. **复位别用 `DAP_ResetTarget`**：它会把目标"复位并停住"，甚至留在半启动状态。
   用 **nRESET 脉冲**；而**拉过 nRESET 之后 SWD 可能整条哑掉**（再怎么发激活序列都 NO ACK），
   这时唯一的干净恢复是**重开 USB 会话**（释放接口再认领 + 重新初始化）——已经实现在 `reset()` 里，
   不用再让用户拔插。`SWJ_Pins` 的 `wait` 参数单位是**微秒**。
9. **FAULT 之后必须写 DP ABORT 清 sticky**，否则后续 AP 访问全部继续 FAULT（像是"探针瞎了"）。
10. **控制块要校验结构再认**：RAM 里可能出现**上一次固件编译留下的旧控制块**（"SEGGER RTT" 字符串还在），
    锁错了就会读出垃圾通道项。`Rtt.validate()` 会检查通道数、缓冲大小、读写指针、缓冲指针对齐。
11. **页面在后台时定时器被限速**（实测轮询从 ~190 Hz 掉到 ~4 Hz）。跑自动化测试要加
    `--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding`。

## 五、走桥时的节奏（OpenOCD Tcl RPC）

一轮 `readUp` 是 3 条 RPC（读通道项 + 读数据 + 回写 RdOff）。按 WebUSB 那种 190 Hz 猛刷就是
~570 条/秒，OpenOCD 的 Tcl 口扛不住，会冒出 `read_memory 只回来 0 个字` 的瞬时失败。
所以桥后端的轮询有 **30 ms 间隔下限**，并且 `readMem` 遇到短读会重试一次。
OpenOCD 的读数本来也只有 ~17 KB/s，快轮询没有意义。

## 五、目标固件（`tools/target-firmware/stm32f103`）

一份给本工具当靶子的最小固件：UART（USART1，PA9/PA10，115200）+ SEGGER RTT
（ch0 `Terminal` 带 ANSI 颜色、ch1 `Log`），命令既能从串口敲也能从 RTT 下行敲，输出同时进两路。

- 命令：`help / info / uptime / echo <文本> / led on|off|toggle / hex / ansi / long / flood [KB] / reboot`
- 编译：`pwsh -File build.ps1`（用 `arm-none-eabi-gcc`，不需要 Keil；本机在
  `E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin`）
- 烧录：`pwsh -File flash.ps1`（OpenOCD + CMSIS-DAP；路径会自动转成**正斜杠**，
  否则 Tcl 把 `\t`/`\b` 当转义吃掉）
- 串口自测：`python test_uart.py`（DAPLink 的两个 CDC 都试；本机 **COM66** 才是桥到 PA9/PA10 的那路，
  COM61 是探针自己跑的 PikaPython REPL）
- ⚠️ 固件的 UART 输出必须**有忙等上限，而且上限要小**：DAPLink 的 CDC 串口没主机读的时候会堵，
  死等 `TXE` 会把主循环一起卡死（现象："RTT 日志还在刷，但发命令没反应"）。
- 🚨 **`out()` 必须"先写 RTT、再写串口"**：反过来时串口那一侧的堵塞会把 RTT 输出一起饿死 ——
  实测现象极具迷惑性：**ch1（只走 RTT）正常刷，ch0（先写串口再写 RTT）一个字节都没有**。
  这是本项目里最后一个把"下行命令没反应"伪装成"RTT 坏了"的 bug。

## 五点五、2026-09-27 深度排查新增（WebUSB 烧录器衍生）

给烧录器做 WebUSB 直烧时，用 pyOCD(LibUSB) 对照法又挖出三条，全部已落实到代码：

12. **TAR 写是 posted 的，有竞态**：写 TAR 后立刻访问 DRW，可能打到**上一个 TAR 地址**
    （实测：设 TAR=CPUID 后读到 DHCSR 的值）。RTT 轮询里 TAR 每次都变，竞态命中时
    读到的就是控制块/别处内容 —— 这就是「错位读」和 log 乱码的根源之一。
    修复：`_setTAR` 写完补一次 AP 读作屏障（读强制 posted 写完成）。
    实测修复后 flash 区 30/30 次读稳定正确。
13. **AP CSW 别乱动**：CSW 读回 0x23000052（DeviceEn=1、MasterType=DBG 等），pyOCD
    会写成 0x23000012/0x6F000052 再写回 —— 试遍各种组合（含 HNONSEC/MasterType/
    DeviceEn 变体）对本探针的 PPB 写通路都没有改善，保持读回默认值即可。
    ⚠️ 但要**确认 AddrInc=1**（CSW bit4-5 = 01）：目标复位后 CSW 会回到 0x23000042
    （自增关），必须重设。
14. ~~**本探针固件（CherryUSB）的 PPB（0xE000EDxx 调试寄存器区）写在 WebUSB 路径下
    会被静默丢弃**~~ —— **这条结论是错的**，见下方第 16 条：写一直是好的，坏的是**读**
    （JS 位运算把地址变成负数 → 读回空数组）。RTT Viewer / WebUSB 烧录现在能正常工作
    就是最好的证明。

### ⚠️ 第 12 条的更正（2026-09-27 晚，用户报障后复测）

12 的"修复"是**反的**：`_setTAR` 写完 TAR 后补任何"屏障读"都更糟，两种都试过：
- 读 **AP CSW / AP DRW**：AP 读是**挂起读**（读回上一笔读事务的值），这一下会把旧值
  顶进流水线 → 紧接着的访问拿到残渣 → DHCSR 轮询报「S_REGRDY 没置位」、RTT 直接连不上；
- 读 **DP RDBUFF**：不污染 AP 流水线，但会让 `_targetInit` 里的「写 DP SELECT」FAULT。

现在 `_setTAR` 只做「冲 posted 写（DP RDBUFF，且只在写之后）→ 写 TAR」，不加屏障读。
另外：**TAR 每次访问都重写，不做"地址没变就跳过"的缓存** —— CSW.AddrInc=1 会让 TAR
自动前进且不回来，同址连读第 2 次起读到的就是后面几个字（「错位读」的真身）。

## 五点六、2026-09-27 晚：RTT 连不上 / 烧录校验失败 的完整根因

用户报「RTT 总是连不上、偶尔又能连；WebUSB 烧录像是某个寄存器写不进去」，
逐条复现并修复，全部有裸客户端 / 浏览器线级抓包双向印证（`tmp/dap_*.py` 是这次的排查脚本）：

15. **掉电-上电必 FAULT（RTT 连不上的元凶）**：`_targetInit` 里先写 DP CTRL/STAT=0
    掉电、50ms 后再写 0x50000000 上电 —— **那个上电写在本探针 + F103 上必 FAULT**，
    `_transfer` 见 FAULT 直接抛错。更坑的是炸过一次后 DP 停在"掉电已请求 + sticky"，
    **下一次连接继续炸**（自锁）→ 表现为"总是连不上、偶尔能连"。
    → 正常路径**只写上电位**；FAULT 时 ABORT 清 sticky + 重走 SWD 激活序列后重试；
    掉电-上电只留在 `recover()` 当最后手段。
16. **`readMem` 地址 32 位有符号溢出**：`addr & ~3` 是 **32 位有符号**运算，地址一旦
    ≥ 0x80000000（PPB 区就是：DHCSR=0xE000EDF0）就变负数，而 `addr` 本身还是正数 →
    `addr - start` 差出 2^32 → `subarray` 越界 → **返回空数组（不是报错！）**。
    后果极隐蔽：`isHalted()` 永远读不到 S_HALT；`regRead/regWrite` 永远等不到 S_REGRDY
    —— 这就是「固件能写入、但某个寄存器写不进去」的真身（其实写对了，是读全空）。
    → `readMem`/`writeMem` 一律先把 addr `>>> 0`，start/end 也 `>>> 0`。
17. **块访问跨 4KB 边界时 TAR 自增绕回页首**：地址自增是**有界**的，本机实测走到 4KB
    边界就绕回本页开头（读：读到页首数据；写：**写进错误地址**）。
    → 块大小按 1KB 收窄，且新块落在 4KB 边界时重写 TAR。
18. **探针会把块读响应截短**：120 字的请求常常只回一部分，旧代码把**没填到的字静默留 0**
    → "校验读到 0x0，其实 flash 是对的"；RTT 表项读取也会拿到假 pbuf/wr。
    → 按响应里真实返回的条数推进并接着读完。
19. **flashloader 三个坑**：① LR 必须=`load_address | 1`（算法 blob 第 0 条指令就是
    `BKPT`，算法 `bx lr` 就停回来；写 0xFFFFFFFE 会跑飞 → 只能等超时）；
    ② PC 必须**最后**写（写 PC 即跳转执行，先写它后面的参数就白写）；
    ③ 跑算法前必须**摁住中断**（SysTick + NVIC），否则擦掉向量表后中断进来直接 **LOCKUP**
    （C_HALT 清不掉，只能复位）。
20. **写完 TAR 不要插"prime 读"**：读会让 TAR 自增 —— prime 读 2 个字就让正式读从
    **start+8** 开始（实测报错 `0x8000000 处读到 0xb9，期望 0x0`，而 b9 正是偏移 8
    处的字节）。**这颗探针的读没有挂起读滞后**，读到就是读到的地址。
21. **烧完必须用 AIRCR 系统复位（SYSRESETREQ），不能只拉 nRESET**：很多接线（本机这块
    F103）**根本没把 NRST 连到探针**，拉引脚等于没复位；而跑 flashloader 前
    `maskInterrupts()` 关过目标的 SysTick/NVIC → 固件"能跑但不打印"（RTT 连得上、
    控制块也对、一个字节都不来）。
22. **DP SELECT 要在上电之后写**（pyOCD 同序）：上电前写在状态不干净时会直接 FAULT
    （`SWD FAULT（…地址 0x8）`）。
23. **WebUSB 没有取消接口**：`withTimeout` 超时只是"我们不等了"，底层 bulk 传输仍挂着
    → 会偷走下一条响应，攒多了甚至把 `navigator.usb.getDevices()` 卡死（实测烧录器
    卡在第一步 90 秒以上）。→ 超时即把设备标脏，下次认领前 `device.reset()` 做
    **USB 端口复位**；所有 USB 操作（open/claim/clearHalt/transferIn/Out）都要有超时。
24. **内存访问必须互斥**：RTT 轮询（读上行 + 推进 RdOff）与用户下行发送（读下行表项 +
    写数据 + 写 WrOff）并发时会交错，而 TAR / 读流水线是**共享状态** → 下行命令"发送
    成功"但固件一个字节都没收到。→ 探针级 promise 链互斥（`_withLock`，可重入）。
25. **两档访问策略（把烧录与 RTT 分开优化）**：`probe.fast`
    · `fast=false` 严格档（默认；烧录器 + 用户手动动作）：小块读双读、每次写都回读校验；
    · `fast=true` 快速档（RTT 后台轮询）：RAM 小块单读（`Rtt._entry` 结构校验不过才重读）、
      同地址的重复写不回读；
    · **PPB 区（DHCSR 等状态位）任何档位都双读**，不许省 —— 误判=烧录校验失败。
    吞吐实测（8MHz、hello world 吞吐固件）：154（全严格）→ 209 → 249（读分级）→
    259.5 KB/s（再去掉写回读节流）；历史峰值 330 KB/s，差额是保正确性的固定开销。
26. **烧录器自己开的 probe 会话必须 disconnect**（成功失败都要）：否则接口一直被占着，
    现象是"烧录失败后 RTT 连不上、再点烧录报『占用 USB 接口失败』，只能刷新页面"。

自测与一键命令见根目录 `Makefile`（`make` 看帮助；`make test/test-hw/fw-restore/check`）。

### 仍待查（诚实记录）
- `tools/selftest/browser-hw.test.mjs webusb` 的「下行 help 回包」仍**偶发**超时；同一个
  动作单独跑 `tmp/rtt-downlink.mjs` 稳定通过（`✅ 收到完整命令列表`）。怀疑是该用例
  "连接后 1~2 秒就发命令"的时序差异，下一轮专门查。**界面里手动点下行发送是通的。**
- 用 OpenOCD 反复烧过之后，偶发需要先 `make fw-flash`（OpenOCD 自带完整复位）清一次场，
  WebUSB 侧才肯重新连接；疑与 LOCKUP/中断屏蔽的残留状态有关，已加软复位兜底，继续观察。

## 五点七、STM32H7B0（value line）接入记录（2026-09-27，板子未到 → 只做静态验证）

给 H7B0 做 RTT 吞吐测试固件时，工具链这边补了这些（都用不依赖硬件的检查验过）：

| 事项 | 做法 | 验证方式 |
|---|---|---|
| flash 算法进 `algos.js` | **不许手抄 base64**：`tools/dev/extract-algo.py` 从本机 pyOCD（0.45.1）的 `target_STM32H7B0xx.py` 抽 `FLASH_ALGO`，生成条目后由脚本插入 | `make algo-check` |
| 算法条目自洽性 | 新增 `tools/dev/verify-algo.py`：blob 可解码、四个入口在 blob 内且指向**有效 Thumb 指令**、static_base/页缓冲/栈与 blob 不冲突、flash 参数与编程粒度合理 | 8 个系列全过 |
| 烧录计划 | 新增 `tools/dev/check-flash-plan.mjs`：纯计算演练"擦除 → 分块 → 尾块补齐 → 范围检查" | H7B0 固件：擦 1 次(8KB)、编程 1 次(尾块补到 32B)、范围 ✅ |
| 编程粒度 | H7 的 flash 按 **256 位（32B）flash word** 编程 → 条目加 `write_granularity: 32`，`flash/view.js` 的尾块补齐改成按它（默认 4） | `make flash-plan` |
| 芯片预设 | `app/core/chips.js` 加 `stm32h7b0`：扫描范围给 DTCM + AXI SRAM **两段**（H7 内存是散的） | 页面「芯片」下拉 |
| 桥目标 | `bridge/bridge.config.json` 加 `stm32h7b0`（OpenOCD 用 `stm32h7x.cfg`，其 RM0455 分支认 DBGMCU id 0x480） | `make bridge` + 页面连桥 |
| H7 特有寄存器 | 固件 README 记了 9 条坑，关键是：**PWR/SYSCFG 时钟要先开**（`RCC_APB4ENR`，否则 PWR/SYSCFG 的写被静默忽略）、VOS0、升频前先加 flash latency | 逐条指令反汇编核对过 |

顺带修掉一个**所有芯片都受益**的真 bug：`FlashRunner.chunkSize()` 单缓冲时用
`begin_stack - page_buffers[0]` 当容量，而 L0/F0/F4 的算法把页缓冲放在**栈顶之上**（差值为负）
→ 旧写法回落成 256B，比 L0 的 128B 页还大。改成单缓冲直接按页大小（pyOCD 保证缓冲 ≥ 一页），
现在 L0 的块是 128B ✅。

> 真机步骤与预期数字见 `tools/target-firmware/stm32h7b0_rtt_speed/README.md` 的 bring-up 清单。
> 实测吞吐数字待板子到手后补。

## 五点八、STM32H7B0 首次接入失败记录（2026-09-27，硬件侧待查）

自制 H7B0 板接上 MicroLink（SWD）后的实测现象：

| 操作 | 结果 |
|---|---|
| 读 DP IDCODE | **0x6BA02477** ✅（H7 家族 SW-DP；连读 5 次稳定） |
| 读 DP CTRL/STAT | 0x00000000（CSYSPWRUPACK=0、CDBGPWRUPACK=0） |
| 写 DP SELECT / CTRL/STAT | **FAULT(4)** ❌ |
| 读 AP CSW | FAULT ❌ |
| nRESET 引脚 | 读到高；主动拉低再放开也一样 ❌ |

**软件侧已排除的项**（都有脚本，全部无效）：写序调换、延迟 50ms、SWD 时钟
250k/500k/1M/2M、`SWD_Configure` turnaround 1~4、线复位 64/256 位、重新激活、
DAP_WriteABORT 清 sticky、**复位下连接**、把"读 IDCODE + 写寄存器"放进同一条 DAP_Transfer。

**结论**：SWJ-DP 本体活着（IDCODE 读得到），但它后面的**调试/系统电源域起不来**，
或器件处于**禁止调试写入**的状态。最可能是 **VCORE 没起来**（自制板漏了 VCAP 的 2.2µF、
VDDA/VDD33USB 供电不全、QFP 电源脚虚焊），其次才是选项字节（RDP/TZEN）。
—— 顺带修正一条旧认知：H7 **不容忍**只置 `CDBGPWRUPREQ` 的上电值（F1 能凑合），
所以 `dap-webusb.js` 现在写 `0x70000000`（两个请求位都置），并会把两个 ACK 位分别报出来。

复现脚本（都在 `tmp/`，裸 CMSIS-DAP，不依赖网页）：
`h7_dp_probe.py`（策略梯子）、`h7_last_attempts.py`（turnaround/时钟/批处理/复位下连接）、
`h7_release_nrst.py`（规范地驱动 nRESET 后重连）。


另：激活序列「必须一次发 88 位」的旧结论只对部分固件成立 —— pyOCD 按
[51 个 1][0x9EE7][51 个 1][8 个 0] 分四条发也能工作。写法不唯一，别照抄文档教条。

## 六、自测现状（2026-09-26 全部真机通过）

| 用例集 | 结果 | 命令 |
|---|---|---|
| Node 协议层（控制块定位/绕回/丢包/下行/ELF/HEX） | **34/34** | `node tools\selftest\rtt.test.mjs` |
| 桥端到端（真板，上下行 + 复位 + flood） | **19/19** | `node bridge\rtt-bridge.mjs --target stm32f103` → `node tools\selftest\bridge.test.mjs` |
| 浏览器无硬件（演示串口，整条 UI 链路） | **15/15** | `index.html?demo=serial&selftest=1` |
| 浏览器 + 真探针（WebUSB 零安装 RTT） | **11/11** | `node tools\selftest\browser-hw.test.mjs webusb` |
| 浏览器 + 桥（OpenOCD 后端） | **5/5** | `node tools\selftest\browser-hw.test.mjs bridge` |
