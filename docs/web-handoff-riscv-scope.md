# 给网页侧的最小改动说明：J-Scope 采 RISC-V/JTAG 目标

> 适用固件：`29ad951` 及以后（HID 0x32 的 `flags bit6` / 状态字 0 `bit1` 是这一版加的）。
> 网页仓库：`web-serial-rtt-tools`（本文只描述协议，不含网页代码）。
> 固件侧权威协议：`firmware/application_5301/Custom HID Protocol.md` 第 16 条。

## 0. 一句话结论

**波形页可以一行不改就采 RISC-V 目标** —— 传输后端由**全局目标类型**决定
（HID `CMD_RTT` action 10，与 RTT-over-JTAG 用的是同一个开关）：

```
CMD_RTT(0x31)  action 10 : Byte[0x03]=10, Byte[0x04]= 0 → SWD/ARM，1 → RISC-V/JTAG
```

所以只要先把目标类型切成 RISC-V（网页 RTT 那一侧本来就有这个开关，或者直接用命令行
`python script_test/scope_hss_test.py status --riscv` 设一下），波形页照旧配置 + 启动即可。
下面的 1~4 是**建议**的打磨项，不做也能用。

## 1. 固件侧这次加了什么（三个新信息位）

| 位置 | 位 | 含义 |
| --- | --- | --- |
| 配置报文（action 7）`Byte[0x08]` | `flags bit6 = 0x40` | **强制**本次会话走 RISC-V/JTAG。不带就跟随全局目标类型 |
| DEF 包（kind=1）`flags` 字段 | `bit6` | **生效**后端是否为 RISC-V（主机拿它对账） |
| 状态字 0（`Byte[0x04..07]`） | `bit1` | **生效**后端是否为 RISC-V。丢弃模式没有 DEF 包，就靠这一位 |

⚠️ 为什么"你设的"和"生效的"可能不同：**后端拉不起来时会自动换另一条路重试一次**
（全局目标类型是粘的 —— 上次采过 RISC-V，这次采 ARM 就会先撞一次 SWD 失败）。
所以页面**显示时一律用生效值**，不要用自己下发的 flags 反推。

## 2. 建议做（按性价比排序）

1. **显示当前后端**：波形页读状态字 0 的 `bit1`（或 DEF 的 `flags bit6`），显示成
   `SWD/ARM` 或 `RISC-V/JTAG`。这一条最值 —— "我明明选了 RISC-V 怎么还是 SWD" 这类
   问题一眼就能定位。
2. **RISC-V 下把 SWD 专属的东西藏起来或置灰**：
   - action 3（设 SWD 时钟 Hz）：JTAG 下无效（探针会忽略）；
   - `flags bit4`（`DAP_Data.clock_delay=0`）：JTAG 下无效；
   - 状态字里的 `swdHz` 字段、标定响应里的 `blob 偏移/clock_delay`（字 3/4）：JTAG 下无意义；
   - 提示语里的"周期下限 2 µs"（那是 SWD 单字流水的下限）：RISC-V 单字约 **3.2 µs**、
     8 通道约 **36.1 µs**（2026-09-30 修复周期性 SBCS 检查后；修复前 4.3 / 36.6 µs），
     按 SWD 的下限给建议周期会让用户看到大面积丢拍。
3. **目标类型选择器**：如果它只在 RTT 那一页，建议波形页也放一个（或至少把当前值显示出来）。
4. **采样率提示按后端分档**（实测，HPM6800EVK / HPM6880）：

   | 配置 | SWD @60M | RISC-V/JTAG |
   | --- | --- | --- |
   | 单变量 u32 | 1.588 µs → 629 kHz | **3.15 µs → 317 kHz**（09-30 修复后；修复前 4.25 µs → 235 kHz） |
   | 8 通道（32 B 一个 span） | 11.19 µs → 89.3 kHz | **36.1 µs → 27.7 kHz**（09-30 回归；09-29 为 36.6 / 27.3） |

   RISC-V 侧"零丢拍"的周期建议取 **≥ 1.5×** 上表值（探针侧还有组帧与 USB 的开销）。

## 3. 数据面（0x83）完全没变

512 B 自描述包、DEF/DATA/STAT/EVT、变长帧、`aux` 语义、`t_us` 时间轴、序号缺口 —— 
全部与 SWD 一致，网页的解码路径**不需要任何分支**。

## 4. 两个行为差异要知道

JTAG 只有一条 TAP：**采样期间主机不能同时用 OpenOCD/pyOCD 调同一块板**（SWD 侧也一样
互斥，但 SWD 侧探针还能"让路一拍"给 DAP，JTAG 侧目前不参与让路）。
另外 RISC-V 每次采样都要占目标系统总线，高频采样可能影响目标实时性。

### 4.1 变量在可缓存区里 → 波形可能是**平的/全 0**（用户会以为功能坏了）

探针走 **SBA（系统总线访问）读内存，绕过目标的 D-cache**：目标刚写进 cache、还没写回
SRAM 的变量，探针读到的是上一次写回的值，极端情况读到全 0。这不是网页的 bug，也不是
探针能修的（J-Link 读 RISC-V 同理）。用户侧的三种解法：

1. 把要看的变量放进**非缓存区**（推荐；HPM6800EVK 上 `ATTR_PLACE_AT_NONCACHEABLE_BSS`）；
2. 目标每拍把变量的 cacheline 写回（`l1c_dc_writeback`，**地址与长度都要按 cacheline
   对齐**，HPM6880 是 64 B；写错会挂 SDK 断言 → `abort()` → `_exit()` 停车）；
3. 把该内存区域配成 write-through / write-around。

> 想在网页上给提示的话，判据很便宜：连续 N 拍所有通道都一模一样（尤其全 0）就提示
> "目标可能没在跑，或变量在可缓存区里（SBA 读不到新值）"。探针**不会**报错 ——
> DMI 应答正常，读到的确实"是内存里的值"。

### 4.2 一帧里多个变量不是同一瞬间的（撕裂）

读一个 32 B span 要 ~30 µs，期间目标可能已经更新到下一拍，于是帧内会出现"前几个变量
是上一拍、后面是下一拍"。实测撕裂率 8.9%（按节拍采样）~26.6%（满速），随机落在各个
变量之间。任何调试器都一样；想避免就让用户少选变量、或目标侧用 seqlock/双缓冲。

## 5. 怎么自测（命令行，与网页走同一根管子）

```bat
:: 探针切 SWD+JTAG 输出模式（RAM-only，掉电即失）
python script_test\hpm6800_probe.py set-mode 1

:: 采 RISC-V：单字 / 8 通道 / 端到端（会先把全局目标类型设成 RISC-V）
python script_test\scope_hss_test.py bench --riscv --set one  --addr 0x1240000 --iters 2000
python script_test\scope_hss_test.py bench --riscv --set one  --base 0x1240000 --iters 1000
python script_test\scope_hss_test.py run   --riscv --set one  --addr 0x1240024 --period 100 --secs 2

:: 采 SWD：把目标类型切回来（粘的）
python script_test\scope_hss_test.py bench --swd --set one --clock 60000000 --iters 2000
```

`run` 的输出里会直接打出生效后端（`后端=SWD/ARM` / `后端=RISC-V/JTAG`），可以拿它跟
网页显示的值对一下。
