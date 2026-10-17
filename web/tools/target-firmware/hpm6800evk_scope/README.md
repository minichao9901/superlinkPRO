# hpm6800evk_scope —— HPM6800EVK（HPM6880，RISC-V/JTAG）J-Scope 靶子固件

一份**已知契约**的变量块 + 已知时基，用来验收探针的 RISC-V J-Scope 通路：
采回来的每一个字都能被反算核对，而不是"看着像波形"。

- 契约定义（8 × u32 = 32 B 各字段怎么算）：`src/main.c` 头部注释
- 探针侧后端与协议：`firmware/application_5301/Custom HID Protocol.md` 第 16 条
- 采样器与验收脚本：`script_test/scope_hss_test.py`
- **两块变量、两种用途**：契约块 `g_v`（10 kHz 更新 / 20 级查表，逐值对账用）与
  **高速平滑块 `g_v_hi`**（200 kHz 更新 / 400 级，J-Scope 上看"连续"波形用）—— 见第六节

## 一、构建 / 烧录

```powershell
# 1) 构建（HPM SDK 环境在 E:\sdk_env_v1.11.0，可用 HPM_SDK_ENV_DIR 覆盖）
pwsh -File script_test\hpm6800evk_scope\build.ps1
#    产物：build\flash_xip\output\demo.elf（SDK 统一叫 demo.elf）
#    末尾会打印 g_v / g_mchtmr_hz / g_updates 的地址 —— 填给 --base

# 2) 烧录（走探针的 CMSIS-DAP + OpenOCD，烧 ELF，不能烧 .bin）
python script_test\hpm6800_flash_target.py script_test\hpm6800evk_scope\build\flash_xip\output\demo.elf
```

⚠️ 烧录前必须让探针把 JTAG TAP 交出来，否则 OpenOCD 报
`Unsupported DTM version: -1` / `CMSIS-DAP: JTAG not supported`：

```powershell
python script_test\hpm6800_riscv.py stop        # 释放 TAP（RISC-V 引擎会一直占着它）
python script_test\hpm6800_probe.py info        # output_md 必须是 1 (SWD+JTAG)
python script_test\hpm6800_probe.py set-mode 1  # 若被切成了 0（SWD-only）
```

## 二、验收（探针侧）

```powershell
# 变量块地址以构建输出为准（当前 = 0x01240000；换编译器/改代码都会变，不要硬编码）
python script_test/scope_hss_test.py run --riscv --set rv --base 0x01240000 --period 200 --secs 2
```

期望结果（实测）：

```
—— 靶子契约核对（逐字段由同一拍 g_tick 反算；允许读期间靶子推进一拍）——
  u_hi     10320/10320 与 tick 或 tick+1 相符
  f_sin    10320/10320 与 tick 或 tick+1 相符
  ...
  lfsr     10319/10319 逐拍步进正确
  契约核对：PASS
```

其它有用的档：

| 命令 | 用途 | 实测 |
| --- | --- | --- |
| `--set rv --period 200` | 按节拍采样，逐字段契约核对 | 5.1 kHz，PASS |
| `--set rv --period 1` | 8 变量 32 B span 的真实上限 | 26.1 kHz，PASS |
| `--set one --addr 0x01240000 --period 1` | 单变量快路径（hold/pipe） | **235 kHz** |
| `--dump 20` | 打印样本值，人工看 | — |
| `python script_test/hpm6800_riscv.py sbastat` | SBCS 实值 + SBA sticky 错误计数（必须为 0） | 0 |

> ⚠️ 如果 `sbastat` 报 `sbbusy` 挂着（不是 0），说明 SBA 被"没有响应的地址"挂死了
> （例如有人读了 0x40000000 —— 这块板没挂 SDRAM）。清错/复位 DM/复位 TAP 都解不开，
> **只能复位目标**（重新烧录或断电重上电）。

## 三、两个必须知道的坑（都踩过，别再踩）

