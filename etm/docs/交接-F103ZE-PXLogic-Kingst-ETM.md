# F103ZE ETM + PXLogic / Kingst 交接记录

更新时间：2026-10-13（Asia/Shanghai）  
工程：`E:\web-etm-trace`  
目标：STM32F103ZE（Cortex-M3，ETMv3），4 位并行 DDR Trace；用逻辑分析仪采集，后续解出 TPIU / ETM 指令执行流。

## 2026-10-13 PXLogic 换线后已完成采集与 Web 解码分析

已使用另一位 AI 的 `tools/la/pxlogic_la.py`，500 MSa/s、2.0 V、不加滤波，连续三轮10 ms真实采集与解码通过：185030 / 187254 / 188104条指令，overflow及包错误均为0，全部对应源码，并核对分支落点和线性指令连续性。页面8774可打开 `PXLogic · F103ZE · 48/24 MHz · 10 ms · C`。

当前通过的是连续运行时的Python自由采集→Web导入分析；网页直接采集及PXLogic断点联动触发仍待适配和验收，不与Kingst联动混淆。配置/波形根因、复现命令、证据位置及限制见 [PXLogic-F103ZE采集解码验收](PXLogic-F103ZE采集解码验收.md)。下方旧实验失败记录保留作为过程证据。

## 2026-10-13 当前推荐：CDC 48/24 MHz，分支广播可选且默认关闭

当前板上是普通 `firmware/stm32f103_cdc_trace/fw-ze.elf`，CPU/HCLK 48 MHz、TRACECLK 24 MHz。
新页面入口：http://127.0.0.1:8774/ 。请重新载入新的 ELF，不使用原72 MHz镜像核对目标。
采集页新增分支广播勾选和带宽说明，返回栈压缩/周期计数按芯片能力启用；实际配置回读并保存。
单纯降频时同一CDC窗口仍有16次overflow，关闭分支广播后最终控制器三轮10 ms均为0。
已知负载的复位/无复位/再复位也通过，首轮652个完整round的调用和循环次数正确。
详见 [CDC时钟与ETM选项验收](F103ZE-CDC-48MHz与ETM选项验收.md)。

## 接手必读：没有波形的根因、Web 稳定流程与分支广播带宽

本节为2026-10-13对前序排查的核对结论。下面旧记录包含当时的推测，应以本节和当前验收报告为准。

### 1. 最初没有 Trace 是多个问题叠加

**明确的初始化故障：**旧独立固件把状态等待和编程顺序用错，超时后直接返回，跳过后续输出配置。
现场读到 `ETMCR=0xC10`（编程位bit10仍置位，不能据ETMEN置位认定正在输出）、`DBGMCU_CR=0x7`
（TRACE_IOEN关闭）、`ITM_TCR=0`。这些是初始化没有完整完成的证据；不能说固件初始化“一直是对的”。
另一个 `etm_status` 结算早于引脚状态回填的问题导致误报，需要与实际输出故障区分。

**接线与GPIO问题：**最初五路曾接为PE1～PE5，已纠正为CH0～4对应PE2～PE6，并共地。
GPIOE的PE2～PE6必须配置为50 MHz复用推挽，DBGMCU也必须打开Trace引脚门控；只配置ETM本体不足以建立输出。

**诊断改变了目标状态：**此前重新建立调试会话时所用的初始化、复位/暂停和CoreSight写入，以及TPIU协议切换，
可能改变已经由固件建立的Trace状态，造成“配置后有波形、诊断后又消失”的反复现象。
这不是“所有pyOCD attach都会复位”的通用结论，更不能认为SWD只读回读本身必然破坏Trace。
当前验收显式使用持续的attach会话，实际运行同一套Web目标控制器，已经证明SWD在线控制与Trace采集可以配合使用。

### 2. 当前Web为什么稳定

固件负责真实HSE/PLL、CPU/HCLK、Flash等待周期、USB及业务外设；普通Web模式不自行开启ETM。
网页识别F103ZE的ETMv3能力，使用其实际的 **ETM → TPIU** 通路，不套用H7的Funnel/ETF寄存器。

```text
持续的同一探针会话
  → 校验目标Flash与ELF
  → 复位/运行并停在明确的业务断点
  → 配置Trace GPIO、门控、ETM/TPIU并回读
  → 武装Kingst
  → 继续CPU并采集
  → 保存原始数据和配置快照、解码
  → 恢复本页断点与取得的寄存器资源
```

ETM的关键控制是：退出power-down，关闭输出门控，置编程位并等有效编程状态，写配置，退出编程并使能输出。
失败会明确报错，不能用“忽略所有超时、继续初始化”代替确认配置成功。
TPIU在此联动流程中一直保持并行协议0；恢复时也保留该协议，避免已实测的运行中切换停钟问题。
“恢复”不表示自动切回SWO；切换协议前应复位并由对应页面重新配置。

成功标准同时包括实际波形、合法TPIU/ETMv3包、匹配ELF及已知调用/循环核对，不能仅凭TRACECLK存在或四条数据线有跳变认定成功。

### 3. 24 MHz TRACECLK仍可能overflow的原因

四条数据线、双沿DDR、引脚TRACECLK 24 MHz时，原始线速上限为：

```text
4 bit × 2 edge/cycle × 24 MHz = 192 Mbit/s = 24 MB/s
```

