# STM32H743 · scope（HSS 采样）测试固件

给「J-Scope 波形页」（探针侧 HSS 采样）用的**靶子**：每个被采样的量都有**精确已知的数学波形**，
所以"采样率对不对、有没有混叠、丢了多少、有没有撕裂读"都能**客观判定**，不用靠肉眼看波形。

- 变量契约、应有值算式、ISR 更新顺序 —— **与 `../stm32f103_scope/` 逐字相同**（同一套靶子）。
- 时基：HSE 25MHz → PLL1 → SYSCLK 480MHz（CPU 240 / AXI 240）→ SysTick 每 24000 周期
  = **10 kHz**，ISR 里所有量一次更新完。
- 目标固件**不占任何外设**（只开时钟 + SysTick + 可选 D-Cache），寄存器级，不依赖 HAL/CMSIS。
- 与 F103 那份的**唯一实质差别**：H7 上"变量放哪块 RAM"变成了硬要求 —— 见第 2 节。

> 📦 目录根的 **`fw.elf` 是编好的产物**（默认 flash 版），直接在网页里载入即可；
> 改了源码就重跑 `build.ps1`（它会重新覆盖这一份）。

---

## 1. 变量契约（`t` = g_tick，10 kHz 计数）

| 变量 | 地址¹ | 类型 | 应有值 |
|---|---|---|---|
| `g_pack.f_sin` | 0x24001014 | f32 | `SIN100[t%100]/1000` —— 100 Hz 正弦 ±1.0 |
| `g_pack.f_tri` | 0x24001018 | f32 | 100 Hz 三角，峰值在 `t%100==50`（与正弦差 90°） |
| `g_pack.i_tick` | 0x2400101c | i32 | `t` |
| `g_pack.u_ramp` | 0x24001020 | u16 | `t % 1000`（100 Hz 锯齿） |
| `g_pack.i_sq1k` | 0x24001022 | i16 | `(t%10<5) ? +1000 : -1000` —— **1 kHz 方波** |
| `g_pack.u_cnt` | 0x24001024 | u8 | `t & 0xFF`（约 39 Hz 回绕） |
| `g_pack.i_saw` | 0x24001025 | i8 | `(int8_t)(t*3)` —— 8 位锯齿，步进 3 |
| `g_pack.rsv0` | 0x24001026 | u8 | 保留（只为让 `u_hi` 4 字节对齐，不采） |
| `g_pack.u_hi` | 0x24001028 | u32 | `0x10000000 \| (t & 0xFFFF)` —— **高位非零，验 u32 精度** |
| `g_lfsr` | 0x24000000 | u32 | xorshift32（宽带伪随机） |
| `g_far_cnt` | 0x24000008 | u32 | `t` |
| `g_far_sq100` | 0x2400000c | i16 | 100 Hz 方波 ±1000 |
| `g_isr_count` | 0x24001010 | u32 | `t`（与 `g_tick` 同值 → 验两个地址都对） |
| `g_pair_a` / `g_pair_b` | 0x2400102c / 0x2400102e | u16 | `t` / `~t` —— **撕裂自检**：`a^b != 0xFFFF` 即"读到两次 store 之间" |
| `g_pulse` | 0x24001030 | u8 | `(t%2000 < 100) ? 1 : 0` —— 5 Hz 脉冲、5% 占空比（**触发测试**用） |
| `g_ramp64` | 0x24001038 | f64 | `1.0 + t*1e-6`（主循环更新，验 8 字节载荷/浮点精度） |
| `g_sq5k` | 0x24001040 | i16 | `(t&1) ? +1000 : -1000` —— **5 kHz 方波：采样率 < 10 kHz 必然混叠** |
| `g_tick` | 0x24001044 | u32 | `t`（**主对齐量**） |
| `g_hole[4096]` | 0x2400000e | u8[] | 占位（不采），作用只是把地址拉开 4 KB |

