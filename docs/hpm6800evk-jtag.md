# HPM6800EVK（HPM6880, RISC-V）用 akaLinkPro 探针调 JTAG —— 打通与提速

> 结论一句话：**RISC-V 走 JTAG 时，主机侧驱动（OpenOCD）是往返延迟受限的，
> 96~154 KB/s 就是天花板；把搬运搬进探针固件后到 1.19 MB/s（约 9 倍）**，
> 而这个提升的关键不是"JTAG 汇编优化"本身（那部分只占 +24%），
> 而是**让探针自己发 DMI 扫描**、彻底干掉每次访问一个 USB 往返。

---

## 1. 硬件与接线（先说两个坑）

| 项 | 值 |
|---|---|
| 目标 | HPM6800EVK（板上 SoC = **HPM6880**，双核 RISC-V，TAP IDCODE `0x1000563D`） |
| 探针 | **HPM5301EVKLite 移植板**（固件 `build_dfu_evklite`，CDC=COM5，output_mode=1 = SWD+JTAG） |
| 链路 | 探针 J5 20 针 ⟷ EVK **CN1** 20 针牛角座，EVK 上 TRST/TDI/TMS/TDO/TCK 五个跳线帽必须拔掉 |
| RISC-V | **只有 JTAG，没有 SWD**；`transport select jtag` |
| 探针 JTAG 引脚 | `TCK=PA06 TMS=PA07 TDI=PA05 TDO=PA04`（`-DJTAG_PIN_*_SHIFT` 按板子传） |

### 坑 ①：20 针排线的第 15 脚会打到自己

HPM5301EVKLite 的 **J5.15 就是它自己的 `RESET_N`**（README 早有警告），而 CN1.15
是目标 nSRST。于是 **OpenOCD 一 assert srst，探针自己先被复位掉** —— 现象是
OpenOCD 卡死、探针从 USB 上消失（本次复现过两次）。所以本仓库的 cfg 用：

```tcl
reset_config none      # 走 Debug Module 的 ndmreset，不碰 SRST 引脚
```

`reset_config srst_only`（SDK 自带 `probes/cmsis_dap.cfg` 的默认）在这套接线下
**不能用**。探针掉线后的恢复脚本：`script_test/probe_usb_recover.ps1`（给它所在
的那个 USB hub 断电重上，因为板子是 USB 供电的，顺带复位 MCU）。

### 坑 ②：`adapter speed` 对 JTAG 扫描无效

`JTAG_Sequence()` → `JTAG_Sequence_GPIO_ASM_45M(info, tdi, tdo, 0)` **把 delay 写死
传 0**，汇编里也根本没读这个参数，而时序是 `.equ` 常量。所以 JTAG 侧
`adapter speed` 是个摆设（SWD 侧有 6 档运行时加载的 blob，JTAG 没有）。

---

## 2. 阶段一：调试跑通

```bat
:: 探针切 JTAG（TDI/TDO 与 VCOM 共用引脚，必须切模式）
python script_test\hpm6800_probe.py set-mode 1
:: OpenOCD 接目标
openocd -s <sdk>\tools\openocd\tcl -s <sdk>\hpm_sdk\boards\openocd ^
        -f script_test\openocd_hpm6800evk_dap.cfg -c "init" -c "halt" -c "reg" -c "shutdown"
```

实测全部通过：TAP 识别 → `datacount=4 progbufsize=8` → `XLEN=32 misa=0x4094112d`
→ halt / 寄存器 / 内存读写（写 `0xDEADBEEF` 读回一致）/ 擦写校验 NOR flash
（`flash write_image erase` 40 KiB/s，写完后 `mdw` 读回的字节与 `.bin` 逐字节一致）。

**烧录 HPM6800EVK 不需要 J-Link**：SDK 的 `boards/openocd/boards/hpm6800evk.cfg`
带 `hpm_xpi` flash driver，本探针在 JTAG 下直接就能烧：

```bat
python script_test\hpm6800_flash_target.py <image.bin>
```

---

## 3. 阶段二：SRAM 读写速度（两种口径）

