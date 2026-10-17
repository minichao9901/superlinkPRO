# 栈帧与局部变量：固件、Web 压测、GDB 对照

本次增加的是测试基础设施。akaLinkPro 探针固件和 RTT/JScope 的运行代码不变。
F103 与 H743 使用共享的 `tools/target-firmware/common/dbg_frames.c`，主循环新增第12阶段。
原有11个阶段保留，既有单步、断点、结构体与 DWT 验收仍可单独执行。

## 测试契约

固定输入，不比较 g_loops、SysTick 或中断计数。7个全局检查点符号指向准确的 NOP 指令：

| 检查点 | 核对内容 |
|---|---|
| recursive | 5层同名递归帧；depth_copy 为0..4，seed_copy 为1028、1021、1014、1007、1000 |
| leaf | 参数、leaf_value、局部数组 items、record 结构体中的有符号数和嵌套数组 |
| shadow | 外层 shadow=60、内层 shadow=70，两者按词法作用域分别读取 |
| shadow-exit | 内层变量消失，外层变量保持可见 |
| register | arg=23、register_value=70，检查寄存器位置 |
| before-call | arg=70、stack_value=79，参数位于寄存器 |
| after-call | arg=70 移至栈、result=346；位置迁移不能产生旧值 |

GDB 先独立读取板上的 Flash，与 ELF 的只读 Flash 段逐字节匹配，之后采集各检查点。
对照文件包含 ELF SHA-256、构建信息、GDB版本、共享测试代码 SHA-256、各帧函数/PC/SP、参数/局部变量、
可用状态、复合字段和内存位置。核心必测变量缺失、递归层数不够、固定输入答案错误，
或构建没有真正覆盖寄存器到栈的位置迁移，采集均失败。不会生成一个“部分覆盖”的合格答案。
变量从 GDB 各活动词法块按 Symbol 读取，不能用按名字求值把内外层同名变量都读成内层。

Web 侧重新验证板上 Flash；逐帧与 GDB 比较，正向/反向切帧，检查源码位置和选中按钮。
每次回溯/切帧前后比较 R0..R15、MSP/PSP/xPSR/CONTROL/PRIMASK/BASEPRI/FAULTMASK 和当前栈起点最多512字节，检查只读性。
默认200轮重复递归暂停，检查值、调用链、FPB槽位和故障计数。
写寄存器、写内存、单步、继续、重载 ELF、复位、断开后验证旧缓存失效。
仅两边都不可用的变量可以作为“不可用状态一致”，不能用它充当核心变量覆盖。
任何差异均报告失败。该专用验收关闭旧套件的 FAULT 重试包装，并拒绝意外复位/恢复记录。

## F103CB 基线流程（Windows，无需管理员）

以下命令从仓库根目录执行。需要 PATH 中的 PowerShell、ARM GCC、支持 Python 的 ARM GDB、
已有的 OpenOCD/GDB server，以及原有浏览器/CDP准备环境。
脚本不自动烧录，不替用户启动或终止其他工具，也不会杀掉未知进程。

1. 构建新测试固件：

```powershell
make build-dbg-frames-f103cb
```

输出：`tools/target-firmware/stm32f103_dbgstress/build-cb-og/fw.elf`、fw.bin、fw.hex、build-info.json。
默认 `-Og -g3 -gdwarf-4 -fasynchronous-unwind-tables`，所有输出独立于原 `-Os` 目录。
把这个目录里的新固件烧进板子；不要使用旧 `fw.elf` 快照或原来的 build-cb 产物。

2. 断开网页对探针的占用，启动适合板子的 GDB server，然后采集标准答案：

```powershell
make dbg-frame-oracle FRAME_REMOTE=127.0.0.1:3333
```

默认调用 arm-none-eabi-gdb。路径不同可传 `FRAME_GDB=...`。
F103ZE 的 BOOT0唤醒采用原有 VTOR/SP/PC配方；CB/H743采用正常复位。
采集失败会清除旧对照文件，3分钟超时会终止此次 GDB 客户端。
默认输出 `tmp/frame-oracle-f103cb.json`。

