# DWT 数据观察点与 bt 回溯

调试器侧栏新增「数据观察点（DWT）」和「调用栈」，不需要改变探针协议或固件。
已使用 akaLinkPro 和 STM32F103CB（128KB Flash / 20KB RAM）验证以下硬件行为：
DWT 的 CPU 读/写/rw 模式、1/2/4/16 字节范围、4 个比较器、复位重装与已有槽位保留；
匹配 CB 固件的 EHABI 深层调用链、源码行号、只读回溯及有效 RAM 范围内的候选扫描。
H743、浮点异常帧和 RISC-V 目前只有离线覆盖；下面清单中其他硬件行为仍需实际验收。

## 命令

```text
wp g_tick w 4
wp 0x20000000 r 4
wp 0x20000010 rw 16
wpl
wpd #1
wpd all
halt
bt
bt 8
bt scan 16
```

- `wp <地址|符号> [r|w|rw] [字节数]` 默认观察写入、4字节。
  范围须为2的幂且起始地址按该范围对齐；硬件不支持的 MASK 会被回读检查拒绝。
  字符/半字变量请明确写1/2字节，非对齐大范围请拆成多个观察点。
- `wpl` 显示稳定的比较器编号，`wpd` 按这个编号删除；删除其他项不重新编号。
- 原有 `w/watch`、`wl`、`wd` 仍是变量值监视，不会设置硬件观察点。
- `bt [深度]` 默认16层，允许1..64层；暂停后读取当前内核的栈，不自动停住运行中的程序。
- `bt scan [深度]` 是单独的候选地址扫描模式，结果明确标成 candidate。
- Tab 支持新命令、wp模式/大小、wpd编号和bt scan参数补全。

## DWT 支持与清理

当前实现针对 Cortex-M3/M4/M7（Armv7-M），根据 CPUID 区分内核，读取 NUMCOMP 决定容量。
Armv8-M 的 FUNCTION 编码不同，M0/M0+、M23/M33和RISC-V不复用这套写法，会明确报不支持。

只占用 FUNCTION 为 disabled 的比较器，保留已有trace/其他调试器占用。
设置时先禁用、写 COMP/MASK、回读，再使能 FUNCTION；失败不加入本会话列表并尝试禁用。
复位后重装本会话观察点，断开时清理本会话申请的比较器。
DEMCR 按读改写保留其他位，不主动关闭全局 TRCENA，避免破坏已有trace。

命中由 DFSR.DWTTRAP 和比较器 MATCHED 提示。DWT事件可能异步于访问指令，停止PC可能
已经越过读写指令；不应把显示的PC无条件当成发生访问的那条指令。
它观察的是内核数据访问，不保证捕获DMA/其他总线主设备写内存，也不是条件表达式/值匹配功能。
模拟目标提供CPU访问模型，调试器自身的SWD读写不触发该模型。

寄存器依据：
- [CMSIS DWT 寄存器结构](https://arm-software.github.io/CMSIS_5/Core/html/structDWT__Type.html)
- [libopencm3 DWT 常量](https://libopencm3.org/docs/latest/stm32g0/html/dwt_8h.html)

## bt 展开与准确性边界

自动展开优先使用匹配 ELF 中的 `.debug_frame` CFI；无对应 CFI 时使用
`.ARM.exidx` / `.ARM.extab`，按
[Arm EHABI](https://github.com/ARM-software/abi-aa/blob/main/ehabi32/ehabi32.rst)
解释compact personality 0/1/2、整数寄存器弹栈、vsp调整及常见VFP栈大小调整。
不会在目标上执行personality函数，也不修改寄存器/内存或继续运行目标。
不支持的opcode、CANTUNWIND、坏地址、短读、SP倒退、循环均停止并保留此前得到的帧。

GCC工程可启用 `-g -funwind-tables`，链接脚本须保留ARM unwind段，并载入与板上程序
一致的最终ELF。只带 `-g` 不保证存在 `.ARM.exidx`；strip/链接丢弃也可能使其缺失。
现已支持 DWARF32 `.debug_frame` 的常见 Cortex-M 核心寄存器规则；
`.eh_frame`、CFI 表达式和非核心寄存器规则尚不支持，会说明停止原因。
局部变量、参数和帧切换参见 [栈帧与局部变量](debug-frame-locals.md)。

EHABI按函数描述稳定栈帧，不能保证任意序言/尾声中途都准确。停在函数入口时明确提示
先单步到函数体后重试；序言/尾声其他位置、优化/内联/尾调用仍可能导致缺帧或不准确。
函数名和源码行来自ELF；返回地址的符号定位用调用者指令内的地址，PC栏保留返回地址。

支持识别经典Armv7-M EXC_RETURN，恢复基本/扩展浮点异常硬件帧，处理xPSR对齐填充。
恢复跨MSP/PSP异常帧后停止并说明边界，不声称能遍历所有嵌套异常、RTOS任务或安全状态。
不恢复/显示浮点寄存器值，只跳过相应保存区域以计算返回地址。

默认栈读取边界为当前SP向上4096字节，每次读前检查边界，Ctrl+C可中断后续读取。
扫描最多读取4KB，并用ELF可执行段和已有架构调用解码器筛选返回地址（ARM直接BL；
RISC-V按现有callEndingAt能力）。旧栈值、函数指针和重复地址仍可能混入，结果不是完整
调用链；ARM间接BLX和其他未覆盖调用编码可能被漏掉。RISC-V当前仅支持这种扫描模式。

侧栏点击带源码位置的帧可跳到源码。继续、单步、复位、命令修改状态、重新载入ELF或
断开后标记快照失效，避免把旧调用栈显示成当前状态。

## 离线测试与上板清单

```sh
node tools/selftest/dbg-dwt.test.mjs
node tools/selftest/dbg-backtrace.test.mjs
node tools/selftest/dbg-watch-bt-ui.test.mjs
node tools/dev/check-syntax.mjs
node tools/dev/check-liquid.mjs
```

新增测试覆盖DWT模式/对齐/容量/占用/回滚/复位/清理，EHABI表与opcode、边界、FAULT、
取消、循环、异常帧、扫描标注，以及DOM替身下的按钮状态、源码跳转与补全。
DOM替身不能验证浏览器布局或原生WebUSB。离线测试结果应以当前检出的固件样本与测试日志为准，
不能将某次环境缺少 H743 ELF 的失败作为固定基线。

回家先在F103/M3和H743/M7上各检查：
1. 已知循环写变量：wp写模式命中，读模式不误报；再用明确的CPU读验证读模式。
2. rw模式、1/2/4/16字节范围、对齐拒绝、容量用尽、其他trace槽位不被改写。
3. 观察点与FPB断点同时存在，继续/单步正常，复位后继续命中，断开重连不残留本次观察点。
4. 三级noinline函数调用，载入保留EHABI的匹配ELF，在函数体内暂停，bt顺序/地址/源码正确。
5. 有寄存器保存、局部变量和浮点保存的函数，以及基本/扩展异常帧。
6. 缺展开表、坏SP、栈短读、函数入口、Ctrl+C，均明确停止/说明，不显示伪成功。
7. 运行/单步后旧面板失效，点击有效帧能跳到正确源码；窄侧栏和长符号名可滚动查看。
8. RISC-V上wp明确拒绝，bt明确提示自动展开不可用，bt scan不访问ARM PPB寄存器。