`script_test/sram_speed_hpm6800.py`（主机侧、OpenOCD `load_image`/`dump_image`，
AXI_SRAM `0x01200000`）：

| OpenOCD 后端 | 写 | 读 |
|---|---|---|
| `progbuf`（默认） | 154.0 KB/s | **95.9 KB/s** |
| `sba` | 87.0 | 85.3 |
| `abstract` | 14.5 | 14.4 |

换后端救不了。原因是**往返**：在 OpenOCD 内部用 Tcl 循环量到单次 DAP 命令
往返 ≈ **97 µs**，而读 40 µs/字、写 26 µs/字，正好等于"**每个 abstract command
（最多 4 字）一个 USB 往返**"。所以主机侧驱动 RISC-V 天生就是延迟受限的，
探针侧再快也没用。

---

## 4. 阶段三：探针侧 RISC-V 引擎（核心工作）

新增 `firmware/application_5301/src/riscv/`：

- `riscv_jtag.c` —— JTAG TAP 导航 + DMI 41 位请求/响应流水线 + **SBA
  （System Bus Access）块搬运**（`sbautoincrement` + `sbreadondata`，
  稳态 **一次 DMI 读 = 一个 32 位字**）；
- `riscv_svc.c` —— 延迟执行层（JTAG 位操作**不能**在 USB 中断里跑，
  与 RTT 桥同一规矩），HID `CMD_RISCV (0x33)`。

数据通路：`sba_read/sba_write` 用的是**系统总线地址**，所以 RTT 控制块要放在
地址两侧一致的 RAM（本芯片 `flash_xip` 的 `.bss`/noncacheable 都落在
**AXI SRAM 0x01200000 起**，正合适；ILM/DLM 是核内地址 0x0/0x80000，
系统总线要走 0x01040000/0x01060000 别名，控制块里存的缓冲指针会对不上）。

### 踩过的三个 DTM 坑（都是"看起来像协议问题"的时序/编码问题）

1. **DTM 要求每次 DR 扫描之间插 Run-Test/Idle 周期**：`dtmcs.idle` 读出就是
   **7**。不插 idle 时 DTM 会**静默停摆**——响应冻结在 `op=3`，看起来跟
   "dmcontrol 写入被拒绝" 一模一样。实测 ≥4 拍即可，这里取 8。
2. **41 位 DR 里 `op` 在 bit[1:0]、data 在 bit[33:2]、addr 在 bit[40:34]**，
   响应里 addr 回显的是**上一个操作**的地址（流水线一拍深）。
3. `dmstatus` 在 `dmactive=0` 时读出来是 0；`dmcontrol.dmactive` 必须先写 1。

### 专用汇编：`JTAG_DP_GPIO_ASM_DMI.S`

把**一次完整 DMI 访问**（idle → Select-DR → Capture-DR → Shift-DR(41) →
Exit1-DR → Update-DR → RTI）收进一个函数，41 位请求直接在寄存器里逐位右移，
TDO 一拍流水采样。替代原来"6 次 `JTAG_Sequence` 调用 + C 侧打包 TDI 字节"
（那部分 C 开销约 1.25 µs，占一次访问的 30%）。

三个踩过的坑（都修了，都在注释里）：

| 现象 | 根因 |
|---|---|
| 响应 `op/addr` 正确但**读数据恒为 0** | bit40 的 TDI 被 `or` 上了导航用的预设 `t6`（里面**带了 TDI=1**），于是每个请求的地址都被或上 `0x40` → 落到非法寄存器，写全不生效 |
| 高位（bit32..40）恒为 0 | 8 位累加器是"每拍右移、新位进 bit31"，**只有正好 32 位**才自然对齐；8 位会停在 bit[31:24]，要 `srli 24` 挪回来 |
| 响应整体**晚一位**（`dmstatus` 读出 `0x00400CA2<<1`） | 高电平相位 nop 不够，TDO 采样点太早。`DMI_CAP_HIGH_NOP` 是编译期可调项，扫出来 **<8 就不行** |

