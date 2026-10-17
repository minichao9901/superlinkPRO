# web-etm-trace 工程说明（给 Claude Code 的约定）

## 一键构建 + 烧录（Windows，Git Bash / MSYS make，在 `firmware/` 下执行）

```bash
cd E:/web-etm-trace/firmware
make flash-f103    # F103ZE 目标板：构建 build/ze，OpenOCD 烧录并校验
make flash-h7b0    # STM32H7B0 CPU 70MHz：构建 build/70，烧录并校验
make flash-h743    # STM32H743 CPU 480MHz：构建 build/，烧录并校验
```

只构建不烧录：`make build-f103` / `make build-h7b0` / `make build-h743`。

每条命令的流程：pwsh 7 调该工程的 `build.ps1` → OpenOCD（DAPLink / CMSIS-DAP，SWD 1000 kHz）→
软复位清 LOCKUP → 屏蔽 SysTick/NVIC → `flash write_image erase` → `verify_image` → `reset run`。
校验没有输出 `verified` 就判失败。

## 关键文件

| 文件 | 作用 |
|---|---|
| `firmware/Makefile` | 三条 `flash-*` 目标，依赖对应的 `build-*` |
| `firmware/tools/run_pwsh.sh` | 用 pwsh 7 绝对路径运行 `.ps1` |
| `firmware/tools/build_{f103,h7b0,h743}.ps1` | 设好 gcc 路径后调该工程的 `build.ps1` |
| `firmware/tools/ocd_flash.sh` | OpenOCD 烧录 + 校验（参数：target cfg、ELF） |
| `firmware/tools/BuildTools.psm1` | `Invoke-Tool`：统一调用 gcc / objcopy / size / nm / objdump |
| `firmware/<工程>/build.ps1` | 各工程构建脚本（已全部改为 `Invoke-Tool`） |

OpenOCD 与 gcc 的本机路径：

- OpenOCD：`E:\Share\env-windows\rtt-debugger-support-package-0.4.0\bin\openocd.exe`（脚本在同目录 `scripts/`）
- arm-none-eabi-gcc：`E:\Share\env-windows\tools\gnu_gcc\arm_gcc\mingw\bin\arm-none-eabi-gcc.exe`
- pwsh 7：`C:\Program Files\PowerShell\7\pwsh.exe`

## 踩过的坑（修改构建/烧录前必读）

1. **pwsh 7 里 `& <exe>` 报 "无法在管道中间执行"**：对 `arm-none-eabi-gcc.exe` 和 `pwsh` 都会出现，与路径写法（斜杠/反斜杠）无关。
   解决：统一用 `Start-Process`（见 `BuildTools.psm1`）。
2. **`Start-Process -ArgumentList` 不转义参数内部的双引号**：`-DRTT_CB_ATTR=__attribute__((section(".rtt_cb"),...))`
   会被拆坏，报 `expected expression before '.' token`。`Invoke-Tool` 已手动转义（`" → \"`）。
3. **bash 里改的 `PATH` 传不到 Windows 的 pwsh**：不要靠 PATH 找 pwsh/gcc，用绝对路径。
4. **Windows PowerShell 5.1 按 GBK 读 UTF-8 无 BOM 的 `.ps1`** 会解析失败（`'(未定义)'` 之类的中文引号）。
   只用 pwsh 7 运行构建脚本（`powershell.exe` 不要用）。
5. **`tools/prep.ps1` 末尾有 `exit 0`**，不能 dot-source，也不能在 bash 环境里起子 pwsh 调用。
   启动文件已是补丁状态（残留弱别名 = 0），所以 `build.ps1` 不再调用它；需要重新打补丁时手动运行。
6. **OpenOCD 是 Windows 程序，ELF 路径必须是 `E:/...` 形式**。`/e/...` 会报 `couldn't open`。
   `ocd_flash.sh` 已用 `cygpath -m` 转换。
7. **bash 里 `/tmp` 与 Windows 进程不互通**：日志写到项目目录（如 `firmware/build_f103.log`），不要写 `/tmp`。
8. **pyocd 的烧录在 F103ZE / H7B0 上多次出现"擦除成功、写入不生效"**（`PROGRAM OK` 但回读全 `0xFF`）。
   当前结论：改用 OpenOCD + 先软复位清 LOCKUP + 屏蔽中断，烧录后 `verify_image` 校验才算数。
   **不要信 pyocd 的 `PROGRAM OK`，必须回读比对。**
9. **探针 USB 通信会中途卡死**（`WriteFile: (0x00000057)` / `unable to find a matching CMSIS-DAP device`）。
   处理：物理拔插探针 USB，再重跑 `make`。已多次验证有效。
10. **目标板 USB 无 COM 口**：先看 `make` 是否通过；烧录后 USB 不枚举，先读 `g_clock_err` / `boot_stage`，不要直接判定固件坏。

## 固件工程现状（2026-10-12）

| 目标 | 构建 | OpenOCD 烧录+校验 | 备注 |
|---|---|---|---|
| F103ZE（`stm32f103_cdc_trace`，`-Chip ze`） | 通过（text 19956） | 通过（`verified 20092 bytes`） | CDC 枚举后 COM 口待确认 |
| STM32H7B0（`stm32h7b0_cdc_trace`，`-CoreMHz 70`） | 通过（text 31732，与历史 fw-70 一致） | **未通过**：探针 USB 卡死，需拔插 | 拔插后重跑 `make flash-h7b0` |
| STM32H743（`stm32h743_cdc_trace`，480MHz） | 通过（text 31168） | **未通过**：找不到 CMSIS-DAP 设备 | 拔插后重跑 `make flash-h743` |

H7B0 的历史结论（已验证）：`fw-70` 的 USB 失败根因是 `system_clock_init` 里
`HAL_RCCEx_GetPeriphCLKFreq(RCC_PERIPHCLK_USB)` 对 USB 恒返回 0，检查条件恒为假，`g_clock_err |= 32` 导致 `boot_fail` 死循环。
已改为 `usb_clock_is_48mhz()`（直接读 `CDCCIP2R.USBSEL` 与 `PLL3DIVR`）。修复后 CDC 自检 6/6 通过。

## 其它约定

- 烧录/擦写不需要二次确认，直接执行（用户明确要求）。
- 修改代码后必须用真实数据验证；烧录后用 `verify_image`（或回读比对）确认，再报告成功。
- 工程目录 `firmware/` 下的 `build_*.log`、`make_*.log`、`*.bak` 是临时产物，不要提交。
