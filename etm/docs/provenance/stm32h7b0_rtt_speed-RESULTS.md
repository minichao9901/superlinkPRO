# STM32H7B0 RTT 饱和吞吐测试 —— 实测结果与踩坑记录

> ⚠️ **2026-10 更新：本目录现在只有 HAL/SDK 版一种构建，"寄存器版（`-Minimal` / `-SlowClock`）"
> 已删除**（用户定调：双轨各自漂过，维护成本大于收益）。下面正文里提到"寄存器版"的地方
> 是**当时的实测记录**，保留作历史；第 152 节的"两种版本怎么选"已改成"只剩一种"。

目标板：**STM32H7B0VBT6 KIT**（板载 25MHz 晶振，128KB Flash 单 Bank）
主机通路：① 网页 WebUSB（CMSIS-DAP v2）② 桥 + OpenOCD（Tcl RPC）
固件：本目录（`build.ps1` 只编 **HAL/SDK 版**；寄存器版已于 2026-10 删除）

---

## 一、实测数字

| 通路 | 吞吐 | 轮询 | 目标侧对账 | 备注 |
|---|---|---|---|---|
| **桥 + OpenOCD**（SWD 4 MHz） | **104.1 KB/s** | 26.0 次/秒 × 4095 B | **100.0%** | 本目录固件（HAL 版），280MHz |
| 桥 + OpenOCD（SWD 1.8 MHz） | 80.7 KB/s | 20.2 次/秒 × 4095 B | **100.0%** | 寄存器版固件 |
| 网页 WebUSB | ⚠️ 未通过 | — | — | 见第五节：块读错位未解决 |
| 参考：F103 @8MHz（WebUSB） | 259.5 KB/s | — | — | 老基线，对比用 |

桥通路的瓶颈是 **OpenOCD 的 Tcl RPC 轮询节奏**（26 次/秒），不是目标也不是探针：
每次读 4095 B 要等一次 RPC 往返。想更快就把 `bridge.config.json` 里 `stm32h7b0` 的
`speed` 往上调（本次用 4000 kHz），或走 WebUSB（同一探针往返只要 ~0.3 ms）。

**目标侧自证**：固件里 `g_bytes` 是目标实际写出去的字节数，
`g_bytes` 的增量与主机读到的字节数**完全相等（100.0%）**，
说明 RTT 确实处于"目标被主机读取速度卡住"的饱和状态（`BLOCK_IF_FIFO_FULL`），
读到的 B/s 就是这条通路的真实吞吐。

### 目标状态核对（OpenOCD dump DTCM 后解码，见 `tmp/h7verify.mjs`）

```
g_sysclk_hz = 280000000      ← HAL 的 SystemCoreClock，实际生效频率
g_clk_src   = 0              ← 0 = HSE 25MHz→PLL（正常路径）
_SEGGER_RTT = "SEGGER RTT"   ← 控制块在位，2 上行 / 1 下行缓冲
aUp[0]      = size 4096, WrOff 4095 / RdOff 0   ← 写满阻塞 = 饱和
g_ms        持续递增          ← 目标活着（卡死时它不涨）
```

---

## 二、可复现的测速步骤

```powershell
# 1) 编译（只有 HAL/SDK 版；时钟配置 = 板子 demo 的 SystemClock_Config 原文）
pwsh -File tools\target-firmware\stm32h7b0_rtt_speed\build.ps1
#    （早先的 -Minimal / -SlowClock 两个开关已随"寄存器版"一起删除 —— 见文件头与第四节）

# 2) 烧录（OpenOCD 兜底；也可以直接用网页「烧录器」零安装烧本目录的 fw.elf）
pwsh -File tools\target-firmware\stm32h7b0_rtt_speed\flash.ps1
#    🚨 烧完确认板子 BOOT0 处于**正常启动(0)**，否则每次复位都回 ROM bootloader，应用不跑

# 3) 测速 A：走桥 + OpenOCD（不需要点任何弹窗）
node bridge\rtt-bridge.mjs --target stm32h7b0        # 一个终端里常驻
$env:ELF='tools\target-firmware\stm32h7b0_rtt_speed\build\fw.elf'
node tools\selftest\rtt-speed.mjs bridge 8           # 另一个终端里跑

# 4) 测速 B：走网页 WebUSB
pwsh -File tools\selftest\launch-browser.ps1 -Url "http://127.0.0.1:8899/index.html?backend=webusb&auto=1&addr=0x20000088#rtt"
node tools\selftest\rtt-speed.mjs webusb 8
```