时序可调项（CMake cache / 环境变量覆盖，见 `CMakeLists.txt`）：
`DMI_CAP_HIGH_NOP`（采样点）、`DMI_NAV_LOW_NOP` / `DMI_NAV_HIGH_NOP`
（导航时钟低/高电平）。**注意导航时钟的低电平不能省**：TMS/TDI 是在拉低的
同一次 `DO_VAL` 写里给出的，紧跟着拉高会让目标采到旧 TMS。

### 结果（`script_test/hpm6800_riscv.py`，1024 B × 50 轮）

| 实现 | 写 | 读 |
|---|---|---|
| 慢路径（C `jtag_seq` 拼 6 次调用） | 957.4 KB/s | 954.5 KB/s |
| 专用 DMI 汇编（cap=8/nav=8，初版） | 1195.3 KB/s | 1181.6 KB/s |
| **专用 DMI 汇编 + TDI 预置 + 循环展开（现行）** | **1511.8 KB/s** | **1504.5 KB/s** |
| 主机驱动 OpenOCD（最好后端） | 154.0 | 95.9 |

单字 SBA 读 25.6 µs（4 次 DMI 扫描）。现行配置相对初版 **+26.5%**，见下面
「TDI 预置」一节。

**教训**：一次 DMI 访问 54 个 TCK（41 移位 + 5 导航 + 8 idle），按 360 MHz 主频、
每 bit 约 23 周期算 ≈ 3.4 µs，和实测 3.4 µs/字吻合——所以**瓶颈就是每 bit 多少拍**，
不是协议也不是目标。有完整性自检脚本兜底：`script_test/hpm6800_selfcheck.py`
（探针写已知图案再读回比对校验和，PASS 才算数）—— 后面所有提速都是在它卡着做的。

### 提速：TDI 预置 + 循环展开（**1504.5 KB/s，+26.5%**）

#### 先把"每 bit 拍数"换算成 TCK 频率：16.4 MHz，而规格上限是 25 MHz

HPM6800EVK 侧 **JTAG TCK 最快 25 MHz**（器件规格）。按引擎的 TCK 开销反推：

```
一次 DMI 访问 = idle(8) + 导航(5) + 移位(41) = 54 TCK
1189.5 KB/s = 304,512 字/秒          (1024 B × 50 轮)
TCK = 304512 × 54 = 16.4 MHz
```

**16.4 MHz 只有上限的 66%** ⇒ 还有 1.5 倍空间没吃满，卡的完全是自己。
按 54 TCK/字算，25 MHz 下的理论上限是 **1.85 MB/s**。

#### 第一步（失败但有信息量）：只把簿记从低相位搬进高相位死等

低相位原本有 8 条指令专等 TDI 建立，高相位有 8 拍纯 nop 等 TDO 往返。把"整理
上一拍 TDO"的 4 条指令搬进死等，周期 18 → 14 拍：

| 版本 | 读 | 自检 |
|---|---|---|
| 原序（簿记在低相位） | 1189.5 KB/s | **PASS** |
| 重排 + cap=4 | **1384.6 KB/s** | FAIL，校验和 `0x11D9A168` |
| 重排 + cap=6 | 1281.9 KB/s | FAIL，**同一个** `0x11D9A168` |

两次不同 cap 拿到**逐字节相同的错误校验和** ⇒ 确定性的逻辑错、不是余量不足：
重排后 TCK 拉低到拉高之间只剩一条 `srli`（约 2 拍 ≈ 5 ns），**TDI 建立时间不够**，
目标每一位采到的都是上一位。**光搬簿记没用** —— 低相位那 8 拍是在等 TDI，不是在
等指令。

#### 第二步（成功）：把 TDI 摆到**上一位的高相位**去

关键是：TCK 已经是高的时候，把 `DO_VAL` 写成 `TCK|TDI` 只是改 TDI 电平、
**不产生任何边沿**。所以"给下一位摆 TDI"可以提前到上一位的高相位里做，
低相位就退化成一条 `sw DO_CLR`，只受目标**最小 TCK 低电平宽度**约束：

