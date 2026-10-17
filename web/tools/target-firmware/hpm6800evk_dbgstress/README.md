# HPM6800EVK 调试器压力测试靶子（RISC-V RV32）

给「调试器」页（`#dbg`）的 **RISC-V/JTAG 后端**用的靶子固件：一颗流水线式"复合负载"，
专门把调试器的各个动作（断点 / 单步 / 复位重跑 / 结构体与位域 / 触发器管理）逼到真实场景。

与 ARM 那份（`tools/target-firmware/stm32h743_dbgstress/`）**逐条对应**，便于两边对照：

| 层次 | 文件 | 内容 |
|---|---|---|
| 控制流 / 调用链 | `src/engine.c` | 叶子函数、`-O0` 的线性语句序列（行号↔地址一一对应）、6 层嵌套调用（`deep_l5` 是 `static`）、分支密集函数、递归斐波那契 |
| 数据结构 | `src/model.c` | 大结构体 `g_model`（位域 + 影子字 + 联合体 + 数组 + 二维网格 + 指针 + 字符串 + 字节数组）、`const` 版本 `g_model_const`（住 flash） |
| 主干 | `src/main.c` | 11 段流水线（MCHTMR 10 kHz 节拍）、trap 现场捕获（`g_trap_*`） |

## 变量为什么放在 `0x01240000`

探针走 **SBA** 读内存，**绕过 D-cache**。变量若落在可缓存区，读回来会是旧值甚至全 0，
看起来像"调试器坏了"。所以所有被观察的变量都放 `.noncacheable.bss`
（`ATTR_PLACE_AT_NONCACHEABLE_BSS*`，HPM6800EVK 上是 AXI SRAM `0x01240000` 起）。

## 构建 / 烧录

```powershell
# 构建（需要 E:\sdk_env_v1.11.0 里的 hpm-sdk + rv32 工具链）
cd tools\target-firmware\hpm6800evk_dbgstress
pwsh -File build.ps1              # → build\flash_xip\output\demo.elf，并拷成目录下的 fw.elf

# 烧录：直接用页面的「烧录器」页（WebUSB，零安装）
#   芯片选 hpm6800evk、文件选本目录的 fw.elf；RISC-V 路径的完成判据是日志里的「烧写 OK / 校验 OK」
```

## 验收（真机压力测试）

```powershell
node tmp\probe-free.mjs --blank        # 先把探针从浏览器手里放开
node tools\selftest\dbg-hw-riscv.mjs   # 57 项断言（等价 make test-dbg-riscv）
```

想跟 **gdb** 逐地址对账（用户要求的"跟 gdb 对比"）：

```powershell
node tmp\rv-gdb-oracle.mjs                       # OpenOCD + riscv32 gdb 生成 tmp/rv-gdb-oracle.json
node tools\selftest\dbg-hw-riscv.mjs             # 套件自动读它，多 3 项对照断言
```

对照口径（三条，都是"同一条 ELF、同一颗核、同一颗探针"）：

1. **指令级单步 19 步逐地址一致**（我们 `s` ↔ gdb `stepi`）；
2. **`文件:行` 断点落点一致**（比的是 DWARF 行号表里那一行的地址；gdb 的 `break 文件:行`
   会跳过函数序言，属另一套语义，脚本里另存为 `bpAddrPrologue`）；
3. **位域排布独立核对**（把 gdb 读到的影子字按页面的移位规则切一遍，必须与 gdb 单读的
   字段值逐个相同）。

## 这份靶子抓出来的真问题（都已修，回归钉在自测里）

| # | 现象 | 根因 | 修在哪 |
|---|---|---|---|
| 1 | 4 个断点只有 1 个会命中；单步"按了没反应" | `_bpAt()` 用**有符号**掩码算地址，`0x8000xxxx` 永远匹配不上 → 从不摘触发器 | `app/dbg/session.js`（统一 `align2()`） |
| 2 | `si` 停在原地、进不去被调函数 | `_readHalfword()` 走的是 **ARM 的 AHB-AP**（JTAG 链路上没有 AP → FAULT） | 改走 `_codeBytes()`（后端自己的读 + ELF 兜底） |
| 3 | 单步偶尔"成功但 PC 没动" | `resumereq` 之后 DM 短时间内仍报 halted → 陈旧读数被当成"已停下" | `_waitResumed()`（跑起来再等停） |
| 4 | 4 个断点命中后**继续**又停回原地 | 同上（在核还没动时就把触发器装回去、把 step 清掉） | 同上 |
| 5 | `fin` 在最内层要"走 400 条指令"还走不出去 | `callEndingAt()` 认 `c.jal` 的**判据写成了整字相等**（`hw === 0x2001`），几乎认不出任何 `c.jal` | `app/dbg/rv.js`（掩码 `0xe003`） |
| 6 | `reset halt` 之后断点全部失效 | **触发器是 hart 的 CSR，hart 复位就没了**；页面断点表还留着 | `resetHalt()/resetRun()` 复位后重新下发 |
| 7 | `p g_model_const`（flash 里的 const）读不出来/极慢 | SBA 读 XIP 窗口（`0x8000_0000` 起）会**超时 5.3 s 并把 DM 打乱** | 该窗口直接用 ELF 只读段（`memRead`），并写明日志 |
| 8 | 偶发"连不上探针"（响应回显 0x3 ≠ 命令 0x0） | 上一个会话留下的陈旧 IN 包 | `connect()` 重开探针（最多 3 次） |

另有一条**产品级边界**写在这里备查：`dm.init()`（DM 复位）会把核放开跑 —— 调试会话里
"原来停着"的话，重新初始化之后必须再停一次，否则后续抽象命令全是 `cmderr=4`
（`app/dbg/riscv.js` 的 `memRead` 里已处理）。