¹ 本机构建实测（`build.ps1` 每次会打印一遍；换编译器/改代码后地址会变，**以 nm 为准**）。
`g_pack` 整块 **24 B**、连续（`check.py` 会断言这个大小）。

**诊断量**（不属契约，但页面/脚本可以顺手采；都在同一块 AXI SRAM 里）：

| 变量 | 地址 | 含义 |
|---|---|---|
| `g_z_dcache` | 0x24001050 | 0 = D-Cache 关，1 = 开（一眼看出烧的是哪一版） |
| `g_z_sysclk_hz` | 0x24001058 | 实际 SYSCLK（400000000 或降级的 64000000） |
| `g_z_hclk_hz` | 0x24001054 | 实际 HCLK（= SysTick 时基；200000000 / 64000000） |
| `g_z_clk_src` | 0x2400104c | 0 = HSE→PLL，1 = HSI 直出（保命档） |
| `g_z_clk_err` | 0x24001048 | 出错位或：bit0 VOSRDY / bit1 HSERDY / bit2 PLLRDY / bit3 切换 / bit7 降级 |

**更新顺序 = 契约的一部分**（主机据此放宽断言）：先 `g_tick`，再 `g_pack` 各字段（逐个 store，
**不是原子快照**），最后散落量。所以任何读到的值都可能落在"上一 tick"⇒ 断言要允许 ±1 tick。

---

## 2. 内存布局：被采样的变量**必须**在 AXI SRAM（H7 特有的硬要求）

```
0x08000000  FLASH   128 KB   向量表 + 代码 + .rodata（探针读得到，但这里没有被采样的量）
0x20000000  DTCM    128 KB   ★ 只放栈 —— 内核私有总线，AHB-AP 探针**读不到**
0x24000000  AXI SRAM 512 KB  ★ .data/.bss 全在这里（所有 g_* = 被采样的量）
```

H7 的 ITCM/DTCM 挂在**内核私有总线**上，外部调试器走 AHB-AP 够不着（本仓库
`../stm32h743_rtt_speed/README.md` 与 `../stm32h7b0_rtt_speed/RESULTS.md` 都记着这条）。
J-Scope 靶子的全部意义就是"让探针把变量读出来"，所以链接脚本把 `.data/.bss` 整个放到 AXI SRAM；
栈留在 DTCM（探针不需要读栈，而 DTCM 零等待、最省事）。

**这条要求是机器可验的**（不需要硬件）：

```powershell
arm-none-eabi-nm -S build\fw.elf | Select-String '\sg_'
# 输出里每一行都必须是 24xxxxxx：
#   24001014 00000018 B g_pack      24001044 00000004 B g_tick
#   24000000 00000004 D g_lfsr      2400000e 00001000 B g_hole
python check.py --static-only        # 会逐条断言"都在 AXI SRAM"，越界直接判失败
```

有人会问"DTCM 到底能不能被探针读到"—— 本仓库的结论是**读不到**，而这正是这份靶子要坐实的事：
如果你手上的探针/工具链真能读 DTCM，把 `ld/stm32h743.ld` 的 `.data/.bss` 改回 `> DTCM`
再采一遍，就能拿到反证（`check.py` 的静态检查会先拦下来，别硬改忘了那一步）。

### 地址分两块 → 正好对比"读计划"的两条路径

```
span A  0x24000000 .. 0x2400000e   14 B   （g_lfsr / g_far_cnt / g_far_sq100）
        ── 4 KB 空洞（g_hole 占位）──
span B  0x24001010 .. 0x24001048   56 B   （其余全部；再接 20 B 诊断量 g_z_*）
```

| 采样方案 | 每样本（按 scope 页面的时钟模型估） | 上限（模型估算） |
|---|---|---|
| 8 个字段全取 `g_pack`（**一个 span，24 B**） | ≈12.4 µs | **≈81 kHz** |
| 跨 span 取 8 个（A 14 B + B 56 B） | ≈31 µs | **≈32 kHz** |
| 朴素：8 次 `swd_read_memory()` | ≈54 µs | ≈18 kHz |

