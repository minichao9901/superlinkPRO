# 对账基线（tools/fixtures/gen/）

这里放的是**用户自己的 Python 工具** `uvprojx2cmake.py`（仓库：`E:\VibeCoding\my_trace_tools`）
对一个真实 Keil 工程的**原始产物**，用来给网页版「工程生成」页做逐字节对账
（`node tools/selftest/gen-parity.mjs`）。

**这些文件不是手写的**，改动它们等于改验收标准 —— 除非你确认 Python 工具那边换了模板。

## mdk-arm/ —— 6 个产物

工程：`CubeMX_Config.uvprojx`（STM32F103C8，本目录 `uvprojx-sample/` 里的那份）
生成命令（在 Python 工具仓库里跑）：

```
python uvprojx2cmake.py reference/RTT_TRACE_LOG/App_helloworld/MDK-ARM/CubeMX_Config.uvprojx ^
    --gen-jlink --gen-gdb --gen-pyocd --gen-openocd --gen-test-bin --force
```

| 文件 | 字节 | 说明 |
|---|---|---|
| Makefile.jlink | 6043 | J-Link 烧录/调试 Makefile |
| jlink_gdb.script | 1633 | GDB 脚本 |
| Makefile.pyocd | 4497 | PyOCD Makefile |
| Makefile.openocd | 7493 | OpenOCD Makefile |
| rtt_logger.py | 2363 | OpenOCD 项附带的 socket 日志脚本 |
| test_sram.bin | 20480 | SRAM 读写测试图案（`i % 256`） |

对应参数（写死在 `tools/selftest/gen-parity.mjs` 的 `FIXTURE_PARAMS` 里）：

| 参数 | 值 |
|---|---|
| project_name | `MDK-ARM`（Python 工具取的是 .uvprojx 所在**目录名**） |
| jlink_device | `STM32F103RB` |
| flash_start | `0x08000000` |
| rtt_address | `0x20002000` |
| pyocd_target | `stm32f103rb` |
| openocd_root | `E:/MounRiver/MounRiver_Studio2/resources/app/resources/win32/components/WCH/OpenOCD/OpenOCD` |
| openocd_interface | `interface/cmsis-dap.cfg` |
| openocd_target | `target/stm32f1x.cfg` |

注意：所有文本产物都是 **CRLF** 换行（Python 在 Windows 上 `open(path,'w')` 的默认行为），
所以网页版默认也用 CRLF；要 LF 在页面上切一下即可（内容除换行外完全一致）。

## uvprojx-sample/

`CubeMX_Config.uvprojx` 原件，给解析器自测用（`<Device>` = STM32F103C8、
`<Cpu>` 用的是 `IRAM(0x20000000-0x20004FFF)` 这种 dash 写法）。
