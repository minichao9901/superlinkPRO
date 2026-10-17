# H743 CDC+ETM 固件：多种频率预编译 ELF

本目录下的 `fw-cpuNN-traceNN.elf/.bin/.hex` 是**同一份源码、不同时钟档**的构建产物，
可直接喂给网页工具（或 `tools/h743_etm.py`）做 trace。

统一条件（四个档都一样）：

- **AHB = CPU**（`RCC_HCLK_DIV1`，HPRE=/1，D1CPRE=/1）⇒ HCLK == CPU 频率
- **TRACECLK = VCO / (2×R) = 30 MHz**（来自 PLL1R；30 MHz 是这块板的实测上限，见下）
- USB 内核时钟 **PLL3Q = 48 MHz**；USB 供电 External；**TRDT 按 HCLK 查表**（已修，不再用 CPU 频率）
- 构建脚本：`build.ps1 -CoreMHz <CPU> -TraceR <R> -AhbDiv 1 -Trdt Hclk -UsbSupply External`

| 文件 | CPU | TRACECLK | HCLK(=AHB) | VCO | M / N / P / R | TRDT | 实测结论 |
|---|---:|---:|---:|---:|---|---:|---|
| `fw-cpu30-trace30.elf` | 30 MHz | 30 MHz | 30 MHz | 240 | 5 / 48 / 8 / 4 | **0x7** | ✅ 同频（CPU=TRACECLK）可解；5ms 窗 11,295 条、50ms 窗 82,109 条 |
| `fw-cpu60-trace30.elf` | 60 MHz | 30 MHz | 60 MHz | 480 | 5 / 96 / 8 / 8 | 0x6 | ✅ **推荐档**；50ms 窗 **326,109 条**，字节零丢失 |
| `fw-cpu90-trace30.elf` | 90 MHz | 30 MHz | 90 MHz | 720 | 5 / 144 / 8 / 12 | 0x6 | ✅ 可用（5ms 量级未单独跑满 50ms） |
| `fw-cpu120-trace30.elf` | 120 MHz | 30 MHz | 120 MHz | 960 | 5 / 192 / 8 / 16 | 0x6 | ✅ 5ms 窗 68,403 条，USB 栈函数全解出 |

## 用的时候要填给网页/工具的数字

- **expected CPU** = 表里的 CPU
- **expected TRACECLK** = 30 MHz（四档都一样）
- LA 采样率建议 **500 MSa/s**（30 MHz 下 16.7 点/周期）；窗口按需要 5 ms ～ 秒级
- LA 接线：`CH0=PE2=TRACECLK`、`CH1=PE3=D0`、`CH2=PE4=D1`、`CH3=PE5=D2`、`CH4=PE6=D3`

## 关键提醒

1. **TRACECLK 不要超过 30–35 MHz**：40 MHz 在本板 + 当前接线 + 500 MSa/s LA 上**所有取样相位都解不出**
   （已用"CPU480 + TRACECLK40"的密集流对照排除是解析问题，属物理上限）。
2. **CPU 越高，同样墙钟时间内抓到的指令越多**（同窗 5ms：30→11,295、60→33,848、120→68,403 条），
   但**同频档（30/30）也是完全可解、字节零丢失**的，只是同墙钟内 CPU 执行得少。
3. 换档烧录后 CDC 会重新枚举（VID `F055` / PID `7431`），约 0.6 s 后 COM 口出现。
4. 板子自检：`pwsh tools/h743/ocd.ps1 -Cmds 'mdw 0x20003000 3'` → 期望 `34524354 <sysclk> 00000001`。
5. 这四个 ELF 的 **HCLK == CPU**；若要测"AHB 分频"（如 USB TRDT 场景）用
   `build.ps1 -CoreMHz 120 -TraceR 16 -AhbDiv 4`（HCLK=30）另build。

## 重新生成

```powershell
cd E:\web-etm-trace\firmware\stm32h743_cdc_trace
$gcc = 'E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe'
foreach ($v in @(@{n='cpu30-trace30';core=30;r=4}, @{n='cpu60-trace30';core=60;r=8},
                 @{n='cpu90-trace30';core=90;r=12}, @{n='cpu120-trace30';core=120;r=16})) {
    pwsh -NoProfile -File .\build.ps1 -CoreMHz $v.core -TraceR $v.r -AhbDiv 1 -Trdt Hclk -UsbSupply External -Gcc $gcc
    Copy-Item build\fw.elf "fw-$($v.n).elf" -Force
    Copy-Item build\fw.bin "fw-$($v.n).bin" -Force
    Copy-Item build\fw.hex "fw-$($v.n).hex" -Force
}
```

> 注意：`build.ps1` 每次都会覆盖 `build/fw.*` 和目录根的 `fw.elf/fw.bin/fw.hex`（不带档位后缀的那些）。
> 带后缀的 `fw-cpuNN-traceNN.*` 是本表这四份，手改 `build.ps1` 时别把它们清掉。

## ⚠️ 变体必须与源码同步（2026-10-17 真机踩到）

**症状**：网页里载入变体 ELF 后，底部报红字

```
目标机器码与 ELF 不匹配：0x08000298
```

**原因**：变体 ELF 是**某个时刻的源码快照**。源码一改（哪怕只改一行 C），代码会整体挪位，
变体就与板子里的固件对不上——网页会逐字节比对 ELF 与目标 Flash，地址错位即报错。
本次就是这样：变体是 13:42 打的，而板子里烧的是 17:34 的 `build/fw.elf`（源码在那之后修过），
两者符号地址差 8 字节：