**这些是模型估算，不是实测** —— 页面里会显示它自己的估算，真值要等探针固件标定。
注意：span B 如果连 `g_z_*` 一起勾上会超过页面单个 span 的 64 B 上限，页面会**自动拆成多个 span**。

---

## 3. 编译 / 烧录 / 验收

```powershell
pwsh -File build.ps1            # 默认 flash 版（arm-none-eabi-gcc 10.3，-g3 -gdwarf-4）
pwsh -File build.ps1 -DCache    # D-Cache 干扰实验版（见第 4 节）→ build-dcache\fw.elf
pwsh -File build.ps1 -NonCache  # ★★ cache 开 + MPU 把 AXI SRAM 配成非缓存（推荐）→ build-noncache\fw.elf
pwsh -File build.ps1 -Clean

pwsh -File flash.ps1            # OpenOCD + CMSIS-DAP 烧录（写不进 flash 时见第 5 节第 5 条）
python check.py                 # ★ 客观验收：静态查地址 + halt→dump RAM→逐项核对 + 复测 10 kHz 时基
python check.py --static-only   # 只做静态检查（没有硬件也能跑）
python check.py --hold          # 跑完保持 halt（排障）
```

`check.py` 做三件事：
1. **静态**：从符号表取地址，断言每个被采样的量都在 AXI SRAM（H7 上这是硬要求）；
2. halt 住目标、dump `0x24000000` 起 16 KB，**逐个变量核对契约**（浮点容差 2e-6，整型精确比对；
   每个量允许 `t` 或 `t-1`，被 halt 打断的 `g_pair_a/b` 会报"撕裂"而不是判失败）；
3. 让目标跑 3 s 再 halt 一次，用 `Δtick` 反测时基（偏差 >3% 才判失败）。

> `check.py` 用 pyelftools 读符号表取地址 —— **改了变量名/结构体布局要同步改它**。
> pyOCD 通路没实现，但目标名是 `stm32h743xx`（想手动对拍可以用
> `pyocd commander -t stm32h743xx`）。

**怎么用它验证 scope 页面**（与 F103 那份完全一样）：
1. 页面里载入 `build/fw.elf`，勾 8 个变量（建议先勾 `g_pack` 那一组：一个 span 的快路径）；
2. 采样率定成 **30 kHz 以上**（高于 5 kHz 方波的两倍才不混叠）；
3. 对账口径：`g_tick` 斜率 ≈ 采样率；跳变 > 1 = 丢样本（跳变量应等于页面的 `dropped`）；
   按 `g_tick` 反算其余通道；`g_pair_a ^ g_pair_b != 0xFFFF` 的占比 = 撕裂率；
   把采样率降到 6 kHz 看 `g_sq5k`（5 kHz 方波）变成什么低频假波。

---

## 4. ★ 「H7 D-cache 干扰」实验（本固件的第二个用途）

### 4.0 2026-10 上板定因：两个 bug 让"变量采不到"看起来像 D-Cache 的锅

现场症状是**J-Scope 一个变量都采不到**（读回来全是初值）。上板取证（WebUSB 直读寄存器 +
HardFault 压栈现场 + 与兄弟例程 `../stm32h743_rtt_speed` 逐行对账）后定到**两个寄存器偏移错误**，
都跟"缓存策略"无关：

