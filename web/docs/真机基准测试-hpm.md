# 真机场景基准 · HPM6800EVK（2026-10 定稿，RISC-V/JTAG + akaLinkPro）

跟 [`真机基准测试.md`](真机基准测试.md)（STM32F103ZE 那份）**同一套口径**，搬到 RISC-V 板子上。
一条命令跑完并逐项判决：

```powershell
make hw-campaign-hpm                                # 2 轮全场景 + flood↔scope 交替 5 遍（约 6 分钟）
make hw-campaign-hpm ARGS="--cycles=1 --alt=1"      # 冒烟
make hw-campaign-hpm ARGS=--record                  # 只记录不判决，末尾打印"实测 × 80%"的 spec 建议
make hw-campaign-hpm ARGS=--keep-going              # 出错也跑完（长稳观察）
```

脚本：`tools/selftest/hw-campaign-hpm.mjs`。原始数据落 `tmp/hpm-campaign-result.json`，
每轮的 10 s 录音导出到 `tmp/hpm-forward-<时间戳>.bin`。
跑完屏幕上会自动打一张**小结表**（`tools/selftest/campaign-summary.mjs`，F103 那份基准共用同一实现）；
事后想重看不用碰硬件：`make campaign-summary`（默认读 HPM 那份，`ARGS=tmp/campaign-result.json` 读 F103 那份）。

| 步骤 | 判决（spec） |
|---|---|
| ① 烧 **flood 固件**（RTT 在非缓存 AXI SRAM） | ≤ 12 s |
| 　 RTT Viewer（**RISC-V 新通路**，地址取自 ELF 的 `_SEGGER_RTT`） | **> 56.5 KB/s** · 零错位读 |
| 　 RTT 转发（探针侧桥 → CDC） | **> 1.10 MB/s** |
| 　 转发 **10 s 存盘**（页面「记录到文件」） | 文件字节 = 同窗口收数（≥98%）· 内容可读 · 无积压 |
| ② 烧 **scope 固件** → J-Scope：1 变量 / 3 变量（@2 µs 冲上限、@20 µs = 50 kHz 档） | ≥ 206.6 kHz / ≥ 32.6 kHz；50 kHz 档**零丢样本** |
| ③ ①② 重复 2 遍　④ flood↔scope 交替烧录 5 遍 | 逐次计时 |

**spec 怎么定的**（用户口径：先跑一遍，再拿跑出来的值定线）：

- **速率类 = 首跑实测均值 × 80%**（转发 1.377 × 0.8 = 1.101 MB/s、Viewer 70.6 × 0.8 = 56.5 KB/s、
  J-Scope 258.3 × 0.8 = 206.6 kHz / 40.8 × 0.8 = 32.6 kHz）。
- **耗时类 = 实测最坏值 + 15%**（12 s）。**不取 80%**：同一份固件同一根线实测 8.4~10.5 s，
  "80% 的耗时"等于要求它比最坏情况还快 20%，那是宿主调度抖动、不是能力；
  12 s 这条线照样能抓住真正的回归（当年后台页被限速那次是 17 s → 47 s）。
- **正确性类钉死**：存盘一致性 ≥ 98%（实测 99.6~100%）、错位读 = 0、50 kHz 档探针丢 0 / USB 丢 0。

## 结论速览（判决 **20 通过 / 0 失败**）

| 项目 | 实测（两轮） | 交替 5 遍 |
|---|---|---|
| 烧录 flood（45.6 KB，JTAG + 校验 + 复位） | **9.9 / 10.4 s** | 8.4/8.5/8.7/8.5/8.5 s（均 8.53） |
| 烧录 scope（45.1 KB） | **10.2 / 9.0 s** | 8.5×5（均 8.53） |
| RTT Viewer（RISC-V/JTAG，SBA 读法） | **60.0 / 63.9 KB/s**（轮询 1.9~2.2 Hz） | — |
| RTT 转发（主机侧，页面 RX 计数） | **1.376 / 1.377 MB/s** | — |
| RTT 转发（探针侧"已搬运"） | 1.204 / 1.207 MB/s · 档位 4 | — |
| 转发 10.2 s 存盘 | 13.93 / 13.95 MB，一致性 **99.6 / 99.7%**，积压 48~56 KB（write 68 次） | — |
| J-Scope 1 变量 @2 µs（`g_v.tick`，1 span/4 B） | **259.5 / 260.9 kHz** | — |
| J-Scope 3 变量 @2 µs（`g_v.tick/u_hi/f_sin`，1 span/12 B） | **40.7 / 40.8 kHz** | — |
| J-Scope @20 µs（50 kHz 档） | 1 变量 50.00 kHz，3 变量 41.3 kHz；**探针丢 0 / USB 丢 0**（缺口固定 6） | — |

## ① RTT Viewer 的 RISC-V 通路（2026-10 新增，零安装）

同一个"零安装"思路，但底层换了一套：

```
HID（0xFF00）切 output_mode = SWD+JTAG，
并让探针自己的 RISC-V 引擎（0x33 action 0）与 RTT 桥（0x31 action 0）放开 TAP
        ↓
WebUSB 认领 interface 0（open 必须 skipTargetInit：默认那套按 SWD 协商，RISC-V 上必 NO ACK）
        ↓
DAP_Connect(JTAG) → DAP_JTAG_Configure(IR=5) → DAP_JTAG_Sequence
        ↓
RISC-V DMI（IR=0x11，41 位 DR）→ DM 唤醒 → SBA（系统总线）读内存
```