### 1. 变量必须放**非缓存区**，否则探针读到的永远是旧值

HPM6800EVK 的地址属性由 `board_init_pmp()` 用 PMP+PMA 配好：

| 区域 | 属性 |
| --- | --- |
| `0x01200000`~`0x0123FFFF`（`AXI_SRAM`） | 写回缓存（WB/WA） |
| `0x01240000`~`0x0127FFFF`（`AXI_SRAM_NONCACHEABLE`） | `MEM_TYPE_MEM_NON_CACHE_BUF` |

探针读目标内存走 **SBA（系统总线访问）**，**绕过 CPU 的 D-cache**。变量放在可缓存区
时，只要那一行还待在 cache 里没被逐出，SRAM 里就一直是旧值 —— 实测探针读回**全 0**，
而同一时刻 OpenOCD 经 CPU（progbuf）读到的是活值。**这是 SBA 类调试器的固有属性**
（J-Link 读 RISC-V 同理），探针侧修不了。

本固件的做法：契约变量块用 `ATTR_PLACE_AT_NONCACHEABLE_BSS`（`.noncacheable.bss`）。
同时留了一份**对照样本** `g_v_cached`（0x012001d8，可缓存区、不写回）—— 它读出来
是停在旧值的，专门用来把这条限制钉成可复现的证据。

替代做法：每拍把变量的 cacheline 写回。⚠️ `l1c_dc_writeback(addr, size)` 在 SDK 里带
断言：**地址和长度都必须是 cacheline（HPM6880 = 64 B）的整数倍**。写成
`l1c_dc_writeback(0x12001d8, 32)` 会挂断言 → `abort()` → `exit()` → `_exit()` →
ecall → SDK 的 `syscall_handler` 是空实现、`mepc += 4` → 回到 `_exit+8` 的 `j .`
**自旋停车**（实测 PC=0x8000c78e、mcause=0x0B、mepc=0x8000c776，靶子只跑 1 拍就停住，
现场看起来像"探针读值冻结"，其实是靶子自己停了）。

### 2. 一帧里的 8 个字不是同一瞬间的（撕裂），这是正常的

探针读一整个 32 B span 要 ~30 µs，而靶子每 100 µs 更新一次 —— 偶尔会在读的过程中
推进到下一拍。于是帧内会出现"前 k 个字是 tick 的值、后面是 tick+1 的值"。
**这是目标侧的非原子更新，任何调试器都一样**，不是探针读错。验收判据因此写成：

- 每个字必须等于 `tick` 或 `tick+1` 推出来的值（不允许第三种取值）；
- 这些取值必须随字偏移**单调**（先 tick 后 tick+1）—— 顺带证明确实按地址顺序读、
  没有重排/错位；
- LFSR 必须是那条唯一的 32 位序列（逐拍步进核对）—— 错位、串值、重排都过不去。

实测撕裂率：按节拍采样 8.9%~26.6%，满速 17.6%，撕裂点分布横跨各个字。

## 四、异常自报（这个靶子为什么"不会静默死掉"）

`src/main.c` 覆盖了 SDK 的 weak `exception_handler`：把
`cause / epc / mtval / tick` 记到非缓存区（`g_trap_*`，探针和 OpenOCD 都能直接读）
然后停在原地。SDK 默认实现是 `return epc`，会把出错指令**无限重试** —— 表现出来
就是"靶子不动了"，没有任何线索（本次排查在这一点上耗了很久）。

读取现场：

```powershell
python script_test/hpm6800_riscv.py rcheck 0x01240030 1   # g_trap_count
python script_test/hpm6800_riscv.py rcheck 0x01240034 1   # g_trap_cause（0 = 没出过异常）
python script_test/hpm6800_riscv.py rcheck 0x01240028 1   # g_block_magic 应当 = 0x53434F50
```

## 五、符号表（当前构建）

