# 交接报告 · HPM6800EVK：例程烧录 + RTT 转发测速（2026-10-01）

> 接手前**先读第 0 节和第 6 节**（当前状态与恢复步骤），第 3 节是根因链，第 4 节是下一步。

## 0. 一句话状态

| 目标 | 状态 |
|---|---|
| 烧 `lwip_tcpecho`（插桩版）到 HPM6800EVK | **通了，但我们的 WebUSB 路径仍偶发卡死**；OpenOCD 路径 **3/3 稳过、约 5 s** |
| 用 RTT 转发测速 | ✅ **完全达成**（打流中 **1.387 MB/s**，与仓库 HPM 基线 1.377 吻合） |
| 用 RTT Viewer 测速 | ✅ 达成（空闲 43~56 KB/s；打流中 12.8~64 KB/s，零错位读） |
| 10 s 存盘对账 | ✅ 13~14 MB，一致性 **99.5%**，内容是真实 trace 文本 |

> 🔄 **2026-10 续查（本文件第 7 节）**：「偶发卡死」已查明是**确定性缺陷**，根因与 OpenOCD 的差异
> 都用 LA 解码波形锁定了，**verify 那条已在 `d353b4c` 修掉并真机验证**（243 KB 校验 3.1 s 通过）。
> 只剩「第一次 erase 卡满 60 s 才自愈」这一条既有缺陷待办（第 7 节末）。

⚠️ **板子当前需要断电重上电**：被卡死的烧录把目标调试模块（DM）留在挂起态
（`haltreq` 写进去也不停核，OpenOCD 同样 halt 不住），**只有整板断电可解**。断电后 TCP 5001 通即恢复。

## 1. 环境与坐标