3. 退出 GDB server，释放探针，再运行 Web 验收：

```powershell
make test-dbg-frames
```

该入口要求有效对照文件，缺失或 ELF/构建参数不匹配时，在连接浏览器/探针之前失败。
默认200轮，可传 `FRAME_ROUNDS=500`。结果写入 `tmp/dbg-stress-page-f103cb.json`，
日志或报告中的任一失败项都会使退出码为1。常规综合 stress 仍使用原入口，另行运行。
两种客户端按顺序使用同一探针，不得同时连接。

## 优化与板型矩阵

```powershell
# F103CB DWARF5 + Og
pwsh -File tools/target-firmware/stm32f103_dbgstress/build.ps1 -Board cb -Optimization Og -Dwarf5
# F103CB DWARF4 + Os，保留旧默认构建目录
pwsh -File tools/target-firmware/stm32f103_dbgstress/build.ps1 -Board cb -Optimization Os
# H743 DWARF4 + Og
pwsh -File tools/target-firmware/stm32h743_dbgstress/build.ps1 -Optimization Og
```

每个构建都要重新烧录、重新采集 GDB 对照、重新运行 Web 压力验收。
通过 `FRAME_BOARD`、`FRAME_ELF`、`FRAME_ORACLE` 显式选择，不能混用：

| 板型/配置 | FRAME_ELF |
|---|---|
| F103CB Og/DWARF4 | /tools/target-firmware/stm32f103_dbgstress/build-cb-og/fw.elf |
| F103CB Og/DWARF5 | /tools/target-firmware/stm32f103_dbgstress/build-cb-dw5-og/fw.elf |
| F103CB Os/DWARF4 | /tools/target-firmware/stm32f103_dbgstress/build-cb/fw.elf |
| H743 Og/DWARF4 | /tools/target-firmware/stm32h743_dbgstress/build-og/fw.elf |
| H743 Og/DWARF5 | /tools/target-firmware/stm32h743_dbgstress/build-dw5-og/fw.elf |

高优化构建也采用严格对照。如果 GDB 能恢复而 Web 不支持，则验收失败并显示对应变量；
不自动豁免 entry_value、分片位置等当前尚未支持的表达式。先定位并修复，或明确调整发布范围。

## Cortex-M7 同步异常与断点误停

H743 的 Cortex-M7 可能触发 Arm erratum 3092511：异步异常与硬件断点同时发生时，调试器报告的
PC 会落在异常处理入口，而断点地址保存在异常栈帧中。此前 H743 Og/DWARF5 的递归压力因此偶发
显示 `SysTick_Handler`，不能据此当作栈帧值错误，也不能无条件重试。