| # | 错在哪 | 后果 | 证据 |
|---|---|---|---|
| 1 | `PWR_CR3` 定义成 `PWR_BASE + 0x08`（那是 **CR2**；正确是 **0x0C**） | SCUEN 永远清不掉 → 硬件**静默忽略 VOS 的写** → `VOSRDY` 恒 0 → 固件走保命档降级 HSI 64MHz | 现场 `PWR_CR3=0x46`（SCUEN 还在）、`PWR_D3CR=0xC000`（VOS=11 但 VOSRDY=0）、`g_z_clk_err=0x81`；兄弟例程用 0x0C，同板 `PWR_CR3=0x42`、`PWR_D3CR=0xE000`、480MHz、`g_clk_err=0` |
| 2 | `SCB_DCISW` 定义成 `0xE000EF5C`（那是 **DCIMVAC 按地址失效**；正确是 **0xE000EF60**） | set/way 编码（0、32、64…）被当成**内存地址**送进 cache 维护引擎 → `BFSR.IMPRECISERR` + `HFSR.FORCED` → 死在 `Default_Handler`，`g_tick` 恒为 0 | 压栈现场 `PC=0x080002E8`（`str.w r7,[r2,#0xF5C]`），64MHz / 200MHz 两版时钟下都必现；改对地址后同一份代码干净跑起来 |

修完两个偏移后的**真机实测**（阿波罗 H743 + akaLinkPro；烧录走网页「烧录器」的 WebUSB 零安装，
采集走网页「J-Scope」，全程没碰 OpenOCD —— 这块板的 SRST 没接探针）：

| 版本 | `g_z_dcache` | `CCR` | MPU | `g_z_sysclk_hz` / `g_z_hclk_hz` | `g_z_clk_err` | `g_tick` |
|---|---|---|---|---|---|---|
| 默认（cache 关） | 0 | `0x60200`（DC=0） | 关 | 480000000 / 240000000 | 0 | 在走（实测 10003 Hz） |
| `-NonCache`（推荐） | 2 | `0x70200`（DC=1） | region0 = 0x24000000/512KB 非缓存 | 480000000 / 240000000 | 0 | 在走（实测 10003 Hz） |

对应的时钟寄存器现场（`-NonCache`）：`PLLCKSELR=0x52`（PLLSRC=HSE, DIVM1=5）、
`PLL1DIVR=0x10102BF`（N1=192 → VCO 960MHz、P1=/2 → **SYSCLK 480MHz**）、`RCC_CFGR=0x1b`（SWS=PLL1）、
`D1CFGR=0x848`（HPRE/D1PPRE/D1CPRE 全 /2 → AXI 240 / APB3 120 / CPU 240）、
`PWR_CR3=0x42`（SCUEN 已清）、`PWR_D3CR=0xE000`（VOS=11 且 **VOSRDY=1**）、
`ICSR/CFSR/HFSR` 全 0（无 fault）。

J-Scope 侧（`-NonCache`、8 个 `g_pack` 变量、周期 25 µs、2 s）：**79992 个样本 / 39.99 kHz**，
丢(探针 29 · USB 26 · **缺口 0**)，`g_pack` 契约零越界（`u_hi` 高位全 1、`i_sq1k` 只有 ±1000、
`f_sin` 不越界），`i_tick` 2 秒走 20005 拍（= 10 kHz ✓），span 对账一致；
探针侧标定「读一次 **14.553 µs** → 建议周期 ≥18 µs」（比 200MHz 时的 17.969 µs 快 —— 目标 HCLK
从 200 提到 240 之后，AHB-AP 读同一段内存确实更快了）。
—— **变量的值本身是对的、而且在动**；"采不到"自始至终是**固件根本没跑起来 / 时钟没生效**。

> 上板取证的可复用套路：`DHCSR`（halt/运行）→ `ICSR.VECTACTIVE`（在哪个异常里）→
> `CFSR/HFSR`（什么错）→ halt 后用 `DCRSR/DCRDR` 读 **MSP**，再 dump 栈上那 8 个字
> （R0 R1 R2 R3 R12 LR PC xPSR）→ 用 `addr2line` 把压栈 PC 翻成源码行。这一轮就是靠它
> 一眼看到"PC 落在 `str.w r7,[r2,#0xF5C]`"，直接把寄存器偏移错误钉死。

### 4.1 两个实验版本各自的预期表现

`-DCache` / `-NonCache` 编出来的固件除了 `main()` 里多下面这些，源码**一模一样**：