```
低相位:  sw t1, DO_CLR                       # 只拉低 TCK（TDI 早已稳定）  [+ DMI_TCK_LOW_NOP 拍]
高相位:  sw t1, DO_SET                       # 上升沿
         bexti/or/rori                       # 整理上一拍采样（在原死等里干活）
         srli / andi / slli / or / sw        # 摆下一位 TDI（TCK 保持高，无沿）
         lw                                  # 采样本拍 TDO
```

再加上**整个 32 位位移循环展开**（每条位省下 `addi`+`bnez` 两拍）。

| 阶段 | 配置 | 读 KB/s | 自检 |
|---|---|---|---|
| 初版 | cap=8 / nav=8/8 | 1189.5 | PASS |
| TDI 预置 + 展开 | cap=1 / tck_low=4 / nav=6 / 4 | **1504.5** | PASS（连测 3 次） |
| 参照：长跑 2048×64 ×3 | 同上 | 1505.4 / 1505.6 / 1505.8 | 复现性极好 |

**有效 TCK ≈ 20.7 MHz**（1504.5/1189.5 × 16.4），从上限的 66% 提到 **83%**。

#### 翻车记录：`andi t4, \in, JTDI_OFFSET` 不能照抄

45M 汇编里那句 `andi t4, a4, JTDI_OFFSET` 看着能把"取位 + 移位"合成一条，
但那只在 **TDI 流已经预先左移过 `JTDI_SHIFT`** 的代码里成立（那份的 `a4` 就是
那种约定）。这里的 `\in` 是原始 DMI 请求寄存器、bit0 才是当前位，套过去会去取
`bit[JTDI_SHIFT]` —— 于是整个请求被移成全 0（`op=NOP`），**症状极隐蔽**：

```
自检: got 0x00000000 want 0x08ECD0B4   -> FAIL（读回全 0）
基准: moved=51200 iters=50 rate=1324.7 KB/s -> "跑得通"、rderr/wderr 全 0
idcode=0x1000563D dtmcs=0x00007071     -> 还是对的（那条路走 8/8 导航的 C 路径）
```

基准能跑完、计数器全干净，只有**自检**抓得到 —— 这就是它必须当门禁的原因。

#### 四个时序旋钮的实测边界（**都已经在最小值**，别再减）

| 旋钮 | 现值 | 上界判据 |
|---|---|---|
| `DMI_NAV_LOW_NOP` | 6 | **4 → FAIL，读回全 0**（TMS 建立不够；TMS/TDI 与 TCK 拉低同一次 `DO_VAL` 写） |
| `DMI_NAV_HIGH_NOP` | 4 | 4 通过；配合 `NAV_LOW=8` 时的 4/4 曾 FAIL，说明卡的是 LOW 不是 HIGH |
| `DMI_TCK_LOW_NOP` | 4 | **2 → FAIL，校验和 `0x11D9A168`**（TCK 低电平太窄）；0 更快但同样 FAIL |
| `DMI_CAP_HIGH_NOP` | 1 | **0 → FAIL**（采样点不够，上升沿到 `lw` 需 ≥9 拍；TDI 预置后已有 8 条指令垫底，补 1 拍正好） |
| `idle`（运行时） | 8 | **6 只跑得动 6/50 轮就死**；≤4 立刻 `moved=0`（DMI 完全不应答） |

`DMI_BIT40_HIGH_NOP`（Exit1-DR 那一位）单独用 8：它跑在 `DMI_SHIFT_BIT` 之外，
序列里没有"顺带干活"的指令，采样延迟只能靠 nop 补，不能跟着 `CAP_HIGH` 一起降。

#### 还能再往上吗

要摸到 25 MHz 得再砍 ~30% 的拍数，剩下的空间在**导航相位**：一次访问 13 个
导航/idle 时钟 × 每个 ~10 拍 ≈ 130 拍，占总量约 15%。用同一招（把 TMS 也提前到
上一位的高相位摆好）可以把导航低相位也压到"只拉低 TCK"，理论上还能再要百分之十几。
但导航路径同时被 idle 循环复用、TMS 取值随状态变，改动比移位路径绕得多 ——
要做的话同样先用 `hpm6800_selfcheck.py` 卡死。



---

## 5. 阶段四：狂发例程与 RTT 交付率