**测速前必须确认探针没被别人占着**：OpenOCD / pyOCD / J-Link / 另一个浏览器窗口 /
**任何还在后台跑的轮询脚本**，任意一个开着，网页就 `claimInterface` 失败
（页面会提示"是不是还开着"）。
🚨 两个亲测坑：
1. 杀掉桥的 node 进程**不会**连带杀掉它派生的 OpenOCD —— 孤儿 OpenOCD 会一直占着探针。
   用 `make bridge-stop` 或 `Get-Process openocd | Stop-Process` 清掉。
2. **本机最容易忽略的是自己**：我有个 900 秒的"抢窗口"后台脚本一直在轮询探针（16 次/秒），
   于是网页每次连都 `claimInterface` 失败，表现成"第一次必失败、偶尔能连上" ——
   查占用时别忘了 `job_list` / `Get-Process node` 看一眼自己起了什么。

---

## 三、坑（这次现场踩的，按严重程度排）

### 1. 🚨 无条件写 `PWR_CR3` 会把板子带进"AP 永不应答"的死状态

最惨的一个。照抄 ST 的 `HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY)` 时，
把 HAL 里**"供电配置已锁定/已是目标值就直接返回"**的判断丢了，改成无条件写 ——
后果：

```
写 PWR_CR3 → ACTVOSRDY 永不置位（实测固件卡在等它的循环里，PC=0x08000110）
           → 目标内部时钟域停摆
           → SWD 的 DP 还能读 IDCODE（它由探针的 SWCLK 直接驱动！）
             但**所有 AP 事务恒为 WAIT/FAULT**
           → OpenOCD/pyOCD/裸 DAP 全部失效，只有**整板断电**能恢复
```

**判据**（记住这个组合，能一眼认出）：`DP IDCODE 读得到 + 所有 AP 恒 WAIT/FAULT`。
DP 是探针时钟驱动的，所以"DP 好"只证明探针和 SWCLK/SWDIO 线好，
**AP 才是目标内部时钟的证人**。

处理：寄存器版已改为**只读不写** `PWR_CR3`（不是 LDO 只记标志位，不替硬件做决定）；
HAL 版天然安全（HAL 自带判断）。另外给寄存器版加了保险：
**VOS0 没生效就绝不尝试 280MHz**，直接退 64MHz。

### 2. 🚨 RM0455(H7A3/B3/B0) 的寄存器布局和 RM0433(H743) 不一样

照 H743 抄会静默出错（写进去不报错，就是不生效）：

| 寄存器 | H743 写法 | **H7B0 实际** |
|---|---|---|
| `RCC_PLLCKSELR` | `PLLSRC[23:22]`、`DIVM1[5:0]` | **`PLLSRC[1:0]`、`DIVM1[9:4]`** |
| `RCC_APB4ENR` | 偏移 `0x6C` | **偏移 `0xF4`** |
| `PLLCFGR` | `VCOSEL=1` 是宽量程 | **`VCOSEL=0` 才是宽量程**（192~836MHz） |
| PWR | `D3CR` + `SYSCFG_PWRCR.ODEN` | **`SRDCR`**，且**没有** ODEN 这个开关 |

第一版按 H743 写 → `PLLCKSELR=7` 被硬件解释成 `PLLSRC=HSE`（板子没接 HSE 就是没输入）
+ `DIVM1=1` → PLL 永远锁不上 → 固件死在等 `PLL1RDY`。
**校验方法**：和板子自带 SDK 的 `stm32h7b0xx.h` 逐条对（本仓库 `sdk\Drivers\CMSIS\...` 里就有一份）。

### 3. 块读跨 1KB 边界会读到页首数据（TAR 自增回绕）

本探针（akaLinkPro 0D28:0204）的 AHB-AP **TAR 自增在 1KB 边界回绕**
（早期在 MicroLink 上观察到的是 4KB）。不重设 TAR 的后果极隐蔽：