```c
/* -NonCache：先配 MPU 属性，再使能 cache（顺序不能反） */
mpu_noncache_axi();                 /* TEX=001 C=0 B=0 S=1 → Normal 非缓存 */
SCB_CCR |= (1u << 17);              /* I-Cache */
dcache_invalidate_all();            /* 打到 0xE000EF60 —— 地址错一格就炸总线 */
SCB_CCR |= (1u << 16);              /* D-Cache */
g_z_dcache = 2u;                    /* 让主机一眼看出烧的是哪一版 */
```

AHB-AP（探针）走的是**物理内存**，而 CPU 的写会停在 D-Cache 里（AXI SRAM 默认是
Normal / Write-Back / Write-Allocate）。于是：

| 现象 | 关 D-Cache（默认） | 开 D-Cache（-DCache） |
|---|---|---|
| `g_tick` / `g_pack` 采样值 | 每个样本都在变，与契约逐点吻合 | 可能**冻住不变**（脏行长期不驱逐），或**成片变旧** |
| `g_ramp64`（主循环每 tick 写） | 正常递增 | 最容易被 cache 兜住的量 |
| `check.py` | 全绿 | 大概率报"与 tick 对不上" |

**正确读法**：这不是 bug，是**示范**。开 cache 后"采样值变旧/卡住不变"恰恰证明
"探针读的是内存、不是 cache"这条前提；要采到真值就必须让变量落在**非缓存**内存
（或像本项目这样把 D-Cache 关掉）。
做实验时请把两版都采一遍，并顺手采 `g_z_dcache` 确认当前跑的是哪一版。
（`g_z_clk_*` 也可以一起采：它们能告诉你目标有没有真的跑到 480MHz。）

---

## 5. 坑（本固件已踩/已处理）

1. **DTCM 探针读不到** ⇒ `.data/.bss` 全放 AXI SRAM，栈留 DTCM。这是本目录与 F103 版最大的差别，
   也是"为什么不能照抄 F103 的链接脚本"的唯一原因（`nm` 可验、`check.py --static-only` 可断言）。
2. **`g_hole` 会被 gc 掉**：`-fdata-sections` + `--gc-sections` 下，没有任何引用的 4096 B 数组
   会连同"地址空洞"一起消失。现在 `main()` 里有 `g_hole[0] = 0;` 保命 —— 别删。
3. **必须 `-gdwarf-4`**：scope 页面第一版的 DWARF 解析器只吃 DWARF 4，而 GCC 11+ 默认切 5。
   本机 GCC 10.3 默认就是 4，仍然显式写死，免得工具链一升级就解析不出来。
4. **`RCC_PLLCKSELR.DIVM1` 里写的是"分频值本身"**（5 = /5），**不是"分频值-1"**；
   只有 N1/P1/Q1/R1 才是"值-1"。依据：`__HAL_RCC_PLL_CONFIG` 宏直接 `(__PLLM1__) << 4U`，
   且 `HAL_RCC_GetSysClockFreq()` 里 `pllm = (PLLCKSELR & DIVM1) >> 4` 之后**直接做除**。
   ⚠️ 顺手记一条：兄弟目录 `../stm32h7b0_rtt_speed` 的 `-Minimal` 版在这里写成了 `divm - 1`，
   于是它实际跑 350MHz 而固件自报 280MHz（HAL/SDK 版不受影响）。本目录的两份都按正确写法。
   （2026-10：那个寄存器版已随 `-Minimal`/`-SlowClock` 一起删除，本条留作历史。）
5. **只交 flash 版**（2026-10 用户定调：H743 走 flash，不做纯 RAM 运行版；原来的 `-Ram` 分支与
   `ld/stm32h743_ram.ld` 已删，需要时从 git 历史里取 —— 那份 RAM 版自带 `SCB_VTOR = g_vectors`，
   否则中断向量还从 flash 别名取，SysTick 一进中断就飞）。
   早期在本机跑 `flash.ps1` 报过 `timed out while waiting for target halted`（halt 通、AHB-AP
   读写正常、flash 全 0xFF、RDP 无保护，但 SRST 没接到探针）—— 那是**当时那条连接/复位方式**的
   问题，不是固件的问题。写不进 flash 时按这几条查：SWD 的 nRESET 有没有接、烧录器用的复位方式
   （connect-under-reset）、读保护 RDP，或者改用板子自带的下载方式（BOOT 跳线 + 串口/USB DFU）。