### 5.1 板子不启动 —— 真因：**`.bin` 里没有启动头**（已解决）

第一次烧进去后 `reset halt` 读到 `PC = 0x2001d4c8`，跑 300 ms 不动 —— CPU 停在
**boot ROM**，从未跳进应用（`_start = 0x80003000`）。

`readelf`/`objcopy` 逐段对比后定位：

| 文件 | 0x80001000（boot header） | 结果 |
|---|---|---|
| `demo.elf` 的 `.boot_header` 段 | `bf109000…`（tag `0x009010BF`，非零）| 正常 |
| `objcopy -O binary` 出来的 `demo.bin` | **全 0** | **烧了就不启动** |

也就是说**这个 SDK 构建产出的 `.bin` 不含启动头**（用户那份 `work/` 里的
已知构建同样全 0，所以它大概也从没真正跑起来过）。改成烧 **ELF** 后：

```
flash write_image erase demo.elf      -> wrote 46712 bytes
mdw 0x80001000  -> 009010bf ...       启动头进 flash 了
reset halt ; reg pc -> 0x80003000     ROM 跳进 _start 了
resume; 500ms; halt
mdw 0x1240000 -> 47474553 52205245 00005454   "SEGGER RTT" —— 固件在跑
```

**结论：烧 HPM6800EVK 一律用 ELF，不要用 `.bin`。**
`script_test/hpm6800_flash_target.py` 已默认走 ELF，并把"复位后 PC"打出来自检。

### 5.2 狂发固件

`script_test/hpm6800evk_rtt_flood/`（`flash_xip`，RTT 上行缓冲 32 KB，
`BLOCK_IF_FIFO_FULL`，死循环发 `hello world!\n`），
`_SEGGER_RTT` 在 **0x01240000**（AXI SRAM，探针可直接读写）。

> 构建注意：**本地 SDK 的 `drivers/src/hpm_enet_drv.c` 被改过，无条件
> `#include "trace_log.h"`**，而该头只存在于 `samples/lwip/lwip_tcpecho/src/TRACE_LOG/`，
> 任何新工程都会因此编不过；本工程把该目录加进 include 路径绕过（没动 SDK）。

### 5.3 RTT 交付率 —— **已打通：1389 KB/s，字节级零丢包**

探针侧 RTT 桥的 RISC-V 后端（`rtt_bridge_set_target(1)`，HID CMD_RTT action 10；
`rtt_read_bytes` / RdOff 回写分别走 `riscv_jtag_read` / `riscv_jtag_write_word`）
加上之后，实测（`script_test/hpm6800_rtt_delivery.py COM5 5`）：

```
rc=0, cb=0x01240000, up=0x01240018
host read 7208960 bytes in 5.08s -> 1385.0 KB/s
bridge: drained=7211008 bytes, polls=3521, moves=3521, rderr=0, wderr=0
```

20 秒长跑 + 字节流校验（`script_test/hpm6800_rtt_loss.py COM5 20`）：

```
host received   28516352 bytes in 20.046s -> 1389.2 KB/s
probe drained   28516352 bytes (polls=13924 moves=13924 rderr=0 wderr=0 zips=0)
probe-host delta: 0 bytes
records=2193565 lost=0 dup=0 tail=7
stream check OK: pattern exact - 28516345 bytes, 0 lost, 0 duplicated
```

狂发固件写的是同一条 13 字节记录 `"hello world!\n"`，**丢一个字节模式必然错位**，
所以"2193565 条完整记录、0 丢 0 重"比计数更能说明问题：**28.5 MB 一个字节
不丢、不重**；`probe-host delta: 0` 说明连"探针搬了多少 vs 主机收了多少"都完全对上。
速率贴着探针侧 SBA 的 1.50 MB/s 天花板（§4，TDI 预置提速后），CDC/USB 那一跳不是瓶颈。