```
读 1024 字节 @0x200001e8 → 前 536 字节正确，之后全是 0x20000000 起的数据
→ 正好把 RTT 控制块签名 "SEGGER RTT" 混进来
→ 上层"错位读防护"判为脏数据、整段丢弃 → readUp() 一直返回 0 字节
   现象："探针连上了、控制块也找得到，就是一个字节都读不到"
```

已在 `app\rtt\dap-webusb.js` 的读/写两条路径上改为**每个 1KB 边界都重设 TAR**。
⚠️ **待验证**：改完后块读仍偶发含签名（见第五节）。

### 4. WebUSB 授权是"每浏览器 profile、每设备"的

- 换探针（换了 VID/PID）→ 必须重新在设备选择框里点一次；已授权过的设备不用点
- 探针被别的程序占着时，**选择框里根本不会列出它**（报 "No device selected"），
  不是驱动问题 —— 先确认 OpenOCD/pyOCD 都退了

### 5. BOOT0 高电平时的调试器自救

烧录后忘了把 BOOT0 拨回、或想知道 ROM bootloader 下链路好不好使时：

```
DP IDCODE 读得到、AP0 IDR/CSW 正常（AP1/AP2/AP3 FAULT 是正常的，那三个没实现）
→ 说明 SWD 链路好，可以直接烧
```

⚠️ BOOT0 还高时，**任何一次复位都会掉回 ROM bootloader**，应用就停了 ——
用调试器把 PC/SP 指到固件入口能临时跑起来（`reg pc 0x080002c8; reg sp 0x20020000; resume`），
但下次上电还是 boot。**长期使用务必把 BOOT0 拨回 0**。

### 6. 探针由目标板供电时，板子断电 = 探针也掉线

轮询抢窗口的脚本不能一直握着 USB 句柄（会 `Errno 19 No such device` 崩掉），
必须每轮重新 `find + claim`。

---

## 四、固件版本（2026-10 起只剩一种）

| | HAL/SDK 版（唯一） |
|---|---|
| 时钟配置 | **板子 demo 的 `SystemClock_Config()` 原文**（280MHz / VOS0） |
| 依赖 | 要带 `sdk\` 目录（HAL+CMSIS，~4MB，51 文件） |
| 体积 | 8 KB |
| 降级保护 | 无（HAL 失败直接 `Error_Handler` 停机）—— 要"保命档"就改 `sdk\Core\Src\main.c` 的 `SystemClock_Config()` |
| 说明 | 早先的"寄存器版（`-Minimal`/`-SlowClock`，零依赖、1.6KB、三级降级）"**已于 2026-10 删除**：它和 HAL 版双轨，两边各自漂过（DIVM1 写成"值-1"→ 350MHz 却自报 280MHz；`PWR_CR3` 偏移写成 0x08 → 读到 CR2），收益不抵维护成本 |

`sdk\` 目录是从板子自带 SDK 拷来的（`<板子SDK>\SDK\DEMO\USART`），只保留了编译需要的
HAL/CMSIS 文件，**改动只有 `Core\Src\main.c`**：主循环换成 RTT 灌流 + 加几个对账用的全局量，
时钟配置一字未改。启动文件用 `src\startup.c`（C 写，多了 `SystemInit()` 调用）。

---

## 五、未完成 / 待办

1. **WebUSB 通路的吞吐数字还没拿到**（这是本次唯一没闭环的目标）。
   现状：探针能连（`akaLinkPro CMSIS-DAP · 512B/包 · SWD 1000kHz`）、
   控制块能定位（`0x20000088，上行缓冲 4096 B`），
   但块读（>512B）仍偶发读到含 `SEGGER RTT` 签名的错位数据 → `readUp()` 返回 0 字节。
   已按第 3 条改了 TAR 重设策略，**改完只测到"部分正确"，还没拿到干净曲线**。
   下一步建议：先用 `tmp\blockscan.mjs` 把"哪种长度、哪个地址区间"会错位测清楚，
   再决定是继续在页面里补 TAR 重设，还是改成"先按 ≥1KB 校验块首、坏块整段重读"。
2. 网页"新开页面后第一次连接"的失败已定位为**孤儿 OpenOCD 占着探针**
   （见第二节末尾）。体验上值得改：`claimInterface` 失败时自动重试 2~3 次再报错。
3. F103 路径需要在探针接回来时复验一次（本轮 `DP_PWRUP = 0x70000000` 改动只在本机 H7B0 上验证过）。