| 符号 | 地址 | 说明 |
| --- | --- | --- |
| `g_v` | `0x01240000` | 契约变量块（32 B，→ `--set rv --base`） |
| `g_v_hi` | `0x01240020` | **高速平滑块**（8 B：`tick` + `f_sin`，见第六节） |
| `g_mchtmr_hz` | `0x01240028` | 靶子实测 MCHTMR 频率（= 0x016E3600 = 24 MHz） |
| `g_updates` | `0x0124002c` | 更新次数（tick 的镜像） |
| `g_block_magic` / `g_block_addr` | `0x01240030` / `0x01240034` | `'SCOP'` / `&g_v`，自描述头 |
| `g_trap_*` | `0x01240038`..`0x01240048` | 异常现场（count/cause/epc/mtval/tick） |
| `g_v_cached` | `0x012001d8` | **可缓存对照样本**（不写回 → SBA 读到旧值） |
| `g_updates_cached` | `0x012001f8` | 同上 |

（地址随构建变化，**不要硬编码**，以 `build.ps1` 末尾的 `nm` 输出为准。）

## 六、高速平滑块 `g_v_hi`（为什么另加一块）

### 现象：采样率明明够，波形却是"台阶"

契约块是 **10 kHz 更新 + 20 点查表**：

```c
const uint32_t step = hz / 10000U;   /* 100 µs 一拍 */
float f_sin = kSin20[p20];           /* 500 Hz 正弦：一个周期只有 20 个点 */
```

⇒ 一个 500 Hz 周期（2 ms）里只有 **20 个不同的值**，每级台阶持续 **100 µs**。
用 100 kHz 采样时，一个台阶内部 **10 个采样点取到同一个值**，J-Scope 上看到的就是
"台阶 / 点很少"——**既不是采样率不够，也不是探针丢点**，而是信号本身就长这样
（这属于契约的一部分；`i_sq5k` 更是故意留的混叠坑）。

**判据（一眼分清是谁的锅）**：把 `g_v.tick` 和 `g_v.f_sin` 一起勾上 ——
台阶宽 100 µs、每个台阶内 10 个采样点、`tick` 每台阶 +1 ⇒ 目标更新率就是 10 kHz；
若 `tick` 仍每台阶 +1 而台阶宽度随"周期"设置变化，问题才在采样侧。

### 对策：另开一块"更新率 ≫ 采样率"的变量

| | 契约块 `g_v` | 高速平滑块 `g_v_hi` |
| --- | --- | --- |
| 更新率 | 10 kHz（100 µs 一拍） | **200 kHz（5 µs 一拍）** |
| 正弦表 | `kSin20[20]` | `kSinHi[400]`（`tools/dev/gen-sin-table.py` 生成） |
| 一个 500 Hz 周期内不同值 | **20 个** | **400 个** |
| 100 kHz 采样时 | 每级台阶 10 个采样点相同 | **每个采样点都是新值** → 连续曲线 |

- 字段：`g_v_hi.tick`（u32，+0，200 kHz 计数）· `g_v_hi.f_sin`（f32，+4，500 Hz 平滑正弦）
- 频率 = 更新率 ÷ 表长 = 200000 ÷ 400 = **500.00 Hz**
  （MCHTMR 24 MHz ⇒ `hi_step` = 120，整除无累积误差；`while` 补齐欠账，不受循环抖动影响）
- **契约块一个字节都没动**：`scope_hss_test.py` 那套逐字段反算照旧全过
- 判据：`g_v_hi.tick` 在采样相邻两点的差 ≈ 采样率 ÷ 200 kHz（100 kHz 采样 ⇒ ≈ 2）；
  台阶占比 ≈ 0%（每个采样点都不同）
- 改了表长或更新率必须同步改：`f = HI_UPDATE_HZ / SIN_HI_N`，然后重跑
  `python tools/dev/gen-sin-table.py`（`--check` 能校验仓库里那份没被手改）