> ⚠️ **主机侧读法同样决定结果**（这一条一开始差点让我误判成"提速把链路搞不稳了"）。
> 用 pyserial 的 `ser.read(n)` 读：10 秒能过、20 秒就报 `stream diverges`，而探针
> `rderr/wderr` **全是 0** —— 因为 `read(n)` 每次新分配缓冲，实测把主机侧压到
> ~2169 KB/s，速率一高就在主机侧丢字节。改成 `readinto()` + 复用同一个 1 MB 缓冲，
> 并且**先停桥再收尾巴**（生产者停了以后还缺的才算真丢包），立刻干净：
> `probe-host delta: 0`。项目里所有测速脚本都是这个写法。

#### 真因：`rtt_write_word()` 对 RISC-V 的返回码极性反了

这是本轮唯一的功能性 bug，也是最难看出来的一类 —— 它不报错、不崩，只是**静默地
把成功当失败**。桥里两个后端的底层约定相反：

| 底层调用 | 成功 | 失败 |
|---|---|---|
| `swd_write_word()` | 1 | 0 |
| `riscv_jtag_write_word()` | 0 | 负 |

`rtt_write_word()` 的调用方一律按 **`0 = 失败`** 判：

```c
if (rtt_write_word(up + RDOFF, new_rd) == 0) { s_write_err++; s_rd_pending = ...; return -1; }
```

SWD 分支写对了（成功返回 `-1`），RISC-V 分支忘了取反。后果是一条**死循环**：
第一块 2048 B 搬完 → 回写 RdOff 其实成功 → 被判成失败 → 置 `s_rd_pending` 并
`return -1` → 下一轮先进"幂等补写"分支 → 又"成功当失败" → 再 `return -1`……
**从此再也不读控制块**，`drained` 永远冻在 2048、`write_err` 每轮 +1。

实测指纹（`script_test/hpm6800_rtt_diag.py`，用 HID PEEK 直接读探针 RAM）：

```
s_write_err     = 0x0D        <- 在涨
s_rd_pend_v     = 0           <- 但 pending 又总被清掉（写真的失败了才会清）
s_drained       = 0x800       <- 冻在第一块
uartrx.in/out   = 0x800/0x800 <- 注意：数据其实已经过 USB 走了
```

修法就是归一语义：`rtt_write_word()` 明确成 **0 = 成功 / -1 = 失败**（与
`rtt_read_bytes` 一致），两个后端各自取反，两个调用点同步改成 `!= 0` 判失败。

#### "主机读 0 字节"是**测量脚本自己的坑**，不是固件问题

`uartrx.in` 和 `out` 都是 `0x800`：2048 B 早就被 USB 取走了。原脚本的顺序是
`ACT_START` → `sleep(0.3)` → 读状态 → **才** `serial.Serial()` +
`reset_input_buffer()`，把桥推出来的唯一一块数据整个丢掉；而桥又因为上面的 bug
再没产出第二块，于是"读 0 字节"看起来像固件不发数据。现在读线程在 `ACT_START`
**之前**起来。

#### 顺带修掉的第二个坑：错误路径里调了 `swd_clear_errors()`

桥的错误恢复路径（`rtt_find_cb()` 重试、`rtt_bridge_poll()` 每 3 次失败后的重扫、
基准重试）都在调 DAPLink 的 `swd_clear_errors()`。在 JTAG 模式下这等于**把 SWD
引擎的时钟打在 JTAG 引脚上**，TAP 状态机当场被打散 —— 之后每一次 DMI 扫描都只能
读到 IDCODE（实测 `s_dbg` 四条全是 `0x…1000563D`），比不清还糟。
现在按目标分派：SWD 走 `swd_clear_errors()`，RISC-V 走新的
`riscv_jtag_clear_errors()`（SBCS 的 `sbbusyerror`/`sberror` 写 1 清零）。
同理 `rtt_clock_step_down()`（SWD 的降档阶梯）对 RISC-V 无意义，改成
`rtt_link_recover()`：只重走 `riscv_jtag_open()`（TAP 复位 + 重新加载 IR=DMI）。

> 教训：**跨后端的适配层必须显式归一"成功/失败"的方向**，而且要把可观测计数
> 打进状态字里 —— 这次就是靠 `s_write_err` 在涨、`s_rd_pend_v` 却是 0 这一对
> 互相矛盾的读数才定位到的。只加"重试"类健壮性补丁完全没用：四次构建的计数
> **逐字节相同**，因为失败点根本不在那些分支上。