| 项 | 值 |
|---|---|
| 目标板 | HPM6800EVK（HPM6880，RISC-V 双 hart，JTAG，IR=5，TAP idcode `0x1000563D`） |
| 网络 | 板子 `192.168.100.10/24`（TCP echo 口 **5001**）；PC 侧网卡「以太网」`192.168.100.11/24` |
| 例程源码 | `E:\sdk_env_v1.11.0\hpm_sdk\samples\lwip\lwip_tcpecho`（**全量 trace 插桩** + SEGGER RTT） |
| 构建目录 | `E:\sdk_env_v1.11.0\work\lwip_lwip_tcpecho_hpm6800evk_flash_sdram_xip_debug` |
| 镜像 | `output\demo.elf`（烧写量 **240.6 KB**，flash_sdram_xip） |
| RTT 控制块 | `_SEGGER_RTT = 0x4c0003c0`；上行环 `_acUpBuffer = 0x4c000468`，**8 MB**（SDK 默认），BLOCK_IF_FIFO_FULL |
| 内存属性 | 该区在 linker 里叫 SDRAM_NONCACHEABLE，且 `board_init_pmp()` 对 `0x4c000000..0x4e000000` 配了 `MEM_TYPE_MEM_NON_CACHE_BUF` —— **真非缓存，地址不用改** |
| 探针 | akaLinkPro `0D28:0204`（CMSIS-DAP v2 bulk + CDC=COM5 + 自定义 HID 0xFF00 + DFU） |
| OpenOCD | `E:\sdk_env_v1.11.0\tools\openocd\openocd.exe`（0.12.0+dev-04410-g45e78b300），脚本目录 `tools\openocd\tcl` |
| 探针脚本 | `E:\Share\github\akaLinkPro\script_test\`（`hpm6800_probe.py` / `hpm6800_riscv.py` / `hpm6800_flash_target.py` / `openocd_hpm6800evk_dap.cfg`） |
| 工具仓 | `E:\web-serial-rtt-tools`，分支 `main`，最新提交 **65e302f** |

## 2. 提交记录（工具仓已 push；SDK 仓无远程，仅本地）

| 提交 | 内容 |
|---|---|
| `0e159d6` | fix(rtt/hpm)：缓冲上限 1MB→32MB、`readUp` 分块 64KB、丢失记账按积压、读全 0 重试自愈、SBA 健康自检、`make grant` 三类授权一把梭 |
| `98a84f0` | fix(flash/hpm)：算法"跑不回来"就地自愈重试；失败别把核扔在跑飞态 |
| `56be5e9` | docs：更正"别缩 RTT 环"（缩了会把固件憋死在启动里） |
| **`65e302f`** | **perf(flash/hpm)：对照 OpenOCD 定因后的三处提速 + 两处"别再自伤"的修**（见第 3 节） |
| SDK `7761733` → `52d87b5` | 环 256KB → **撤回，恢复 SDK 默认 8MB** |

## 3. 关键发现（根因链，按重要性）

1. **🚨 拉 nRESET 会把探针自己复位**（"越失败越不对劲"的真凶）
   `openocd_hpm6800evk_dap.cfg` 原文：*"Do NOT use the SRST pin: 20 针排线第 15 脚是探针自己的 RESET_N，
   拉 nSRST 会把探针一起复位（observed as OpenOCD hanging and the probe dropping off USB）"*。
   我们的失败路径原来**不分路径都调 `probe.reset()`（SWJ_nRESET 脉冲）** → 烧录一失败就把探针打掉线 →
   之后所有命令响应错位（实测 `响应回显 0x3 ≠ 命令 0x0`、`DAP_JTAG_Sequence 0xff`、USB 读超时）。
   → 已改为：HPM/RISC-V 路径**跳过 nRESET**，复位目标走 DM 的 ndmreset（`recoverAfterFailure()`）。

2. **DMI 写没有批量**（烧录慢 10 倍的主因）
   原来每个字 = `WRITE` + `NOP` **两次 USB 往返**（1388 B 的 flashloader 就 694 次）。
   → 已改成 `WRITE,NOP,WRITE,NOP…` 压进**一条** `DAP_JTAG_Sequence`（仍每拍收状态，失败退回逐字并对齐地址）。
   效果：**42~78 s → 13.0 s**；JTAG 批次 91633 → 26430。

3. **擦除范围没对齐扇区**
   OpenOCD 会补 `Adding extra erase range, 0x80000000 .. 0x800003ff`；我们把非对齐范围
   （`0x80000400 + 3.1 KB`）直接丢给 ROM API。→ 已对齐（起点向下、终点向上取整到 4 KB）。

4. **探针上报 1024，但 >512 B 的命令不可靠**（2× 提速暂时吃不到）
   固件 `DAP_config.h`：`DAP_PACKET_SIZE 512` / **`DAP_XFER_SIZE = 2×512 = 1024`**，
   `DAP.c:180` 注释明说"不能报端点 mps，否则主机每次只能带 ~508 B"。但实测逐档扫描：
   **请求 577 B 正常；721 B 起响应恒少 222 B；973 B 更乱**。固件侧 `DAP_JTAG_Sequence` 与 OUT 回调
   都没有硬上限 → 怀疑在 USB 多包收发一层，**未解决**。目前仍按 512 B 端点包长算批次。

5. **RTT 环不能缩**（我犯过又撤回）
   缩到 256 KB 后：冷启动那一波插桩 trace（整条 lwip/enet 初始化路径都插桩）**当场灌满环**，
   而通道是 `BLOCK_IF_FIFO_FULL` → 固件**阻塞在 `SEGGER_RTT_Write` 里出不了 `enet_init`**
   → 板子 ping 不通"像坏了"（接上 Viewer 排空才恢复）。8 MB 时启动 trace 装得下，**无人读也能自己起来**。
   要减小积压请在**主机侧**做（本仓已做）。

6. **授权**：Web Serial / WebHID / WebUSB 三类，按「来源 + 设备标识」记；串口那条绑 **USB 口实例 ID**
   （换口 `&N&` 就变）。已做 `make grant`（两个 profile × 三个来源），`make open` / `page-prep` 会自动补。

7. **未解**（第 4 节）：**算法偶尔"不回来"**（60 s halt 超时），且会把目标 DM 卡成"haltreq 无效"，
   只有断电可解。已排除"中断"假说（`mstatus.MIE` 本来就是 0）；卡死瞬间 `dmstatus` 解出
   **`allhavereset=3`** —— 目标在算法运行期间**被复位过**。

### OpenOCD 对照基线（同一块板、同一支探针、同一份 ELF）

| | OpenOCD | 我们（修复前 → 修复后） |
|---|---|---|
| 成功率 | **3/3** | 偶发卡死 |
| 耗时（246 KB） | **4.88 / 5.03 / 5.28 s**（49 KiB/s） | 42~78 s → **13.0 s** |
| 复位流程 | `init → reset init → halt → 写 → 校验 → reset run`，**实测 reset-init（时钟/DDR）在本流程并没跑**，也是直接 halt 一个跑满初始化的 SoC | 同（halt → 烧） |
| 命令风格 | DMI 压批 + **多笔在飞**（队列化） | 同步一问一答（读已批 12 字、写已批 12 字） |

**决定性 A/B**：我们的路径刚报 `DAP 0xff` → **紧接着 OpenOCD 在同一探针上 4.88 s 一次成功**
（没拔插、没复位探针）→ **问题在我们的驱动方式/会话状态，不是硬件。**

## 4. 下一步建议（按性价比排序）

1. **抓卡死瞬间的完整现场**（最该先做）：在 `withAlgoRetry` 的超时分支里 dump
   `dcsr / dmcontrol / dmstatus / sbcs / dpc + 前后各 50 条 JTAG 命令`。
   现有日志：`tmp/hpm-speed-erasehang.log`（卡在擦除）、`tmp/hpm-final-1.log`（卡在擦除的重试）。
   关注点：`allhavereset=3` 是谁引起的（看门狗 EWDG？trap？我们的 ndmreset 泄漏？）。
2. **改成多笔在飞的队列式下发**（照 OpenOCD）：`DapJtagTransport` 目前严格"发一条等一条"，
   而 OpenOCD 的 CMSIS-DAP 驱动会排队多条命令（`MAX_PENDING_REQUESTS=4`）。
3. **给用户一条立刻可用的稳路**：把网页「烧录器」后端切到**本地桥 + OpenOCD**，
   并预置 akaLinkPro 的 `openocd_hpm6800evk_dap.cfg`（改动不大）。
4. 攻 **>512 B 多包命令**（能直接再快 2×）。
5. 顺手：`hpm6800_riscv.py stop` 必须在 OpenOCD 之前跑，否则报
   `Unsupported DTM version: -1`（探针自己的 RISC-V 引擎占着 TAP）。

## 5. 复现命令与脚本清单

```powershell
# ---- 我们的路径（页面编排，CDP 驱动 9333 上的授权 Chrome）----
cd E:\web-serial-rtt-tools
node tmp/hpm-tcpecho-speed.mjs --stage=flash      # 只烧录
node tmp/hpm-tcpecho-speed.mjs --stage=measure    # 全流程：Viewer → 转发 → 打流 → 存盘
node tmp/hpm-tcpecho-speed.mjs --stage=fwd --reset-first   # 只测转发（可先复位目标）
node tmp/hpm-tcpecho-speed.mjs --stage=diag       # 现场取证：控制块/表项/计时读/分阶段耗时
node tmp/tcp-echo-load.mjs --conns=8 --secs=20    # TCP 打流器（→ 192.168.100.10:5001）
node tmp/page-eval.mjs "<任意表达式>"              # 在页面里执行（诊断用，带 90s 看门狗）