代码：`app/rtt/riscv-mem.js`（对上层只暴露 `readMem/writeMem`）+ 复用烧录页那套
`app/flash/hpm/{dap-transport,riscv-dm}.js`。`Rtt`（`app/rtt/protocol.js`）本来就只依赖这两个方法，
所以定位/轮询/下行/记录**一行都没改**。

页面上：RTT Viewer 的「目标类型」选 **RISC-V / JTAG**，芯片那格会换成 HPM 列表
（`#r-rv-chip`，10 块板 + 「其它 RISC-V」），RAM 范围按系列自动带出 —— 默认窗口是从各系列 SDK
链接脚本的 `AXI_SRAM_NONCACHEABLE` 推的（HPM6800 → **0x01240000**，本机固件 `_SEGGER_RTT` 实测就在那儿）。
最稳的用法是「载入 ELF…」用符号把地址填死。

### 为什么只有 60 KB/s（而 F103 那条有 616 KB/s）

**瓶颈是 USB 往返，不是 JTAG 时钟**。真机实测（`tmp/riscv-read-bench.mjs`）：

| TCK（DAP_SWJ_Clock） | 读 256 B | 内容 |
|---|---|---|
| 1 / 5 / 10 / 20 / 30 / 45 / 60 MHz | 39.8~43.9 ms（**几乎一样**） | 全部正确 |

逐字读时每个字要两次 DMI 扫描 = 两条 CMSIS-DAP 命令 ≈ 2 × 0.28 ms ⇒ 只有 **5.7 KB/s**。
一条 `DAP_JTAG_Sequence` 本来就能装几十拍，于是把**一批 12 个字（24 拍）压进一条命令**
（请求 433 B / 响应 144 B，都在 512 B 包内）⇒ 512 个字从 ~1024 条命令降到 **69 条**，
实测 6.3 → **59 KB/s**。这就是现在 Viewer 那 60~65 KB/s 的来源。

🚨 批量路径**每拍都收状态**（`READ,NOP,READ,NOP…`），不是"投一批再收"：DMI 流水线只有一级深，
丢一拍不会报错，只会让后面所有字**整体错位一个**（自增在硬件里推进）。任何一拍非 SUCCESS 就从那个字起
退回逐字，并**把 `sbaddress0` 写回去对齐**；连撞 3 次就整段不再批（批不动不算错误，逐字照样读全）。
离线自测见 `tools/selftest/hpm-flash.test.mjs` 第 8 节（位序/自增、批次接缝、非对齐、512 B 包预算、
被拒时退回逐字）。

### 三个真机踩到的坑（都写进代码注释了）

1. **通道名指向 XIP flash → SBA 永久挂起 → 整条链路死**。
   RTT 控制块 up 通道的 `sName = 0x8000cf1c`（字符串在 flash 窗口），SBA 读那个窗口事务永不完成、
   `sbbusy` 不落；**挂起之后连 SRAM 都读不出来**。现象不是"这个名字读不到"，而是
   "Viewer 连上以后一个字节都不来"（`Rtt.name()` 永不返回 → 界面一直停在「正在查找 RTT 控制块…」）。
   现在：`name()` 带 1.2 s 超时（名字只是显示用，读不到就当没有）；`RiscvMem` 单字超时压到 700 ms，
   失败就按 `riscv-dm.js` 记下的解药**复位 DM**（`dmcontrol` 先 0 再 1）后重试一次。
2. **控制块地址要自己给**：HPM 的结构体在 AXI SRAM（本机 `_SEGGER_RTT = 0x01240000`），
   "从 0x20000000 起自动搜"是搜不到的，还会把读错数拉高。基准脚本直接从 ELF 符号表取。
3. **必须是非缓存内存**：探针的 SBA 读**不旁路 D-Cache**，放可缓存区读到的是陈旧值。
   scope 固件故意放了 `g_v`（非缓存）/ `g_v_cached`（可缓存、不写回）两份做对照 —— 基准脚本
   硬排除 `*_cached`，否则现象是"波形一条平线，速率却正常"。

## ② J-Scope（探针侧 HSS 采样）

变量取 scope 固件契约块 `g_v`（非缓存 AXI SRAM）里的成员，1 变量 = `g_v.tick`，
3 变量 = `g_v.tick/u_hi/f_sin`（结构体里相邻 ⇒ 合并成 1 个 span/12 B）。
@2 µs 冲到上限（1 变量 ~260 kHz、3 变量 ~41 kHz），@20 µs 的 50 kHz 档：1 变量正好 50.00 kHz、
3 变量 41.3 kHz（3 个变量一个 span 的物理上限），**探针与 USB 都是零丢样本**；
"缺口 6" 是**起跑边界**，不是丢包 —— 单独验过（`tmp/hpm-scope-gap.mjs`）：跑 4 s 是 6，跑 12 s 还是 6。

## ③ 前置与固件

- flood 固件：`tools/target-firmware/hpm6800evk_rtt_flood/`（`fw.elf` 已入库，45.6 KB 数据段）；
- scope 固件：`tools/target-firmware/hpm6800evk_scope/`（契约变量块 `g_v` + 对照 `g_v_cached`）；
- 两块都把 RTT/变量放在**非缓存 AXI SRAM**，且都在 96 MHz（HPM 的 CPU 时钟由 SDK 初始化）。
- 串口授权、探针别被别的页签占着 —— 与 [F103 那份的"三条前置"](真机基准测试.md) 相同。