6. **VOS 的写会被硬件静默忽略**，除非 `PWR_CR3.SCUEN` 已经清 0。本固件**只在 SCUEN 置位时**
   才去清它，绝不整块写 `PWR_CR3` —— 无条件写供电寄存器在 H7 上是能把板子写进
   "AP 事务恒 WAIT、只能整板断电"的死状态的（H7B0 那边真踩过）。
7. **升频之前必须先把 flash 等待周期给够**（本固件 LATENCY=4 + WRHIGHFREQ=0b11）。
   厂商例程用 2、兄弟例程用 4+0b10，三种都能跑；等待周期只影响速度不影响正确性，这里取最保守的。
8. **CPU 时钟与 HCLK 故意都取 240MHz**（D1CPRE = HPRE = /2）：H7 上 CPU 时钟与 AXI/HCLK
   可以不同，而 SysTick 到底跟哪一个说法不一（ARM 说跟 processor clock，ST 的 HAL 按 HCLK 算）。
   让两者相等，10 kHz 时基就没有这个歧义 —— 反正被采样量的刷新与 AHB-AP 读只关心 HCLK。
   （兄弟例程 rtt_speed 是 CPU 480 / AXI 240；两边的 **SYSCLK 都是 480MHz**，差的是 CPU 分频。）
9. **每一步时钟等待都有上限**：任何一步失败就整档放弃、退到 HSI 64MHz 并把 `g_z_clk_err` 记下来
   （不接调试器也能知道卡在哪一步）。绝不写无上限的 `while(!(RCC->CR & PLL1RDY))` ——
   那样固件会静静卡死，主机只看到"连上了但一个数都不动"。
10. **`check.py` 里写了 `--static-only`**：没有硬件的时候也能跑（只查符号地址与 `g_pack` 布局）。
    ✅ **2026-10 已上板**（阿波罗 H743 + akaLinkPro 探针，烧录/采集全走网页）：默认版与 `-NonCache`
    版都能跑起来（240MHz HCLK、10 kHz 时基、J-Scope 8 变量零契约越界，数字见第 4.0 节）。
11. **`PWR_CR3` 的偏移是 0x0C，不是 0x08**（0x08 是 CR2）。写错的后果不是报错，而是
    **SCUEN 清不掉 → VOS 写被静默忽略 → VOSRDY 恒 0 → 整个时钟降级**（见 4.0 第 1 条）。
    权威版面：厂商 `stm32h743xx.h` 的 `PWR_TypeDef` = CR1(0x00) CSR1(0x04) CR2(0x08) **CR3(0x0C)**
    CPUCR(0x10) D3CR(0x18)；兄弟例程 `../stm32h743_rtt_speed` 用的就是 0x0C。
12. **Cortex-M7 的 cache 维护寄存器只差 4 字节，错一格就从"失效"变成"按地址清/失效"**：
    `ICIALLU=0xE000EF50 / DCIMVAC=0xEF5C / DCISW=0xEF60 / DCCMVAU=0xEF64 / DCCMVAC=0xEF68 /
    DCCSW=0xEF6C / DCCIMVAC=0xEF70 / DCCISW=0xEF74`（SCB 基址 0xE000ED00；依据厂商
    `core_cm7.h` 的 `SCB_Type` 偏移注释）。本目录曾把 `DCISW` 写成 0xEF5C，于是 set/way 编码被当成
    内存地址 → **不精确总线错误**（见 4.0 第 2 条）。改这类地址时**必须**对着厂商头文件的
    "Address offset" 注释核一遍，别凭记忆写。