格式化、同步及源ID占用一部分带宽，ETM有效载荷上限更低。LA的500 MSa/s只用于过采样波形，不能增加目标输出容量。
ETM-M3的FIFO只有 **24字节**，短时间产出快于出口就可能溢出；整段窗口的平均数据量低于上限也不能排除突发溢出。
[Arm Cortex-M3 DDI0337H，表10-1及ETMCR说明](https://documentation-service.arm.com/static/5e8e107f88295d1e18d34714)

分支广播打开后，直接分支也输出目的地址，增加数据量。
同一CDC初始化起点、500 MSa/s、10 ms实测：原72/36 MHz为63次overflow；48/24 MHz广播开为每轮16次；广播关最终三轮均为0。
进一步核对48 MHz广播开记录：16个overflow之后恢复的首条指令都位于初始化的`memset`中，集中在开头区间。
这支持“紧凑循环的突发地址输出填满FIFO”的判断；内部FIFO占用率没有直接测量，不能据此量化每一时刻的峰值速率或宣称任意负载都不会溢出。
证据：`records/capture-2c0589af7f9049eeaa33159eacd78b5c/decoded.json`，以及
[CDC对照与最终验收](F103ZE-CDC-48MHz与ETM选项验收.md)。

### 4. 关闭分支广播的代价

关闭广播仍跟踪分支执行结果。对于直接分支，解码器从匹配机器码推导目的地址；减少的是可由代码镜像推导的显式地址输出，
不是对PC做间隔采样，也不是裁剪函数或时间范围。间接分支目的地址仍需由协议传递。
固定代码且镜像完全匹配时，完整指令顺序仍可重建；本工程已用652个完整round、5216次mix调用及分支落点核对验证。
[Arm ETMCR bit8定义](https://documentation-service.arm.com/static/5e8e107f88295d1e18d34714)

代价是更依赖正确的代码镜像：自修改代码、动态生成的RAM代码、运行时补丁等场景需要取得对应代码内容，不能只依靠旧ELF。
广播开启提供更多显式地址证据，有助于地址流分析；但即使开启广播，逐条指令和源码重建仍需要对应机器码/符号，不能凭它获得变量值。
当前CDC是固定Flash代码，Web还校验Flash与ELF，因此 **48/24 MHz + 广播关闭** 是合适默认；需要对照时可在采集页主动勾选。

### 5. CPU 24 MHz / TRACECLK 24 MHz能否实现

这里的Trace频率始终指 **PE2引脚TRACECLK**，不是内部TRACECLKIN。F103ZE的CPU时钟为HCLK；TPIU输入接HCLK，
同步Trace引脚时钟固定为HCLK/2。因此CPU降到24 MHz，TRACECLK也降到12 MHz，不能通过普通RCC分频独立配置成24/24 MHz。
[ST RM0008 §31.17.6、§31.17.8](https://www.st.com/resource/en/reference_manual/rm0008-stm32f101xx-stm32f102xx-stm32f103xx-stm32f105xx-and-stm32f107xx-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)

| 情况 | 四位DDR原始线速上限 | 平均到每CPU周期的原始带宽 | F103ZE是否可配置 |
|---|---:|---:|---|
| CPU48 / TRACECLK24 MHz | 192 Mbit/s | 4 bit | 当前默认 |
| CPU24 / TRACECLK24 MHz | 192 Mbit/s | 8 bit | 假设，不能独立配置 |
| CPU24 / TRACECLK12 MHz | 96 Mbit/s | 4 bit | 实际随HCLK降频的结果 |

假设可以维持Trace24而将CPU减半，相对带宽会提高一倍，但能否保留广播仍要按真实分支密度、其他Trace数据和突发量实测。
F103单纯降低主频不会改变上述带宽比，不能把降频当作必然消除overflow的方法。

### 6. 旧记录中的寄存器表述更正

- `SPPR=0`为并行同步Trace；`1`为Manchester SWO、`2`为NRZ SWO、`3`才是保留值。1/2不是非法时钟源。
  本板切到SWO时PE2并行时钟消失、切回后需要复位的观察应作为协议切换限制记录，不推广成所有器件或所有SWD操作的结论。
- `ETMSR bit1`是有效编程状态，应与`ETMCR bit10`的编程请求配合检查；不能把它笼统当成H7的IDLE位。
- `ETMSR bit0`为未报告的溢出标志，不是TRACING使能位；旧记录的`ETMSR=1 → 正在追踪`推断不成立。
  [Arm IHI0014Q，ETMSR位定义](https://documentation-service.arm.com/static/5f90158b4966cd7c95fd5b5e)
- 不依据ETMCR中对ETM-M3无引脚控制作用的端口位判断物理线数；TPIU `CSPSR=0x8`是bit3选择四条数据线，不是“8条线”或“8-bit输入”设置。
- 不能把M3的保留位当作通用ETMv3地址过滤选项；当前Web不写保留的返回栈/周期准确位，也不借保留位来“打开所有地址”。

## 2026-10-13 集成状态：F103ZE 已走通 Web → Kingst 联动

当前操作说明以 [F103ZE-Web联动验收.md](F103ZE-Web联动验收.md) 为准；下面的独立固件解码章节保留为前序证据。
本节前序验收当时的普通固件是 `fw-ze-48-workload.elf`，CPU/HCLK 48 MHz、引脚 TRACECLK 24 MHz；**不是**独立开启 ETM 的 `fw-ze-standalone-48-workload.elf`。
普通固件在 Web 配置之前没有 Trace 输出，这是预期行为。

统一顺序：匹配 ELF → 连接识别 → 添加断点并选起点 → 填实际 HSE 8 MHz、核对时钟 → 配置 Kingst → 联动采集。
服务入口为 http://127.0.0.1:8773/ 。本轮普通固件下三次实板采集已覆盖默认复位、无复位、再次复位，
每轮恢复约 51,550 条指令记录、2 次 SysTick，包问题与 overflow 均为 0；探针已释放。

另一位 AI 的 PXLogic CLI 和界面说明已复核并合入。记录保护、核验采样率与 CSV 时间轴一致性、密集跳变提取性能已补测。
**Web 目前仍使用 Kingst**；PXLogic 的命令行位流/CSV 链已验证，触发、ready、停止/取消与真实 ETM 尚待验收，未接入 Web 联动。
详见 [双 AI 交付集成验收.md](双AI交付集成验收.md) 与 [PXLogic-LA采集后端.md](PXLogic-LA采集后端.md)。

## 前序进展：独立固件 ETMv3 执行路径解码验收

最新结论以 [F103ZE-ETMv3-解码验收.md](F103ZE-ETMv3-解码验收.md) 为准，下面保留前序排查历史。
Kingst 10 ms 原主循环记录恢复 189,669 条指令记录和 10 次 SysTick 中断；新的 2 ms 分支负载记录恢复 52,159 条指令记录。
129 个完整 round 的交替调用和 1～8 次循环全部核对通过，包错误/overflow 均为 0，捕获两端未同步/不完整边界保留。

关键修正：旧对齐脚本仅使用一种时钟沿，漏掉一半 DDR 数据；“bit0 必须为 1”和“00×N+FF×N 同步头”的评分依据不成立。
现用双沿 DDR 提取、TPIU `FF FF FF 7F` 帧同步及 OpenCSD ETMv3 解码。该阶段板上固件为 `fw-ze-standalone-48-workload.elf`（48/24 MHz）。

## 解码方法与复现步骤（接手者从这里开始）

本次采用的完整链路是：

```text
Kingst CSV 跳变列表（CH0=CLK，CH1..4=D0..3）
    ↓ 双沿 DDR 取样，生成两种字节相位候选
TPIU 格式化字节流
    ↓ FSYNC 定位、16 字节帧解复用
Trace ID 2 的 ETMv3 字节流
    ↓ OpenCSD 全解码器 + 匹配 ELF 的机器码镜像
指令地址范围、条件成立/未成立、中断及同步边界
    ↓ Capstone 反汇编核对 + ELF 符号 + DWARF 行号
按执行顺序排列的指令与源码记录
    ↓ 历史调用关系与浏览索引
执行流 / Timeline / 调用统计 / 火焰图 / 源码对照
```

### 1. 输入和目标状态

原始输入用 Kingst 的跳变 CSV，KVDAT 另外保留，便于回看同一次采集。CSV 每行包含该时间点五个通道的电平，
不是一条独立的 Trace 包；第一行是窗口初始电平，不能当作时钟边沿。时间单位为秒。
`tools/trace_port.py:read_tracks()` 读取 UTF-8 或 Kingst 中文 GB18030 表头，并为每条通道建立真实跳变列表。

当前已验证参数是 CPU 48 MHz / TRACECLK 24 MHz、500 MSa/s、2.0 V。
固件自行配置 ETM 后直接采集；采样期间不插入 SWD attach/halt，也不扫描 SPPR。
寄存器核对在采集之后做，结束时 `reset run` 让固件重新建立输出状态。
解码必须配套当时烧录的 ELF；改变固件、编译参数或代码后，不能继续用旧 ELF 重建新采集。

### 2. 双沿 DDR 提取与相位确认

`tools/trace_port.py:extract()` 遍历 TRACECLK 的**所有跳变**，既包含上升沿，也包含下降沿。
设相邻时钟沿为 `t[i]`、`t[i+1]`，取样时间为：

```text
sample = t[i] + fraction × (t[i+1] - t[i])
nibble = D0 | (D1 << 1) | (D2 << 2) | (D3 << 3)
```

目前选择 `fraction=0.25`。D0～D3 的位序由实际接线确定，分别落到 nibble 的 bit0～bit3。
两个相邻 nibble 按先低后高组合成字节，分别尝试从第 0 个或第 1 个 nibble 开始，得到 phase0/phase1。
只使用一种时钟沿会漏掉一半 DDR 符号，旧 `tmp/f103ze_decode_align.py` 的这种做法不能继续用。

**相位通过后续协议结构确认，不能靠字节 bit0 的比例猜。**
`tools/decode_trace.py:decode_capture()` 对两个字节候选分别运行 TPIU 解复用，必须只有一个候选能形成有效帧且包含目标 Trace ID。
原先 2 ms CDC 基线的字节相位是 1，新分支负载捕获的字节相位是 0；相位随采集起始边沿变化，不能写死。

对分支记录运行 `tools/calibrate_capture.py` 扫描了多个取样位置。
fraction 为 0.1、0.2、0.25、0.3 时，解出的 ETM 载荷 SHA256 完全相同；0.25 时最近数据跳变距取样点约 3 ns。
这是本次选择 0.25 的实际依据。半周期中点附近恰好可能是数据变化区，不能默认取 0.5。
相位复验结果保存在 `records/f103ze-48-workload/phase-validation.json`。

### 3. TPIU 同步与 Trace ID 分流

`tools/tpiu_demux.py:demux()` 识别以下物理格式化标记：

- FSYNC：`FF FF FF 7F`。
- HSYNC：`FF 7F`。

先跳过第一个 FSYNC 之前的字节；随后按协议消费同步标记和 16 字节格式化帧。
每帧前 15 字节包含数据或 Trace ID，最后 1 字节是辅助位；解码时恢复偶数字节被借用的最低位，
按 ID 切换规则把字节分配给对应源。ID 0 的填充与 ID 2 的 ETM 数据分开保存。
不能把整个原始数据中所有 `FF 7F` 简单删除，因为合法 Trace 数据也可能包含相同字节值。

帧出现非法结构时当前实现直接报错，不把破损帧前后的数据拼接成“可信路径”。
未同步开头、不完整尾帧和未知源字节均记录在报告里。
本次分支记录的 TPIU 结果是 2,947 个完整帧、170 个 FSYNC、结构错误 0，提取出 28,033 字节 ID 2 载荷。

TPIU FSYNC 与 ETMv3 A-Sync 属于两层：ETMv3 A-Sync 是至少五个 `00` 后跟 `80`。
之前文档中的 `00×N + FF×N` 搜索条件和“所有字节 bit0 必须为 1”的条件均不成立。
看见同步字节模式本身也不等于执行流已验收，必须继续解析包并核对机器码。

### 4. OpenCSD ETMv3 包和机器码路径重建

复用了工程已有的 OpenCSD 1.8.3 库，扩展 `native/decoder.cpp` 支持 ETMv3。
创建解码器使用 `OCSD_BUILTIN_DCD_ETMV3` 和 `OCSD_CREATE_FLG_FULL_DECODER`，
设置 `ARCH_V7`、`profile_CortexM`；H7 原有 ETMv4 路径保持可用。

传给 `ocsd_etmv3_cfg` 的配置来自目标寄存器回读，而不是 H7 的默认配置：

| 配置字段 | 目标寄存器 | 回读值 |
|---|---|---|
| `reg_idr` | ETMIDR | `0x4114f241`（ETMv3.4） |
| `reg_ctrl` | ETMCR | `0x910` |
| `reg_ccer` | ETMCCER | `0x18800` |
| `reg_trc_id` | ETMTRACEIDR | `2` |

JSON 文件是 `configs/f103ze-etmv3.json`，必须含 `"protocol": "ETMv3"`。
`tools/decode_trace.py:decoder_config()` 验证协议与必要字段，ETMv3 不使用 H7 的 IDR0/IDR1 等 ETMv4 配置。

`Image` 从匹配 ELF 中读取 `PT_LOAD` 段，按 ELF 的加载后地址提供给 OpenCSD 的内存访问器。
因此解码器能够结合地址同步、P-header 中的执行原子、分支地址和异常包，沿真实机器码恢复指令范围。
包监视回调保存包偏移、长度、原始十六进制数据及包类型；通用元素回调保存指令范围和同步/异常边界。
主要实际包类型包括 `A_SYNC`、`I_SYNC`、`P_HDR`、`BRANCH_ADDRESS`、`EXCEPTION_EXIT`。

这一步不是对 PC 做插值。缺少同步、缺少地址或缺少匹配机器码时，解码器输出边界/未知状态；当前流程不补造执行路径。
依据：[OpenCSD 官方实现](https://github.com/Linaro/OpenCSD)与[解码 API 说明](https://github.com/Linaro/OpenCSD/blob/master/decoder/docs/prog_guide/prog_guide_main.md)。

### 5. 指令、函数和源码展开

`tools/decode_trace.py:decode()` 用 Capstone 的 Thumb / M-class 模式反汇编每个 OpenCSD 指令范围。
反汇编条数必须等于 `num_instr_range`，最后一条指令末地址必须恰好等于范围结束地址；不符就报错。
按解码器给出的执行状态标记条件成立与未成立指令，保留异常和缺口事件。
当前派生分析对尚未证实的 IT 谓词结果仍采用保守的未知标记，不能把未知条件当作已执行。

函数名来自 ELF `.symtab` 的函数地址范围；源码文件/行号来自 DWARF 行号表。
**源码文本也必须与 ELF 相配**：修改源码后用旧 ELF，可能仍得到正确 PC 和行号，却显示错行的文本。
因此记录包保存 ELF 和配套源码快照，历史记录使用自己的源码，而不是后续被修改的工作目录文件。

`tools/flow_analysis.py` 在确定的指令路径上分析调用/返回与中断，采集前已有调用层级保留为未知祖先。
`tools/record_store.py` 为结果建立 SQLite 浏览索引，供页面的分页、序号定位、Timeline 和火焰图查询。
本次没有逐指令时间戳或周期计数，所以横轴是执行序号；指令计数不能直接当作耗时或 Hz 调用率。
变量值、局部变量和寄存器历史不在本次指令 Trace 解码范围内。

### 6. 为什么认为这次路径恢复可信

除了“解码器没报错”，本次还做了相互独立的检查：

1. OpenOCD 对实际 Flash 与 ELF 比对通过，分支固件验证 20,660 字节。
2. 多个采样相位生成相同 ETM 字节流，且时钟没有解析器定义的异常半周期。
3. TPIU 帧结构和 Trace ID 分流成立，ETM 输入包字节全部核算，没有遗漏或重叠。
4. 反汇编范围和 OpenCSD 的指令条数吻合，所有本次指令都有 DWARF 映射。
5. `tools/validate_f103_capture.py` 用已知负载核对控制流：129 个完整 round 都是八次交替叶函数调用，
   每轮 `trace_mix` 的循环次数均为 1～8；12,035 次直接分支与 37,527 次线性指令衔接符合机器码。
6. 分支窗口恢复 52,159 条指令记录、2 次 SysTick；包问题和 overflow 均为 0。
7. 57 项现有 Python 回归测试通过，旧 H7 ETMv4 字节流用更新的 native 解码器复解仍无错误。

当前窗口仍有 219 字节 ETM 未同步开头、167 字节 TPIU 同步前物理输入和 2 字节不完整 TPIU 尾帧。
这些部分不展开为“已恢复指令”。结论限于已同步窗口与当前验收负载，不能推导为整个启动过程或任意高频负载都无损。

### 7. 接手后如何复现

如果只复解保存的记录，完全不需要连接硬件；从 `E:\web-etm-trace` 运行：

```powershell
# 先构建包含 ETMv3 支持的 OpenCSD 包装器（首次或 native 代码修改后执行）
pwsh -NoProfile -File .\build-decoder.ps1

# 复解本次 2 ms 分支负载，用配套 ELF 和 F103 配置
python tools\decode_trace.py captures\f103ze-48-workload-2ms.csv --elf firmware\stm32f103_cdc_trace\fw-ze-standalone-48-workload.elf --config configs\f103ze-etmv3.json --out decoded\f103ze-replay

# 核对真实分支、八次交替调用和 1～8 次循环
python tools\validate_f103_capture.py decoded\f103ze-replay\decoded.json --elf firmware\stm32f103_cdc_trace\fw-ze-standalone-48-workload.elf
```

如需新采集，先烧录上述独立固件，烧录工具结束时 `reset run`；随后直接采样，中间不做 SWD 诊断：

```powershell
python -u tools\la\kingst_la.py capture --rate 500000000 --time 0.002 --threshold 2.0 --channels 0 1 2 3 4 --out E:\web-etm-trace\captures\f103ze-new.csv --stats --timeout 30
```

如需逐层定位失败，可先运行物理提取并查看两个候选：

```powershell
python tools\trace_port.py captures\f103ze-new.csv --sample-fraction 0.25 --out captures\f103ze-new-ddr
# phase0/phase1 都应分别检查；只对协议确认有效的候选继续处理
python tools\tpiu_demux.py captures\f103ze-new-ddr.phase0.bin
```

建议日常直接使用 `decode_trace.py`，它会自动验证两个候选并拒绝相位不唯一的输入。
CSV 输入表示“尚未移除 TPIU 的物理采集”；BIN 输入表示“已经解复用的单一 ETM 源字节流”，两种输入语义不要混淆。

### 8. 输出文件与查看位置

- `decoded/f103ze-workload/decoded.json`：包、指令、源码、质量与派生调用统计。
- 同目录 `native.jsonl`、`native.log`、`native-config.txt`、`etm.bin`、`tpiu.bin`：分层排查中间结果。
- `records/f103ze-48-workload/f103-validation.json`：真实路径验收报告。
- `decoded/f103ze-workload/simplified.html`：离线左右两栏执行流/源码对照。
- `decoded/f103ze-workload/f103ze-48-workload.etmrecord`：可重新解码的采集包，含 CSV、KVDAT、ELF、配置和源码快照。
- 页面 `http://127.0.0.1:8767/`：选择“F103ZE · CPU 48 / Trace 24 MHz · 2 ms 分支负载”。

网页已支持这些 ETMv3 记录的解码与浏览；F103 的在线 SWD 配置和联动采集控制仍待适配，暂用独立固件与 Kingst 脚本采集。

## 前序排查：修复初始化后，诊断过程仍会改变被测状态

### 结果（干净烧录 + 复位后立即采集，全程不碰 SWD）

```
CH0(PE2/TRACECLK) = 24.002 MHz   48002 跳变   ← 精确等于 48/24MHz 档理论值
CH1(PE3/TRACED0)  = 10.885 MHz   21765 跳变
CH2(PE4/TRACED1)  =  4.005 MHz    8007 跳变
CH3(PE5/TRACED2)  =  7.659 MHz   15313 跳变
CH4(PE6/TRACED3)  =  6.064 MHz   12123 跳变
```

数据形态是**非周期不规则跳变**（真实 trace 包特征，各线相位互不相同），不是时钟那种规整方波。
采集文件：`captures/f103ze-standalone-clean2.csv`（500 MSa/s、1 ms、2.0 V、ch0~ch4）。

### 根因（重要，避免再踩）

**在修复初始化缺陷之后**，此前反复看到“只有时钟、数据线恒高”还有诊断过程改变目标状态的因素；不能把全部故障归为诊断方式：

1. 当时采集前后使用的pyOCD目标初始化/连接流程改变了CoreSight状态，可能包含复位或初始化写入，
   因而此前配置的trace状态丢失，再采时只剩时钟。这是当时那套连接/初始化路径的实测现象，
   不代表任何pyOCD attach，或只读SWD检查，都会复位目标。
2. 更隐蔽的一条：**给TPIU的`SPPR`(0xE00400F0)写1/2会切到SWO协议，3为保留值；当时切换后观察到并行输出消失，
   连 TRACECLK 都停**，而且**写回 0 也恢复不了，必须复位目标**。
   实测（`tmp/f103ze_try_all.py` 扫描）：基线组四条线全跳；在本板把SPPR切到SWO后，并行Trace输出消失，
   回写0也没有恢复，完整复位与重新配置后才恢复并行输出。RM0008定义的SPPR=1/2是合法SWO协议，并非非法时钟源。
   → 该联动流程把SPPR保持0；本板切换输出协议后须完整复位和重新配置再验。

### 正确的工作方式（已验证）

```
# 1) 烧录（OpenOCD 结束时会 reset run，固件自行初始化并保持运行）
bash tools/ocd_flash.sh stm32f1x.cfg stm32f103_cdc_trace/build/ze-standalone-48/fw.elf
# 2) 等约 2 秒让固件配完
# 3) 此独立固件历史采集配置中直接采集；不要重复会改变该目标状态的默认初始化/复位。
#    持续SWD会话及只读回读是否安全取决于会话选项；当前Web模式已验收联动控制。
python tools/la/kingst_la.py capture --rate 500000000 --time 0.001 --threshold 2.0 \
  --channels 0 1 2 3 4 --out E:\web-etm-trace\captures\xxx.csv --stats --no-kvdat
```

如果要**边配边采**，必须用同进程脚本 `tmp/f103ze_try_all.py`（保持 pyOCD 会话，内部 subprocess 调
kingst_la.py 采集），不要分两个进程、也不要中间 halt。

### 本轮对固件的实际修正（仍然有效，是真 bug）

虽然"数据线不动"是诊断造成的假象，但固件里确实存在三个缺陷，已修：

1. `trace_etm_standalone_init()` **第一个状态等待超时（旧记录误称IDLE）后直接 `return`** →
   DBGMCU（trace 引脚使能）、ITM 同步、TRACEOUT 收尾全被跳过。
   修复前实测 `DBGMCU_CR=0x7`（TRACE_IOEN=0）、`ITM_TCR=0`、`etm_status=2`。
2. 编程状态等待顺序错误：应置编程请求后等待ETMSR bit1生效；旧代码的错误等待触发提前返回。不要把H7的IDLE逻辑套到M3。
3. `etm_status` 在引脚位（bit7）之前结算，永远报 3 而非 4。

修复后：`g_trace.etm_status = 4`、`etm_bits = 0xff`（八步全过）。
新增 `etm_bits` 位图，逐步定位失败点，不用再猜。

### 顺带查清

- **F103ZE 没有 funnel**：ROM 表按 PIDR part 号识别为 ITM(0x001)/DWT(0x002)/TPIU(0x023,DEVID=0xCA1)/ETM(0x024)。
  ETM 直连 TPIU，不存在"中间 funnel 没使能"。
- `ETMTECR1`(0xE0041024) 在此芯片不可写（写 0x01000000 回读 0），按 ETMv3 语义 `ADDR_COMP_1=0` 即 trace-all，不影响功能。
- `0xE0041FB4`(ETM LSR) 恒为 0x1、写 LAR 解锁键消不掉，但寄存器照样可写 → 不能用它判断解锁成败，
  已改为功能性判定（翻转无害位再回读）。
- `TPIU_FFCR` 在格式化器运行时**写不进去**（实测反复回读 0x102）；要改必须先写 FFSR.FtStopped。
  不过基线 `FFCR=0x102` 本来就能出数据，无需改动。

### ★ 封印基线（2026-10-13，可直接复现）

```
固件：firmware/stm32f103_cdc_trace/build/ze-standalone-48/fw.elf（48 MHz CPU / 24 MHz TRACECLK）
烧录：bash tools/ocd_flash.sh stm32f1x.cfg <该 elf>        # 结束时会 reset run
采集：python tools/la/kingst_la.py capture --rate 500000000 --time 0.002 --threshold 2.0 \
        --channels 0 1 2 3 4 --out E:\web-etm-trace\captures\f103ze-48-baseline-v1.csv --stats
数据：captures/f103ze-48-baseline-v1.csv（5.4 MB）+ .kvdat（KingstVIS 可直接打开回看）
统计：CH0=24.002MHz / CH1=10.951MHz / CH2=4.029MHz / CH3=7.687MHz / CH4=6.060MHz（各 1.6万~9.6万次跳变）
```

### 解码进展与诚实结论（重要，别高估）

已经做到的：

- **四条数据线全部有活动**，且**不同采集/不同运行状态下解出的字节流不同**
  （对比 `captures/f103ze-itm-baseline.csv` 与 `f103ze-itm-itm.csv` 的解码结果，
  前 96 字节内容明显不同），说明数据在动、且随运行状态变化。

**还没做到的（不要把上一条当成"tracing 已验收"）**：

- **TPIU/ETMv3 正式解码未完成**。用 `tmp/f103ze_decode_align.py` 穷举了对齐
  （采样沿 × 延迟 0..半周期 × 位序 × 半字节序），最佳档仍有 **35% 的字节 bit0=0**，
  而 TPIU 同步头的定义要求"字节 bit0=1"，所以**字节边界仍未确证**；
  也没找到标准的 `0x00×N + 0xFF×N` 同步序列。
- 解码出的字节流呈**周期性重复**（约 16~35 字节一轮）。这既可能是被测固件主循环太单调
  （一个空转循环，ETM 每轮产生同样的包），也可能是 TPIU 在发大量填充。
  **需要用"分支丰富"的 trace 负载复测才能区分** —— 这是下一步的第一件事。
- 判别实验 `tmp/f103ze_itm_probe.py`（开 ITM + 狂写 stimulus port0，2000 次）
  没有让字节流变成明显的 ITM 数据形态，所以"ITM 是否能经 TPIU 输出"也仍未确证。

### 下一步（按优先级）

1. **换丰富负载复测**：把固件主循环改成有多个函数调用/条件分支/不等的循环次数
   （类似 `stm32h7b0_spi_trace` 里 `trace_workload` 那种写法），重新采集。
   若解码流从"周期重复"变成"内容多变"，说明链路与解码都正常，之前只是负载太单调；
   若仍然周期重复，则问题在 TPIU/ETM 数据通路。
2. **先对齐标准同步序列**：TPIU 每 4096 字节插一个同步序列。先在长采集里
   （`--time 0.01`，更多字节）找它，用它的结构反推正确的位序/半字节序，
   而不是靠 bit0 启发式。
3. 对齐确认后，再实现 ETMv3 包解析（branch/PC 采样）→ 指令路径。
4. 全部验收后再决定是否回到 72/36 MHz 档（`build/ze-standalone/fw.elf`）。

### 本轮新增工具（都在 tmp/，可复用）

| 脚本 | 作用 |
|---|---|
| `f103ze_try_all.py` | 自动编排：保持 SWD 会话 + 逐组配寄存器 + 内嵌调 kingst 采集 + 解析统计 |
| `f103ze_decode_align.py` | 穷举 4 位 DDR 解码对齐（沿/延迟/位序/半字节序），用 bit0=1 比例评分 |
| `f103ze_phase_scan.py` | 相位扫描 + 同步序列搜索 |
| `f103ze_tpiu_sync.py` | 在 CSV 里直接找 TPIU 同步序列 |
| `f103ze_itm_probe.py` | ITM 判别实验（灌 ITM 数据看 TPIU 字节流是否变化） |
| `f103ze_etm_hold.py` | 配好 ETM/TPIU/ITM 后保持 SWD 会话 N 秒，便于边挂边采 |
| `f103ze_g_trace.py` | 读并解码 `g_trace`（含 `etm_bits` 八步位图） |
| `f103ze_trace_state.py` | 复位→运行→halt 后一次性 dump 全部 trace 寄存器 |
| `f103ze_coresight_scan.py` | 扫 ROM 表识别 CoreSight 组件 |

> 注意：`f103ze_try_all.py` 里试过给 `SPPR` 写 1/2/3 —— 当时切换后观察到并行TRACECLK停止，
> 且必须复位才能恢复。**SPPR 只写 0**。

---


1. **包解码**：现有 `tools/trace_port.py`、`tools/pxlogic_trace_port.py` 只做 4 位 DDR 字节流候选提取，
   **F103 的 ETMv3/TPIU 解码尚未完成**。Web 解码主链目前只覆盖 H7 ETMv4，不能直接宣布 F103 验收通过。
   需要：TPIU 帧同步（0x80000000 的 32 字节同步序列）→ 半字节解包 → ETMv3 包解析 → 指令路径。
2. **数据质量**：本次采集里 CH0 半周期中位数 10.0 ns（24 MHz 应为 20.8 ns），
   说明存在**多余短跳变**（2 ns 量级）。要么是信号完整性（需要示波器看上升沿/过冲），
   要么是 500 MSa/s 对 24 MHz DDR 的采样相位问题。解码前建议先确认同步序列能否稳定识别。
3. **频率档**：当前验证的是 48/24 MHz 实验档。若要回 72/36 MHz 档，
   烧 `build/ze-standalone/fw.elf` 或标准 `fw-ze.elf`，并重新确认 TRACECLK 应为 36 MHz。

---


### 根因（独立 ETM 固件里三个真 bug）

`firmware/stm32f103_cdc_trace/src/trace_hooks.c` 的 `trace_etm_standalone_init()`：

1. **致命：第一个状态等待超时（旧记录误称IDLE）后直接 `return`**。
   后果是后面的 **DBGMCU（trace 引脚使能）、ITM 同步、TRACEOUT 收尾全都没执行**。
   实测现场证据（修复前）：`g_trace.etm_status=2`、`DBGMCU_CR=0x7`（**TRACE_IOEN(bit5)=0，引脚没使能**）、
   `ITM_TCR=0`、`ETMCR=0xc10`（PROG 还挂着）。GPIOE_CRL 明明已是 `0x4bbbbb44`（PE2~PE6 = AF 推挽 50MHz），
   但外设没被使能 → 引脚上什么都没有 → **这就是"只有时钟/什么都没有"的直接原因**。
2. **状态等待和编程顺序用错**：应置编程请求后检查ETMSR bit1；旧代码的等待位置和状态位解释错误触发超时返回。正确顺序以当前Web驱动为准。
3. `etm_status` 在 `standalone_init()` 内就结算，而引脚位（bit7）是它返回**之后**才置的 → 永远报 3 而不是 4。

修法：IDLE 等待降级为"仅记录、不再中断流程"；按 ETMv3 规定的顺序（PWD_DWN=0 → 清 TRACEOUT → PROG=1 →
写寄存器 → 清 PROG → TRACEOUT=1）重写；新增 `g_trace.etm_bits` 位图逐步记录成败。

### 修复后的实测验证（都已通过）

```
g_trace.etm_status = 4        etm_bits = 0xff（八步全过）
DBGMCU_CR = 0xE7   ← TRACE_IOEN=1, TRACE_MODE=11（4 位 trace），修复前是 0x7
ITM_TCR   = 0x1000D
ETMCR     = 0x810  ← PWD_DWN=0, PROG=0, ETM_EN(b11)=1
ETMSR     = 0x1    ← bit0为未报告溢出标志；旧“TRACING=1”解释已更正，不能据此证明正在输出
TPIU: SSPSR=0xB / CSPSR=8（bit3选择4条数据线）/ SPPR=0（并行同步口）/ FFSR=8（格式化器在跑）/ DEVID=0xCA1
GPIOE_CRL = 0x4bbbbb44  ← PE2~PE6 = AF 推挽 50MHz
```

**Kingst 实测波形（`captures/kingst-f103ze-48-etmecr1.csv`，500 MSa/s）：**

```
CH0(PE2/TRACECLK) = 24.006 MHz，近乎理想方波（高 19.6 ns / 低 22.0 ns）
CH1(PE3) = 无跳变，恒高
CH2(PE4) = 无跳变，恒高
CH3(PE5) = 无跳变，恒高
CH4(PE6) = 无跳变（上一次采集里它曾以 6 MHz 动过）
```

时钟频率**精确等于 24 MHz**，与 48/24 MHz 档的理论值完全吻合 —— 说明
**并行时钟路径已在输出**；ETM有效包与执行流仍须另行解码验证。

### 顺带查清的两件事

- **F103ZE 的 CoreSight 里没有 funnel**：扫 ROM 表（0xE00FF000）只有 12 项，
  经 PIDR part 号识别为 ITM(0x001) / DWT(0x002) / TPIU(0x023, DEVID=0xCA1) / ETM(0x024)。
  即 **ETM 直连 TPIU**，不存在"中间 funnel 没使能"这个可能，排除了这条排查路线。
- **`ETMTECR1` 在这颗 ETM 上不可写**：写 `0x01000000`（INC_EXC, bit24）回读仍是 0。
  按 ETMv3 语义，`ADDR_COMP_1(bit0)=0` 时地址比较器不参与过滤，属于 "trace all"，
  所以该寄存器为 0 不影响功能（Linux 驱动在"全范围追踪"时写的就是 INC_EXC 位，此芯片未实现）。
- **`0xE0041FB4`（ETM LSR）恒为 0x1**，写 LAR 解锁键也消不掉，但 ETM 寄存器照样可写。
  即该 lock 位在此实现里不表示"被锁"，不能用它判断解锁成败 —— 已改成
  **功能性判定**（翻转一个无害位再回读，能读回即视为可写），不再误报 `etm_status=3`。

### 下一步（需要动硬件，不是固件）

现在**只剩数据线要查**：时钟完美、ETM 在 tracing，但 ch1~ch3 恒高。

建议按这个顺序排（由简到繁）：

1. **确认 CH1~CH3 的接线**：LA 的 ch1/ch2/ch3 必须分别接目标 **PE3/PE4/PE5**，且与目标共地。
   已知历史坑：最初错接成 PE1~PE5，后来改对成 PE2~PE6 —— 建议再逐根量一次通断。
2. **单通道验收**：只接一根线（比如 PE3→ch1），采 2 ms / 500 MSa/s，看是否有跳变。
   若单接有跳变、并接就没有，问题在线材/接触/回流路径。
3. **门限对照**：2.0 V 与 1.65 V 各采一次同一根线。若低门限出现跳变，说明是幅度不足（信号完整性），
   而不是固件。
4. 若确认 4 条数据线都能动，再做包解码：现有解析器（`tools/trace_port.py`、`tools/pxlogic_trace_port.py`）
   只产出字节流候选，F103 的 ETMv3/TPIU 解码还没做，Web 解码主链目前只覆盖 H7 ETMv4。

> 采集注意：48/24 MHz 档下 TRACECLK 半周期约 21 ns，500 MSa/s（2 ns/点）够用但余量不大；
> 若要看清数据线的建立/保持，优先用 1 GSa/s（设备最高支持 500 MSa/s，故取 500 MSa/s + 缩短窗口）。

---

## 历史记录（本轮之前的状态，保留备查）

## 当前结论

- 原固件没有初始化 GPIOE 的 PE2～PE6。目标运行时这几脚仍是输入态，LA 上能看到 TRACECLK，但数据线电平很差。已经在 F103ZE 固件中加入 PE2～PE6 高速复用推挽初始化，编译、烧录和 OpenOCD 回读校验均通过。
- 独立 ETM 固件选项也已加入。固件启动后设置 ETM、TPIU、DBGMCU 和 ITM 同步，不需要采集时连接 SWD。`g_trace.etm_status == 4` 表示初始化完成。
- PXLogic 曾成功捕获到 36 MHz 和 24 MHz TRACECLK 及活动数据线，但时钟/数据波形仍有异常短跳变。提高 PXLogic 门限会明显改变短跳变计数，说明逻辑门限或信号完整性影响很大；目前没有模拟示波器波形，不能单凭逻辑采样认定 MCU 输出驱动不足。
- KingstVIS 当前显示设备已连接，但切换 Kingst 后的最新实测没有捕获到 PE2 上升沿。自由采集完成后，导出 CSV 只有初始全 0 电平；明确设置 ch0 上升沿触发则等待 20 秒超时。此批 Kingst 数据无效，不能用于 ETM 解码。
- 因此当前还没有可信的 F103ZE ETM 解码或源码执行流。下一步先查清 Kingst 为何看不到 PE2 时钟，再继续包流重建。

> ↑ 以上"Kingst 看不到 PE2 时钟"的问题已在 2026-10-13 定位并修复：根因是独立 ETM 固件
> 提前 return 导致 DBGMCU 的 trace 引脚使能没执行。现在 TRACECLK 稳定在 24.006 MHz。


## 接线与设备状态

目标接线顺序经用户确认：

| LA 通道 | MCU 引脚 | Trace 信号 |
|---|---|---|
| ch0 | PE2 | TRACECLK |
| ch1 | PE3 | TRACED0 |
| ch2 | PE4 | TRACED1 |
| ch3 | PE5 | TRACED2 |
| ch4 | PE6 | TRACED3 |

用户曾发现最初接成 PE1～PE5，之后已纠正为 PE2～PE6。重新接线或更换 LA 后仍需逐根确认，并确认 LA 与目标板共地。

当前 KingstVIS 窗口标题为“MIPI16 已连接”。从 `vis.config` 读到 ch0～ch3 已启用、ch4 未启用；接下来需在 KingstVIS 的“通道使能设置”里启用 ch4。KingstVIS 的预设毛刺滤波为 ch0～ch4 启用 1 个采样周期；500 MSa/s 下为 2 ns。这个滤波不应滤掉 24/36 MHz DDR Trace 的正常半周期，但要以实测回读为准。

## 固件变更

### PE2～PE6 引脚初始化

`firmware/stm32f103_cdc_trace/src/main.c` 的 `MX_GPIO_Init()` 在 `STM32F103xE` 构建中：

1. 打开 GPIOE 时钟。
2. 将 PE2～PE6 配为 `GPIO_MODE_AF_PP`、`GPIO_NOPULL`、`GPIO_SPEED_FREQ_HIGH`（F1 高速输出档）。

这是独立于 ETM 是否由主机配置的硬件引脚配置。普通 `-Chip ze` 固件采用主机 SWD 配置 ETM/TPIU；`-StandaloneEtm` 构建则由固件在启动时自行配置 ETM/TPIU/ITM。

### 独立 ETM 构建

新增选项：

```powershell
pwsh -NoProfile -File .\build.ps1 -Chip ze -StandaloneEtm
pwsh -NoProfile -File .\build.ps1 -Chip ze -StandaloneEtm -CpuMHz 48
```

输出分别为：

- `firmware/stm32f103_cdc_trace/build/ze-standalone/fw.elf`：72 MHz CPU / 36 MHz TRACECLK。
- `firmware/stm32f103_cdc_trace/build/ze-standalone-48/fw.elf`：48 MHz CPU / 24 MHz TRACECLK（信号裕量对照档）。

最后烧录的是 **48/24 MHz 独立 ETM 版本**。由 `g_trace.etm_status` 在线回读为 `4`，PE 引脚寄存器也符合复用推挽设置。该阶段独立模式采集应避免插入会改变目标的调试初始化：当时使用的pyOCD初始化曾把`ITM_TCR`清零；不代表所有attach或只读SWD都会如此。若用 SWD 检查过目标，重新复位目标后再做 LA 采集。

构建脚本还修正了 USB 向量自检：原检查错用了向量表下标，并未去除 Thumb 地址最低位。现在检查 IRQ19/20 的真实向量，并与 USB HP/LP 函数地址比较。CB 和 ZE 构建均已通过新自检。

## PXLogic 实测记录

设备：PXView 显示 `PX_Tool PX-Logic U3 channel 32`。本轮选 Logic 模式、8 通道/最高 1 GHz、采样率 1 GHz；采集文件是每通道 LSB-first 打包的二进制文件。分析脚本：`tools/pxlogic_trace_port.py`。

有代表性的结果：

- GPIOE 未初始化时，ch0 约 36 MHz；数据线 ch1/ch2 多数低脉冲宽度只有 1～3 ns，ch3 基本恒高。
- 临时经 SWD 开 GPIOE 并将 PE2～PE6 改为复用推挽后，数据线出现持续跳变。这证明 GPIO 配置缺失确实是一个问题。
- 固件加入 GPIO 初始化后，若 SWD 工具未保持 ITM/TPIU 同步配置，某些采集又出现近似空闲电平；保持调试会话并写 `ITM_TCR=0x1000D` 后，四路数据都出现有效跳变。因此开发了独立 ETM 固件，避免正常 LA 采集依赖持续 SWD 会话。
- PXLogic 的门限扫描（目标当时为 72/36 MHz，2 ms）：1.2 V 约 148,297 个 ch0 边沿；1.65 V 约 147,549；2.0 V 约 144,328。理想 36 MHz 方波在 2 ms 应约 144,000 个边沿。门限提高后时钟边沿计数更接近期望值，但数据通道仍有大量短跳变。
- 48/24 MHz 独立固件下，PXLogic 5 ms 捕获约 249,292 个 ch0 边沿，中位半周期 21 ns；理想 24 MHz 时钟约 240,000 个边沿。比 36 MHz 档更接近，但仍有多余边沿，且尚未用 Kingst 对同一信号成功复测。
- 尝试写 F103 TPIU `ACPR=1` 后，实测 TRACECLK 没有按预期减半；已恢复为 0。不要假定 ACPR 能控制当前 F103 的并行 Trace 时钟。
- PXLogic 设备只公布 Logic 模式，没有模拟/DSO 模式。因此目前无法从 PXLogic 波形直接量出 VIH/VIL、上升时间、过冲或振铃。

主要采集目录：`captures/pxlogic-f103ze/`。其中有初始错接、GPIO 临时试验、同步试验、阈值扫描、独立固件 72/36 和 48/24 MHz 的原始采样与 JSON 报告。不要把 `raw-*` 目录以外的 `.phase*.bin` 当作完整解码结果；它们只是候选字节流。

## Kingst 最近一次失败及“触发”说明

1. 刚切换 Kingst 时，Windows 已枚举 `Kingst Logic Analyzer`，但 KingstVIS 标题是“MIPI16 未连接”，命令返回 `device disconnected`。
2. 正常关闭并重启 KingstVIS 后标题显示“MIPI16 已连接”。
3. 500 MSa/s、2 ms、2.0 V、ch0～ch4 的自由采集命令返回成功，实际深度为 1,000,000，实际窗口 0～2 ms，`get-last-error=no error`。脚本随后因相对导出路径未落盘而报错。
4. 用绝对路径导出当前 buffer 后文件只有 77 字节：表头及一行 `0,0,0,0,0`，没有跳变。
5. 改用显式 `--trigger "--reset --pos-edge 0"` 后，20 秒内没有等到 ch0 上升沿，命令超时；随后已执行 `python tools/la/kingst_la.py stop` 清除挂起采集。

所以当前“触发不了”的直接事实是 **Kingst 没看到 ch0 上升沿**。自由采集其实已结束，只是 buffer 没有信号。排查顺序建议：先在 KingstVIS GUI 打开实时状态，确认 ch0 接 PE2 且有 24 MHz 方波；再检查共地、线缆接触、通道使能和门限。确认 ch0 有波形后，再用上升沿触发；之后逐路检查 ch1～ch4。

Kingst 脚本默认 `set-trigger --reset`，含义是重置触发条件并自由采样，不是等待一个未命中的触发。显式 ch0 上升沿触发的命令形式已经用于本轮诊断。CSV 当前导出采用绝对路径；相对路径的 `export-data` 这次未成功落盘。

## 下一步建议

1. **先验证 Kingst 的原始硬件输入。** GUI 实时状态确认 ch0=PE2 有 24 MHz 时钟；若没有，先查目标运行状态、共地、线缆接触、通道启用与阈值。ch4 目前在 Kingst 配置中关闭，应启用。
2. **Kingst 单通道验收。** 用 500 MSa/s、1～2 ms、先自由采样，导出绝对路径 CSV；`stats`/`freq` 检查 ch0。再打开 ch0 上升沿触发验证确实能触发。
3. **四路数据验收。** 依次确认 ch1～ch4 的跳变和脉宽，再采 5～10 ms。至少应看见稳定的 24 MHz TRACECLK 和与 DDR 相符的数据变化。
4. **物理质量判断。** 当前证据只说明门限敏感和有多余数字边沿，不能单凭此认定 MCU 驱动能力不足。用示波器测 PE2～PE6 的实际电压、上升/下降时间、过冲和振铃；同时尽量缩短信号线、让地线紧邻信号线。屏蔽本身不能替代低电感回流路径。
5. **再做包解码。** Kingst 波形有效后，使用 `tools/trace_port.py` 或更新后的解析器做 4 位 DDR 采样相位与 TPIU framing 验证。现有解析器只生成字节流候选，尚未完成 F103 ETMv3/TPIU 解码；当前 Web 解码主链主要覆盖 H7 ETMv4，不能直接宣布 F103 执行流验收通过。
6. **最后恢复频率档。** 48/24 MHz 是实验对照固件。验证完后，若要继续用户原先的 72/36 MHz 档，烧录 `build/ze-standalone/fw.elf` 或标准 `fw-ze.elf`，并记录清楚目标上实际运行的版本。

## 常用命令

从 `E:\web-etm-trace` 运行 Kingst 自由采样（请用绝对输出路径）：

```powershell
python tools\la\kingst_la.py capture `
  --rate 500000000 --time 0.002 --threshold 2.0 `
  --channels 0 1 2 3 4 `
  --out 'E:\web-etm-trace\captures\kingst-f103ze-check.csv' `
  --stats --timeout 30 --no-kvdat
```

用 ch0 上升沿显式触发：

```powershell
python -u tools\la\kingst_la.py capture `
  --rate 500000000 --time 0.002 --threshold 2.0 `
  --channels 0 1 2 3 4 `
  --trigger '--reset --pos-edge 0' `
  --out 'E:\web-etm-trace\captures\kingst-f103ze-edge.csv' `
  --stats --timeout 20 --no-kvdat
```

如果触发等待卡住，先执行：

```powershell
python tools\la\kingst_la.py stop
```

## 关键文件与工作区状态

- `firmware/stm32f103_cdc_trace/src/main.c`：PE2～PE6 初始化、48/72 MHz 时钟分支。
- `firmware/stm32f103_cdc_trace/src/trace_hooks.c`、`trace_hooks.h`：DWT/诊断结构和独立 ETM 初始化。
- `firmware/stm32f103_cdc_trace/build.ps1`：独立 ETM / CPU 档构建、USB 向量自检。
- `tools/f103ze_etm.py`：标准 72 MHz 固件的主机 SWD 配置、状态和恢复脚本。
- `tools/pxlogic_trace_port.py`：PXLogic 逐通道二进制到 DDR 字节流候选提取。
- `tools/trace_port.py`：Kingst CSV 到 DDR 字节流候选提取。
- `tmp/`：PXLogic 临时同步采集、脉宽分析、阈值扫描和 Kingst 实验脚本。
- `captures/pxlogic-f103ze/`、`captures/kingst-f103ze-*.csv`：实验原始数据与报告。

本轮没有提交 commit。仓库状态含大量现有未跟踪工程文件及本轮新增文件，接手时先看 `git status --short`，不要批量清理或覆盖实验数据。