压力脚本仅对 H743/Cortex-M7 做严格确认：当前 PC 必须在 `SysTick_Handler`；DFSR 必须有 BKPT 位，
DWT 不能报告观察点命中；用户断点及活动 FPB 比较器必须指向本轮检查点；当前异常号必须为 SysTick；
异常栈帧必须是线程态、Thumb 状态，且保存的 PC 与检查点完全一致。全部吻合后才恢复运行并等待
精确 FPB 停点，每个检查点最多恢复4次；任何证据缺失、地址不符或重复误停都仍然失败。
Arm 的 errata notice：[Cortex-M7 Software Developer Errata Notice，3092511](https://documentation-service.arm.com/static/665dff778ad83c4754308908)。

## 已执行验证

2026-10-06（Asia/Shanghai），H743 实板，FPB 8 个比较器/rev2：

| 配置 | 结果 | 说明 |
|---|---:|---|
| Os / DWARF4 | 19 通过 / 0 失败 | 7 个 GDB 检查点及 200 轮压力通过（05:55，SWD 5 MHz；首次 10 MHz 尝试在第80轮发生 WebUSB transferOut 错误，完整重跑通过） |
| Os / DWARF5 | 19 通过 / 0 失败 | 7 个 GDB 检查点及 200 轮压力通过（06:12，SWD 5 MHz；此前 20 轮通过于 02:59） |
| Og / DWARF4 | 19 通过 / 0 失败 | 7 个 GDB 检查点及 200 轮压力通过（06:03，SWD 5 MHz） |
| Og / DWARF5 | 19 通过 / 0 失败 | 03:25 完成200轮、03:30 用最终 DWTTRAP 判据再跑20轮（SWD 10 MHz）；06:48 在 SWD 5 MHz 完成200轮同频复测。各轮中均有1次证据完整的 M7 误停恢复 |

Os/DWARF4、Os/DWARF5 与 Og/DWARF4 的 200 轮结果均逐项比较 Web 局部变量和 GDB 对照，检查调用者易失寄存器、递归同名变量、位置迁移、作用域/优化标记、寄存器和栈只读性、FPB 比较器无泄漏，以及写操作、单步、继续、重载 ELF、复位和断开后的旧帧/缓存清理。三组均为变量差异0，无意外复位、无隐藏传输重试。测试使用匹配固件和 oracle：Os/DWARF4 ELF SHA-256 为 `4c5205678f420bde77906aa6bf5a208a0f00858e7aa9b7e8cbcdffb9081f7f37`，Os/DWARF5 为 `c09ec44878298c72b915b624fe93de0af72245a76e1d50422782afb1e75292f6`，Og/DWARF4 为 `18172a408ed01865794af2eae02d2643165bd84d06ac43a08af4d480f22f52f4`；原始报告分别为 `tmp/dbg-frame-h743-os-dw4-200-5mhz.json`、`tmp/dbg-frame-h743-os-dw5-200-5mhz.json` 与 `tmp/dbg-frame-h743-og-dw4-200-5mhz.json`。首次 Os/DWARF4 的 10 MHz 尝试在第80轮遇到2次 WebUSB `transferOut` 网络错误，因此不计通过；确认目标仍为 H743 后降到 5 MHz 完整重跑。三组完整 200 轮测试都在 5 MHz 完成。

Og/DWARF5 的早期 20 轮复测曾在一个检查点误停到 `SysTick_Handler`，PC 为 `0x080000f4`，
目标检查点为 `0x08000a3a`。加入上述证据核验后，H743 实板完整 200 轮通过，变量差异为0，
寄存器/栈未被回溯修改，FPB 无泄漏，写操作/单步/继续/重载 ELF/复位/断开后的旧缓存检查通过，
也没有意外复位或隐藏传输重试。此次执行使用 Og/DWARF5 ELF SHA-256
`c559f42988cfc43359ed07e897a895816dcf2b5b9c3d36b1647b0b9baee50153`，报告为
`tmp/dbg-stress-page.json`，GDB oracle 为 `tmp/frame-oracle-h743-og-dw5.json`。
最终 DWTTRAP 判据对应的20轮复归报告是 `tmp/dbg-frames-h743-final-guard.json`。
递归压力中的 M7 误停恢复次数保存在报告 `frameResults[0].pressureM7ErratumRecoveries` 字段；两次均为1次，
保存的事件 PC 是 `SysTick_Handler`，DFSR.BKPT、目标 FPB 和异常栈保存的检查点地址均匹配。

2026-10-06 06:48:48（北京时间）再次用 H743 `Og / DWARF5` 固件和匹配的 GDB oracle，在 SWD 5 MHz 下完成严格 200 轮复测：7 个检查点逐项对照，19 通过 / 0 失败；递归压力中的一次 M7 误停满足异常栈 PC、DFSR.BKPT 和活动 FPB 证据后恢复。变量差异为0，回溯未修改目标寄存器或栈，无比较器泄漏、意外复位或隐藏传输重试。ELF SHA-256 仍为 `c559f42988cfc43359ed07e897a895816dcf2b5b9c3d36b1647b0b9baee50153`，原始报告为 `tmp/dbg-frame-h743-og-dw5-200-5mhz-audit.json`。至此 H743 的 Og/Os × DWARF4/5 四种组合均有匹配 oracle 的 200 轮硬件验收；本次结束后已恢复并读回校验标准 Os/DWARF4 固件。

## 2026-10-06 补充：F103CB ARM 构建矩阵

2026-10-06 约 05:19（北京时间），用 `stm32f103_dbgstress/build.ps1` 为 F103CB 构建了四种优化级别与 DWARF 组合。四个构建均成功，`engine_frame_leaf`、`engine_frame_recursive`、`engine_frame_migrate`、`engine_frame_register` 等检查点符号齐全；Og 镜像 text/data/bss 为 10016/100/784 B，Os 为 9720/100/784 B，均适配 F103CB 容量。

| 配置 | 构建目录 | ELF SHA-256 | ELF / BIN 大小 |
|---|---|---|---:|
| Og / DWARF4 | `build-cb-og` | `7d1c9593df06e1348c90cb4c9a9df82369c2437d1a834f2fd8fca9932b2a70e1` | 187952 / 10116 B |
| Os / DWARF4 | `build-cb` | `e6054e6572a43f55640cbaadcd219e3366a543323f2aaf0175969cae62c76b9f` | 189280 / 9820 B |
| Og / DWARF5 | `build-cb-dw5-og` | `4897e19e3131a84ffeddfd031dd6b41947849ed803592fcfe0628229a5bf7d17` | 185912 / 10116 B |
| Os / DWARF5 | `build-cb-dw5` | `6780a13c3770bc1d0d70559bc7285dfa7d0a09996c02988167082889dd874cad` | 186924 / 9820 B |

这只是 ARM 交叉编译和 ELF/符号检查，不算 F103CB 板上验收。本轮接着的是 H743；板卡 ID 检查确认芯片为 H743 后，F103CB 烧录流程按预期拒绝继续，因此没有把 CB 固件写进 H743。F103CB Og/DWARF4 的实板流程仍需在 CB 接回后依次烧录该目录镜像、采集匹配 ELF 的 GDB oracle，再运行严格 Web 验收。

## 2026-10-06 补充：HPM6800EVK 的 RISC-V 栈帧验证边界

2026-10-06 06:56–07:02（北京时间），HPM6800EVK 的 `make full_flow_6800evk` 完成；固定 RISC-V 调试压力 57/0，随机切换中的调试阶段 53/0。固定流程覆盖源码定位、断点、单步、复位、内存读写和 40 轮停—走—停；另有 RISC-V GDB 指令单步/行断点对照。详细数据见[真机测试记录](validation/2026-10-05-hardware-test-results.md)。

在该次基础测试时，这不等价于本文件中 H743 的栈帧验收：`app/dbg/backtrace.js` 对非 ARM 架构明确返回“仅支持 Cortex-M DWARF CFI / EHABI”，RISC-V 的 `bt` 只能用 `bt scan` 查看候选地址。那时 RISC-V 尚未验证可靠 CFI 展开、递归栈帧局部变量和优化位置迁移，也未构建/验收 Og/Os × DWARF4/5 矩阵。后续实现进度和当前验收边界见紧随其后的收工记录；不得把基本调试器压力结果或合成测试外推为硬件矩阵通过。

## 2026-10-06 补充：RISC-V CFI 实现进度与收工边界

约 07:34（北京时间）继续实现了 RISC-V CFI/局部变量路径：回溯按 x0–x31 与独立 PC 列解析 DWARF CFI，CFA 使用 x2/SP，返回列使用 x1/RA；仅传递 ABI 保留寄存器，位置表达式支持扩展寄存器 `DW_OP_regx` / `DW_OP_bregx`。合成 ELF/会话测试覆盖 CFI 恢复、两帧展开、寄存器有效性与局部变量诊断；`node tools/selftest/dbg-frame-riscv.test.mjs` 的合成部分通过，已有 ARM CFI/EHABI 与局部变量测试也通过。

HPM 调试固件现在包含通用帧检查点，并可隔离构建 `Og/Os × DWARF4/5`。本次只成功构建了 `Og / DWARF4`，ELF SHA-256 为 `54c380cceb7954a201e237c5fc647fc49f5b4947999de1927ab2f01b598a8723`；没有烧录该镜像，也没有采集对应 GDB oracle 或执行任何 HPM 栈帧硬件轮次。用此 ELF 做真实 `.debug_frame` 集成检查时，`dbg_frame_recursive_checkpoint` 未能解析出 CFI 行，说明当前实机 ELF 覆盖仍有未解决问题；不能将合成测试结果外推为 HPM 支持完成。

为了使 `make test-offline` 不受本地是否残留某个 HPM 构建产物影响，真实 ELF 检查现需显式设置 `HPM_DBG_FRAME_ELF=<路径>`；设置后任何缺符号或无 CFI 行仍会使测试失败。剩余 Og/Os × DWARF4/5 构建、真实 ELF 解析修复、四组 GDB oracle、HPM 实板压力和完整 RISC-V 局部变量验收均待后续完成。最近一次 HPM 真机测试仍是上文 06:56–07:02 的基础 `full_flow_6800evk`。

## 离线检查及验证边界

- `make test-dbg-features`：真实既有 ARM ELF解析、变量/帧命令、对照契约和错误答案检测；浏览器/会话替身还检查压力流程、
  只读性、错误目标拒绝和失败收尾，不冒充硬件验证。
- `make test-dbg-frame-native`：需要本机 gcc，使用共享C代码，Og/Os 各运行10000轮；
  仅验证固定输入和编译警告，不代表 Cortex-M编译或板上性能。
- `make test-dbg-frame-gdb`：Python GDB API替身，覆盖复合字段、同名作用域、不可用值和帧边界；
  不是实际 ARM GDB对照。

本次本地也通过 `node tools/selftest/dbg-frame-contract.test.mjs` 和
`node tools/selftest/dbg-frame-hw-runner.test.mjs`；后者覆盖 M7 误停证据完整时恢复，
以及异常栈 PC 或 DFSR 证据不完整时拒绝恢复。H743 已完成本节所列板上验收；F103CB 的四种
ARM 构建已完成，但仍需在对应实板上采集 GDB oracle 并实测。其他板型也必须使用各自新构建的
ELF、匹配的 oracle 和板上运行结果。

GDB API依据：
[Frames](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html)、
[Blocks](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Blocks-In-Python.html)、
[Values](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html)。

## 2026-10-06 收工记录：HPM RISC-V `.eh_frame` 与板上矩阵进度

约 07:40–08:00（北京时间）继续处理 HPM6800EVK 的真实固件 CFI。GNU RISC-V 工具链生成的应用帧信息位于 `.eh_frame`，使用 `zR` augmentation 和 PC-relative `sdata4` FDE 地址；先前只读 `.debug_frame` 因而无法覆盖应用检查点。现在解析器在 RISC-V 寄存器上限显式设为 32 时，优先沿用 `.debug_frame`，找不到时再解析 `.eh_frame` 的 `zR`、`udata4`/`sdata4` 以及 `pcrel`/绝对地址形式。新增合成 pcrel `.eh_frame` 用例。

HPM6800EVK 的 `Og/Os × DWARF4/5` 四种调试 ELF 均构建成功，并分别通过真实 ELF 的七个检查点 CFI 行解析检查；新增 RISC-V 合成展开/局部变量测试与既有 ARM CFI/EHABI 测试通过，`make test-offline` 退出码为 0。构建矩阵如下：

| 配置 | ELF SHA-256 |
|---|---|
| Og / DWARF4 | `54c380cceb7954a201e237c5fc647fc49f5b4947999de1927ab2f01b598a8723` |
| Os / DWARF4 | `85ba8c0e876f04ae9b5228ea23d5fd61e09cf886becaa8c2766ce7d0a14c2a25` |
| Og / DWARF5 | `160b9633434c58dfbd94d5e1fb4fdd3cb06b07297f187f0803742ceb7b4f555a` |
| Os / DWARF5 | `8fdc0a4074b768ea12e35b0711c66cbbb8faf4faaba292d094266c226b14ef80` |

本轮只把 Og/DWARF4 固件烧入 HPM6800EVK，并逐字节读回验证 44,488/44,488 B。由于收工时停止了板上矩阵，尚未为这四个精确 ELF 分别采集匹配的 GDB oracle，也未运行 Web 侧 200 轮局部变量/帧压力；不得将真实 ELF 静态 CFI 检查记作板上栈帧验收。此前 07:46–07:53 的 `make full_flow_6800evk` 已完成，独立记录见[真机测试结果](validation/2026-10-05-hardware-test-results.md)。

### 2026-10-06 Og/DWARF4 首组实板验收进度

- **时间与配置：**北京时间约 09:07–09:12，HPM6800EVK，Og/DWARF4；ELF SHA-256 为 `54c380cceb7954a201e237c5fc647fc49f5b4947999de1927ab2f01b598a8723`。重新烧录并逐字节校验 44,488/44,488 B。
- **GDB oracle：**使用带 Python 3.10 的 WCH RISC-V GDB，七个检查点全部采集成功；ELF 代码逐字节核对通过，必测变量与固定输入契约通过，`arg` 从 `$a0` 寄存器迁移到栈的 DWARF 位置变化也得到确认。oracle 位于本地 `tmp/frame-oracle-6800evk-og-dw4.json`。
- **网页端压力：**首轮因 RISC-V 寄存器名未纳入位置契约、测试 runner 缺少 `join` 导入及 PC 有符号表示问题而未进入有效轮次；修复后七个检查点逐帧对照全部通过。200 轮压力运行至 80 轮时仍无变量差异、寄存器/栈改写或触发器泄漏，随后 WebUSB `transferIn/transferOut` 报传输错误并中断。该次不计通过，也未完成 200 轮。
- **探针状态：**中断后浏览器 USB/HID 设备枚举中已无 akaLink；Windows 将相应端口列为“未知 USB 设备（端口重置失败）”。执行 `pnputil /scan-devices` 后故障仍在，无法继续板测。当前 Og/DWARF4 状态为**GDB 对照通过、网页 80/200 后因探针 USB 断连而未通过验收**；需重新插拔 probe 并重新认板后再重跑完整 200 轮。原始失败终端输出在本次任务记录中，半途报告文件不可作为成功结果。

本次修复已通过 `node tools/selftest/dbg-frame-contract.test.mjs`。四配置矩阵尚未完成：仅 Og/DWARF4 完成 GDB oracle 和部分网页轮次，Os/DWARF4、Og/DWARF5、Os/DWARF5 仍待实板验收。

### 2026-10-06 09:21–09:25 HPM full flow 重试与矩阵预检

- **Full flow：**执行 `make full_flow_6800evk`，流程到 `board-check-6800evk` 即停止；未进入 `hw-campaign-hpm`、调试固件烧录或随机顺序压力。浏览器 `navigator.usb.getDevices()` 返回空，Windows 仍枚举到 `USB\VID_0000&PID_0001`“未知 USB 设备（端口重置失败）”。因此本次没有烧录，也不计 full flow 通过。
- **认板脚本：**首次失败暴露了正式脚本直接调用 `readIdcode()` 的用户手势问题。已改为在 `#flash` 页通过 CDP 鼠标输入实际点击“读 IDCODE”，并只从授权列表选择 akaLink/DAP。无 probe 时用 `IDCODE_WAIT_MS=3500 make board-check-6800evk` 复核，确认不再出现 Chromium 的“Must be handling a user gesture”错误；实际读数因 USB 设备缺失而超时，现如实判为“没读到 IDCODE”，不会误报成另一种板卡。该空载检查不是硬件通过。
- **矩阵预检：**Og/DWARF4、Os/DWARF4、Og/DWARF5、Os/DWARF5 的 ELF 均与各自 `build-info.json` 的 HPM6800EVK 标识和 SHA-256 相符；四个真实 ELF 分别通过 `dbg-frame-riscv.test.mjs` 的静态 CFI/寄存器规则检查。此项只确认构建/CFI 数据完整，尚无四份板上 GDB oracle 或 200 轮 Web 压力结果。
- **待办边界：**探针重新被 Windows 和 WebUSB 识别后，先重跑 `make full_flow_6800evk`；随后逐一烧录四种配置、采集精确 ELF 对应的 GDB oracle，并按 H743 的方法各跑 200 轮帧/局部变量验收。之前 Og/DWARF4 的 80/200 断连记录仍不算通过。

### 2026-10-06 10:31–11:08 HPM6800EVK full_flow 与栈帧矩阵通过

- **供电恢复后 full_flow：**10:31 板卡重新上线，`board-check-6800evk` 读到 RISC-V IDCODE `0x1000563d`。第一次恢复后的 full_flow 中断于 RTT 转发控制块检查；确认掉电后状态未完整恢复，重新认板并完整重跑后，`make full_flow_6800evk` 于约 10:44 退出码 0。固定功能判决 20/0、固定 RISC-V 调试 57/0、随机切换 3/3（feature → debug → feature；调试 53/0）。
- **功能数据：**两轮 RTT Viewer 为 78.8/78.7 KB/s，错位读与溢出丢失均为 0；RTT 转发为 1.379/1.380 MB/s；约 10.2 秒存盘一致性 99.61%/99.78%，积压 108/72 KB。狂发/Scope 交替烧录 5 组完成，均值 2.37/2.44 秒。J-Scope 高速 1/3 变量 @2 µs 为 281.5/281.6 kHz 与 52.0/51.8 kHz，但探针跳拍很多（1 变量约 654k 次、3 变量约 1.34M 次/3 秒）；高速档是吞吐上限压力项，不代表无损。低速 1 变量 @20 µs 与 3 变量 @30 µs 分别为 50.00/33.33 kHz，探针/USB/序列缺口均为 0。
- **新发现并修复：**Og/DWARF4 首次 200 轮主体全部通过，但末尾失效检查发现 RISC-V `writeReg()`、`memWrite()` 没有清除缓存栈帧；之后测试器在已经断开的会话上无条件读硬件触发器，报 `dmiWrite` 空引用。RISC-V 两个写入口现在与 ARM 后端一致，写前清除帧缓存；断开也清缓存。测试收尾在断开后重新连接，再读取硬件触发器确认干净状态。修复后的 Og/DWARF4 完整重跑通过。
- **四配置实板矩阵：**每个配置均烧入对应 ELF 并逐字节校验 Flash；用 WCH RISC-V GDB 对同一 ELF 采集并验证 7 个 GDB 检查点 oracle；Web 侧按匹配 oracle 完成 200 轮递归/切帧/局部变量压力。各组都检查读回值、同名变量作用域与位置迁移、寄存器和栈只读性、写操作/单步/继续/重载 ELF/复位/断开后的缓存失效及触发器清理。全部 20 项通过、0 失败：

| 配置 | 完成时间（北京时间） | ELF SHA-256 | Web 报告 |
|---|---:|---|---|
| Og / DWARF4 | 10:55:43 | `54c380cceb7954a201e237c5fc647fc49f5b4947999de1927ab2f01b598a8723` | `tmp/dbg-frame-6800evk-og-dw4-200-rerun.json` |
| Og / DWARF5 | 11:00:03 | `160b9633434c58dfbd94d5e1fb4fdd3cb06b07297f187f0803742ceb7b4f555a` | `tmp/dbg-frame-6800evk-og-dw5-200.json` |
| Os / DWARF4 | 11:04:13 | `85ba8c0e876f04ae9b5228ea23d5fd61e09cf886becaa8c2766ce7d0a14c2a25` | `tmp/dbg-frame-6800evk-os-dw4-200.json` |
| Os / DWARF5 | 11:08:16 | `8fdc0a4074b768ea12e35b0711c66cbbb8faf4faaba292d094266c226b14ef80` | `tmp/dbg-frame-6800evk-os-dw5-200.json` |

四份匹配 oracle 分别为 `tmp/frame-oracle-6800evk-{og-dw4,og-dw5,os-dw4,os-dw5}.json`。离线全回归 `make test-offline` 也以退出码 0 完成。原始 oracle、JSON、日志保留在本地 `tmp/`，不纳入 Git。
