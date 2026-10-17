# STM32H743 · 调试器压力测试靶子（`dbgstress`）

给**调试器页（`#dbg`）**做发布前压力测试用的固件（2026-10）。兄弟例程 `stm32h743_scope` 与
`stm32h743_rtt_speed` 都太"平"（一个 `main` + 一个自旋），压不出断点/单步/结构体树的毛病来。

```powershell
pwsh -File build.ps1              # DWARF 4 → build\fw.elf（默认）
pwsh -File build.ps1 -Dwarf5      # 同一份源码换成 DWARF 5 → build-dw5\fw.elf（压解析器）
node ..\..\..\tmp\dbg-flash.mjs /tools/target-firmware/stm32h743_dbgstress/build/fw.elf   # 页面 WebUSB 烧录
```

烧完在页面上跑：

```powershell
node tools/selftest/dbg-hw-stress.mjs                      # 真机压测（76 项断言）
node tmp/dbg-gdb-oracle.mjs                                # 先做一份 gdb 标准答案（可选）
node tools/selftest/dbg-hw-stress.mjs                      # 压测会拿它逐地址比对（81 项）
```

## 1. 这块固件为什么长这样

| 设计 | 为什么 |
|---|---|
| **不动时钟**（复位后就用 HSI 64 MHz） | 压测要的是**确定性**。PWR/VOS/MPU/D-Cache 那套在 scope 例程里已经踩平，这里不重复引入变量；SysTick 用处理器时钟 → 10 kHz |
| **D-Cache 关着** | 探针读到的必须是 CPU 刚写进去的值（监视窗口的对账判据）；开了就得配 MPU，那是另一个故事 |
| **11 段流水线**，每轮跑一遍 | 任何一段下断点都会**每轮必命中**；`g_stage` 一眼看出停在第几段 |
| **两个中断**（SysTick 10 kHz + PendSV 10 Hz） | 高频中断里下断点/单步；慢中断适合"进中断再跳出" |
| **多源文件**（main / engine / model / startup） | `b engine.c:NN` 这种跨文件断点才有意义 |
| 全部变量非 `static`、`.data/.bss` 落在 AXI SRAM | ELF 里要有符号；H7 上 AXI SRAM(0x24xxxxxx) 探针读得到 |

## 2. 三个源文件各压什么

* **`src/engine.c`** —— 控制流：
  * `engine_linear()`：**`optimize("O0")` 的线性语句序列**（17 句、一语句一条指令），
    是"单步语义"的对照靶子 —— 拿它跟 `arm-none-eabi-gdb` 的 `next` 逐步比对
    （实测 17 步**逐地址一致**）。
  * `engine_linear_os()`：同样的语句、默认 `-Os`（真实工程的样子）。
  * `engine_deep_chain()`：6 层嵌套调用（每层"调用后再加工"，**故意不构成尾调用**）
    → 测"单步跳出"能不能一层层爬回来。
  * `engine_rec_fib()` / `engine_rec_ack()` / `is_even`·`is_odd`：递归与**互递归**。
  * `engine_dispatch()`：**函数指针表** → 编译器生成 `blx Rn`，测"单步进入"认不认间接调用。
  * `engine_branchy()`：分支/循环/`switch`。
  * `engine_uses_inline()`：内联函数——**没有独立地址**，下断点必须明确报错而不是乱下。
* **`src/model.c`** —— 复杂数据结构：嵌套结构体、二维数组、`double` 矩阵、联合体、
  **位域（含 6 位有符号位域）**、`char[]`（有一条**故意没有 NUL 结尾**）、不可打印字节数组、
  指针链表（`node_t *head` 串起 `nodes[]`）、`const` 限定对象（住 flash）。
  位域组旁边放了 `word`/`word2` 两个"**影子字**"：位域就是按同一套位移规则从它们切出来的，
  于是"页面解出来的位域 == 影子字的对应位段"这条对账可以自动做（真机 + 自测都在用）。
* **`src/main.c`** —— 11 段流水线 + 两个中断。

## 3. 踩过的三个"固件侧"坑（都是 `-Os` + `--gc-sections` 的锅）

1. **`const` 全局对象会被整段回收**：`g_model_const` 没人引用 → 连符号都不剩，
   页面上"根本没有这个变量"。而且**随手加一句引用还会被常量折叠掉**（值在编译期已知）——
   必须**经 `volatile` 指针读**才算真引用（`model_checksum()` 里就是这么保它的）。
2. **`static` 函数会被内联掉**：`deep_l1`/`deep_l5` 是 `static`，`-Os` 直接内联 → `nm` 里找不到，
   "静态函数能不能下断点"就测不到了 → 加 `__attribute__((noinline))`。
3. **`optimize("O0")` 是函数级属性**：整个文件仍按 `-Os` 编，只让这一个函数保持"一语句一指令"。

## 4. 真机压测覆盖了什么（`tools/selftest/dbg-hw-stress.mjs`）

断点（文件:行 / 符号 / static / 符号+偏移 / 多断点 / ISR / 连续命中 5 次 / 越界与内联报错）、
代码同步（每次停下 PC → 源码高亮与行号表逐步一致、跨文件）、
单步（20 步线性序列、`n` 越过调用、`si` 进入直接/间接调用、`fin` 连爬 5 层）、
复位重跑（停在**复位向量** → 断点仍有效 → 再命中，3 轮）、
监视/结构体树（位域与影子字逐位对账、复合路径 `a.b[2].c`、指针、char[]、越界报错）、
FPB 比较器泄漏（直读 `FP_COMP`，全程"占用 == 用户断点数"）、
总线 FAULT 自愈、连续 60 轮"停—走—停"。

拿 `tmp/gdb-oracle.json`（OpenOCD + arm-none-eabi-gdb 生成）做对照时，还会逐地址比对：
`engine_linear` 内的 17 步落点、`文件:行` 断点落点、复位向量、**位域排布**（gdb 逐字段核对）。

## 5. 与 gdb 的口径差异（如实记下来）

步出函数之后落点可能不同：`stage_run()` 被内联进 `main()`，我们的"下一条语句"按**地址序**找，
gdb 的 `next` 按**行号变化**停、并且会跳过内联收尾。两者都在 `main` 里，只是停的语句不同。
函数体内（没有内联干扰的地方）**逐地址一致**。