# ---- OpenOCD 路径（稳，推荐先用）----
cd E:\Share\github\akaLinkPro
py script_test/hpm6800_riscv.py stop              # 让探针 RISC-V 引擎放开 TAP（必须）
py script_test/hpm6800_flash_target.py "E:\sdk_env_v1.11.0\work\lwip_lwip_tcpecho_hpm6800evk_flash_sdram_xip_debug\output\demo.elf"
py script_test/hpm6800_probe.py info | set-mode 1 | reset    # 探针：查看/切 SWD+JTAG/重启

# ---- 离线回归（不需要硬件）----
node tools/selftest/hpm-flash.test.mjs      # 79 项（模拟 TAP+DTM+DM+SBA+XPI）
node tools/selftest/rtt.test.mjs            # 45 项
node tools/selftest/hid-proto.test.mjs      # 55 项
node tools/selftest/flash-image.test.mjs    # 10 项

# ---- 页面/服务 ----
node tools/dev/serve-nocache.mjs 8899       # 静态服务（不发缓存）
make grant                                  # 补探针三类授权（换 USB 口后要重跑）
make open                                   # 起服务 + 浏览器（自动补授权）
```

**只在本地的脚本**（`tmp/` 被 `.gitignore` 排除，没进仓库）：
`hpm-tcpecho-speed.mjs`（主编排）、`tcp-echo-load.mjs`（打流）、`page-eval.mjs`（页面取证）、
`ocd-full2.log`（OpenOCD 完整日志）、`hpm-speed-*.log` / `hpm-tcpecho-forward-*.bin`（证据）。

## 6. 现场状态与恢复步骤

1. **板子**：先**断电重上电**（清 DM 挂起）。恢复判据：`Test-NetConnection 192.168.100.10 -Port 5001` = True。
2. **探针**：正常应处于 `output_mode=1 (SWD+JTAG)`；不放心就 `py script_test/hpm6800_probe.py reset` 重启它。
   若 OpenOCD 报 `Unsupported DTM version: -1` → 先 `py script_test/hpm6800_riscv.py stop`。
3. **网页环境**：`node tools/dev/serve-nocache.mjs 8899`（**它会莫名死掉，是本会话踩过的坑**）+ 9333 CDP 浏览器。
4. **测量基线**（可复现，用于判断有没有回归）：
   - RTT 转发打流中 **1.387 MB/s**（探针侧 1.38~1.48）；空闲 7~10 KB/s
   - RTT Viewer 空闲 **43~56 KB/s**（8.4 Hz、零错位读）；打流中 12.8~64 KB/s
   - 10 s 存盘 13~14 MB、一致性 **99.5%**、内容是 `文件:行| 函数` 形式的 trace 文本
   - 打流时**必须挂着读者**（转发或 Viewer 任一），否则环会被灌满、固件被憋住

---

## 7. 续查（2026-10-01 晚）：LA 解码 OpenOCD 波形 → 定因 → 已修

### 7.1 「偶发卡死」其实是**确定性缺陷**

翻遍 4 份历史日志 + 本会话复现 5 次，模式 100% 一致（不是"偶发丢拍"）：

* **`read`（校验）在 flash offset ≥0x30000 且尺寸 ≥32768 时必卡**；
  同址 4096/8192/16384 正常，0x0/0x10000/0x20000 各 65536 也正常
  → 坏地址落在 **(0x34000, 0x38000]**；真机表现是"校验走到 81%（第 4 块）卡死 60s×2 → 整轮失败"。
* **第一次 `erase` 也必卡满 60 s**（靠 withAlgoRetry 自愈才过），见 7.4。

卡死瞬间的现场（`tmp/hpm-xip*.json`）：

* `dmstatus` 恒报 **running**；**写 haltreq 也停不住核**；抽象命令 `cmderr=4`（halt/resume）；
* **SBA 读 ILM[0..48] 仍是我们的算法（48/48 字节一致）** → 不是"应用重启覆盖了 ILM"；
* `dtmcs.dmistat = 0` → **DTM 没进错误态**，写 `dmireset` 无效（这条假说排除）；
* → **核楔在一条永不完成的 XPI 总线事务上**，只有 ndmreset / 整板断电可解。

### 7.2 两条读路径的可行性（关键判据）

| 路径 | 结果 |
|---|---|
| SBA 直读 XPI 窗口 `0x80000000` | ❌ 自己就挂（`dm.readMem(0x80000000,16)` 超时） |
| CPU 走 XIP 窗口 `lw`（progbuf 执行） | ✅ 0x80000000/0x30000/0x34000/0x36000/0x38000/0x3E000 全部 ~6 ms 秒回真实固件内容 |

### 7.3 LA 解码 OpenOCD：差异只有一条

抓法：LA CH0..3 = TCK/TMS/TDI/TDO，500 MS/s（探针的 JTAG 是**固定时序 ASM blob**，
`adapter speed` 基本被忽略 —— 4 KB 写 100 kHz 与 8 MHz 都是 ~0.14 s，所以只能靠高采样率硬啃）；
用户在 KingstVIS 里解码后导出，本仓 `tmp/la-parse-decoded.py` 解成 DMI 流水账
（220133 次扫描 / 覆盖 2 次完整全量烧录；`tmp/la-jtag-decode.py` 是自带 TAP 状态机的流式解码器）。

| | OpenOCD | 我们（改前） |
|---|---|---|
| `sbcs`/`sbaddress0`/`sbdata0` 写入 | **0 次**（完全不用 SBA） | 全程 SBA |
| `cmdtype=mem`（抽象内存访问） | **0 次** | 0 次 |
| 读目标内存 | progbuf 跑 `lw s1,0(s1)`（CPU 自己走 XIP 窗口） | SBA |
| 写目标内存 | progbuf 跑 `sw s1,0(s0); addi s0,s0,4`（配 `abstractauto`，一个 DATA0 写搬一个字） | SBA |
| 擦/写 flash | 算法 `init/erase/program`，参数与我们**一模一样**（`a0=0x80000000 a1=off a2=size`） | 相同 |
| fence 段 | `fence.i; fence rw,rw(+ebreak)`（progbuf） | **逐字相同** |
| **读回校验** | **根本不读**：`read`（dpc=0x12）入口调用次数 = **0**，`flash write_image` 不校验 | 调 `read` → ROM `flash_read` → **楔死** |

**⇒ 差异就是：我们多做了一步"用 ROM 的 `flash_read` 读回校验"，而这一步在这颗芯片上会楔死总线。**
OpenOCD 要么不读，要么就用 **CPU 走 XIP 窗口**读（progbuf），**从来不碰 ROM 的 read**。
（`nor_config` 解出来完全正常：size=16384 KB / page=256 / sector=4 KB / block=64 KB，
"erase 除零"假说排除；算法 blob 与 SDK `samples/openocd_algo` 的源文件**逐字节相同**，
所以差异只在"怎么驱动"，不在算法本身。）

### 7.4 已修：verify 改走 CPU + XIP 窗口（提交 `d353b4c`）

* 新增 `app/flash/hpm/xip-copy.js`：**手工汇编**的 7 条指令拷贝例程（`lw/sw/addi×3/bne/ebreak`，
  28 B），装载到 SRAM `0x600`（算法 blob 之后、scratchInfo 之前）；自测会**反解机器码逐条核对**。
* `flash.js`：`call()` 抽出 `callAt(addr,…)`（算法入口与例程共用同一套
  "写 a0..a4 → prepareRun → resume → waitHalted"）；`setup()` 每次写例程并读回校验；
  `verify()` 用 `copyFromXip()` 从 XIP 窗口搬进中转区再比 —— **完全不碰 ROM 的 `flash_read`**。
* 自测：`hpm-sim.mjs` 认这条例程，并按实测阈值**建模"ROM read 楔死总线"**；
  `hpm-flash.test.mjs` §4b：反解 7 条机器码 + 0x30000 起校验 64 KB 通过且 ROM read 调用 0 次 +
  反证（真去调 `read` 读同一形状 → 模拟目标当场楔死）。`make test-hpm` 91 项全过。

**真机验证（demo.elf 246360 B）**：

| 段 | 长度 | 擦除 | 编程 | **校验** |
|---|---|---|---|---|
| `0x80000400` | 3216 B | 63.3 s（见 7.5） | 52 ms | 56 ms |
| `0x80003000` | 243144 B | 1.9 s | 2.8 s | **3.1 s** |

改前 243 KB 校验**必卡**（60 s 超时 → 恢复 → 再 60 s → 整轮失败），现在 3.1 s 读完。

### 7.5 也修了：第一次 `erase` 卡满 60 s（提交 `8ecba48`）

现场（`tmp/hpm-erasehang2/3/4.json`）与 7.1 同形：核楔在永不完成的 XPI 事务上，ILM 48/48 完好。
用 `tmp/la-our-capture.py`（页面侧 `/tmp/la-GO` 对齐窗口）抓下**我们自己**那条 erase 的 DMI 流水，
和 OpenOCD 同一调用的流水逐条对齐 —— **入口 dpc=0x6、参数 a0=0x80000000 / a1=0x0 / a2=0x2000、
fence 段、resume 方式全部一致**，所以差异不在"怎么调"，而在**调用前的状态**。

真机 A/B（同参数 erase 8192 B）：

| 前置 | 结果 |
|---|---|
| 直接 `setup()` → `erase` | **卡 >60 s**（靠 withAlgoRetry 自愈才过） |
| 先 reset-**run**（ndmreset 后应用立刻重启并重配 XPI）→ setup → erase | 仍然卡 |
| 先 reset-**halt**（ndmreset + 保持 haltreq，核停在复位向量）→ setup → erase | **118 ms 通过** |

**根因**：目标上跑着 `flash_sdram_xip` 的应用时它**已经把 XPI 配过一遍**；在这种状态上跑
`flash_init`（ROM 的 auto_config 再配一遍）之后，**第一次写类操作（erase）**就会楔死。
reset-halt 让 XPI 从 POR 态由我们重新配置，问题消失。

**改法**：`riscv-dm.js` 新增 `resetHalt()`；`flash.js` 的 `setup()` 先做 reset-halt
（`resetFirst:false` 可关）；`hpm-sim.mjs` 补上"松开 ndmreset 时核是跑还是停，取决于 haltreq
有没有保持"这条真机语义（我们的修复正依赖这个差别，模型不补就自测不出来）。
已排除：SBA 残留（置只写模式后 `sbbusy=0`）、DMI 停摆（`dtmcs.dmistat=0`）。

**真机端到端（demo.elf 246360 B，两个修复一起）**：

| 段 | 擦除 | 编程 | 校验 |
|---|---|---|---|
| `0x80000400`（3216 B） | **122 ms**（原 63,336 ms） | 48 ms | 53 ms |
| `0x80003000`（243144 B） | 1857 ms | 2675 ms | 3096 ms |

**整轮 8.2 s** ✓（OpenOCD 基线 ~4.9 s；改前要么 verify 卡死整轮失败、要么侥幸过了 71 s）。

### 7.6 复现这套排查的工具（都在 `tmp/`，未进仓库）

| 脚本 | 干什么 |
|---|---|
| `tmp/la-ocd-full.py` / `la-ocd-capture.py` | 抓 OpenOCD 的完整/单次烧录波形（GO 文件握手对齐窗口） |
| `tmp/la-our-capture.py` | 抓**我们自己**这条路的波形（页面侧 `opts.waitGo` 等 `/tmp/LA-GO`） |
| `tmp/la-jtag-decode.py` | 自带 TAP 状态机的**流式** JTAG 解码器（吃几百 MB 跳变表；41 位 DR ↔ DMI） |
| `tmp/la-parse-decoded.py` | 解 KingstVIS 的 JTAG 解码导出（`Time[s],TAP state,TDI,TDO`）→ DMI 流水账 |
| `tmp/hpm-forensic.mjs` + `*-page.js` | CDP 驱动真页面跑实验（**会强制重载页面**：ES 模块按 URL 缓存，不重载会跑到旧代码） |
| `tmp/hpm-flashfix-page.js` / `hpm-erasehang-page.js` | 端到端烧录计时 / erase 卡死取证（支持 `resetFirst`/`resetHalt`/`waitGo` 三个开关） |

两条踩过的坑：① LA 只能**自由采集 + 边沿触发**，所以窗口对齐靠"让对方等一个 GO 文件"；
② 这个探针的 JTAG 是**固定时序 ASM blob**，`adapter speed` 基本无效 —— 想解码只能上 500 MS/s 级采样。