#### 速率上限在哪

探针侧 SBA 块读 **1.50 MB/s**（§4，TDI 预置提速后）就是天花板，交付率 1389 KB/s
已经到它的 **92%**（剩下那 8% 是 CDC/USB 那一跳）。再往上只有继续抬 DMI 引擎：
一次 DMI 访问 = `idle(8) + 导航(5) + 41 bit = 54 TCK`，按 360 MHz 主频反推，
现在约 20.7 MHz TCK、目标规格上限 25 MHz。也就是说 RISC-V 侧现在**仍然是
CPU 周期受限**，不是协议受限 —— 剩下的余量在导航相位（见 §4 末尾）。


---

## 6. 复现清单

```bat
:: 1) 探针固件（HPM5301EVKLite）—— 时序旋钮已写成源文件默认值，一般不用覆盖；
::    要扫描才用 env 覆盖（见 script_test/hpm6800_timing_sweep.ps1）
cd firmware\application_5301
python ..\..\script_test\hpm6800_flash_probe.py          :: 构建 + DFU 升级
python ..\..\script_test\hpm6800_probe.py set-mode 1

:: 2) 目标调试（OpenOCD）
python script_test\hpm6800_flash_target.py               :: 用探针烧 HPM6800EVK

:: 3) 探针侧引擎
python script_test\hpm6800_riscv.py open
python script_test\hpm6800_selfcheck.py                  :: 完整性（PASS 才算数）
python script_test\hpm6800_riscv.py rbench 0x1200000 1024 50
python script_test\hpm6800_riscv.py wbench 0x1200000 1024 50   :: ⚠ 见下
 
:: ⚠ 写基准的地址就是目标自己的 RAM：0x1200000 是狂发固件 .bss 的起点，
::    wbench 会把目标正在用的变量/缓冲整片覆盖，目标随后就不产数据了
::    （现象：RTT 桥 poll 几万次全是空环、交付塌到 3 KB/s；读回校验和仍是对的，
::     所以只有速率会暴露它）。做完写基准确认要么换个空闲 scratch 地址，
::     要么按第 2 步重烧一次目标。

:: 4) 主机侧 SRAM 口径
python script_test\sram_speed_hpm6800.py --size 65536 --regions axi

:: 5) RTT 交付率（探针自己搬环，主机只读串口）
python script_test\hpm6800_rtt_delivery.py COM5 5     :: 速率
python script_test\hpm6800_rtt_loss.py COM5 20        :: 字节级丢包校验（模式必须精确复现）
python script_test\hpm6800_rtt_diag.py COM5 3         :: 出问题时读探针 RAM 定位
python script_test\hpm6800_cdc_check.py COM5          :: CDC 通路 / 环 vs 主机字节数对照

:: 6) 时序扫描（每点一次构建+烧写+自检，约 2 分钟）
powershell -File script_test\hpm6800_timing_sweep.ps1
```

> `hpm6800_rtt_diag.py` 里的符号地址是**按当前构建**从
> `build_dfu_evklite/output/akaLinkPro_App.asm` 的 `# <sym>` 注释里取的；
> 改了固件后要重新取（`s_drained` 之类是 `static`，map 里不一定有）。

### HID 协议补充

`CMD_RISCV = 0x33`（原 0x32，让位给 SCOPE）：`req[3]=action`（0 停、1 开、2 读基准、3 写基准、4 单字读基准、
5 读回校验、6 状态、7 时序、8 DMI 原始扫描），`req[4..7]=addr`、`req[8..11]=arg1`、
`req[12..13]=arg2`；响应 12 个 32 位状态字：
`[0] open|pending<<8|rc<<16|action<<24`、`[1] idcode`、`[2] dtmcs`、`[3] dmstatus`、
`[4] moved`、`[5] ticks(24MHz)`、`[6] sbcs`、`[7] delay|iters<<8`、
`[8] bytes/s`、`[9] 校验和`、`[10..11] 前两个字`。
**注意 STATUS/CONFIG 不入队**（队列只有一个槽，排队会把在跑的操作冲掉）。