| 符号 | 板子里（`build/fw.elf` 17:34） | 旧变体（13:42） |
|---|---|---|
| `_init` | 0x08000AB6 | 0x08000AAE |
| `cdc_acm_init` | 0x08000818 | 0x08000828 |
| `trace_hook_entry` | **0x08000B50** | 0x08000B48 |

**怎么快速判断**（一条命令）：

```powershell
arm-none-eabi-nm build\fw.elf | Select-String ' T trace_hook_entry$'
arm-none-eabi-nm fw-cpu60-trace30.elf | Select-String ' T trace_hook_entry$'
```

两个地址不一致 ⇒ 变体过期，必须重建。

**重建（只编译、不烧录；约 4×15 秒）**：

```powershell
cd E:\web-etm-trace\firmware\stm32h743_cdc_trace
$gcc='E:/Share/env-windows/tools/gnu_gcc/arm_gcc/mingw/bin/arm-none-eabi-gcc.exe'
foreach($c in @(@(30,4),@(60,8),@(90,12),@(120,16))){
  & pwsh -NoProfile -File .\build.ps1 -CoreMHz $c[0] -TraceR $c[1] -Gcc $gcc
  Copy-Item build\fw.elf "fw-cpu$($c[0])-trace30.elf" -Force
  Copy-Item build\fw.bin "fw-cpu$($c[0])-trace30.bin" -Force
  Copy-Item build\fw.hex "fw-cpu$($c[0])-trace30.hex" -Force
}
```

> 固件**没有**嵌 `__DATE__`/`__TIME__`，所以同参数重建是**逐字节可复现**的：
> 重建出来的 `fw-cpu60-trace30.elf` 与板子里的 `build/fw.elf` sha256 完全相同（`C610192CE4ACBEFC…`）。
> 万一你只是要跑一次采集，**直接载 `build\fw.elf`** 最稳——它就是刚烧进去的那份。

### 本轮重建后的哈希（2026-10-17 20:30，源码 `aa3af67`）

| 文件 | CPU / TRACECLK | 字节 | sha256（前 16 位） |
|---|---|---|---:|
| `fw-cpu30-trace30.elf` | 30 / 30 MHz | 2,178,084 | `D4D1F17BBF0A0B84` |
| `fw-cpu60-trace30.elf` | 60 / 30 MHz | 2,178,084 | `C610192CE4ACBEFC` ← **与已烧固件相同** |
| `fw-cpu90-trace30.elf` | 90 / 30 MHz | 2,178,084 | `F81401AAA291DBA1` |
| `fw-cpu120-trace30.elf` | 120 / 30 MHz | 2,178,084 | `051C42F4EA925DD3` |

四份的 `trace_hook_entry` 都在 **0x08000B50**（代码布局相同，只有 PLL 常数不同），
所以除 `cpu60` 外，其余三份要**先烧对应固件**再用，否则同样会报不匹配。

另外记一条：本次重建时我误用了 `-TraceR 16` 给 cpu30，得到的其实是 **TRACECLK 7.5 MHz**；
正确的 cpu30 是 `-TraceR 4`（`4f/30 = 4`）。**CPU 越低，R 越小**，别按"高频档的表"照抄。

## 🚨 `.hex` 曾经是"疏"的（2026-10-17 真机定因，已修）

**症状**：网页载入 ELF 后报

```
目标机器码与 ELF 不匹配：0x08000298（重读 4 次仍不一致：ELF 00 ≠ 目标 ff）
```

**取证**（OpenOCD 独立读回，排除"网页读路径"）：

```
0x08000290: 00000000 08006449 ffffffff ffffffff
ELF 期望  : 00000000 08006449 00000000 00000000
```

即 `0x298`、`0x29C` 两个字是 **0xFFFFFFFF（擦除态）**，其余全部正确。

**根因**：`objcopy -O ihex fw.elf fw.hex` 的输出**省略整段 0x00**。原始 hex 记录：

```
:10028000 49640008 49640008 49640008 00000000    ← 0x280，16 字节
:08029000 00000000 49640008                      ← 0x290 只给 8 字节就结束
:1002A000 10F8012B 11F8013B …                    ← 直接跳到 0x2A0
```

**缺的 `0x298..0x29F` 这 8 个 `00` 与板子上缺的 8 字节完全一致** —— 所以板子是从 `.hex` 烧的。

**修法**（`build.ps1` 已改）：先出稠密 `.bin`，再由 bin 生成 hex：

```powershell
arm-none-eabi-objcopy -O binary  fw.elf fw.bin
arm-none-eabi-objcopy -I binary -O ihex --change-addresses 0x08000000 fw.bin fw.hex
```

重建后 `0x08000298` 有记录且值 = `0x00` ✓。

**经验**：
- 往 H7 烧固件**优先用 `.elf`（OpenOCD）或 `.bin`**；用 `.hex` 前先确认它是稠密的。
- 网页烧录器**务必把「校验」勾上** —— 这次若开了校验，烧完当场就会报"0x08000298 读到 0xff，期望 0x0"，不用等到 ETM 那一步才发现。
- 全 0 段落在 `.rodata`/`.data` 里时，这种缺字是**功能性损坏**（固件静默跑错），不只是校验不过。这次恰好是未用的中断向量表槽位（0 与 0xFF 等价）才没出事。
