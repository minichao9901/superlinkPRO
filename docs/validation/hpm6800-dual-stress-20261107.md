# HPM6800EVK 双后端调试压力测试

2026-11-07（北京时间）。验证对象为 `ee00aa3` 加工作区改动的本地 Web 工作台、HPM6800EVK 和 akaLinkPro。两套后端在同一个已授权的浏览器标签页中串行连接，均为 8 MHz JTAG。内置后端导入原版 HPM SDK 1.11.0 板级 CFG，Wasm 后端使用原版 `hpm6800_all_in_one.cfg`。Flash 上共用仓库的 `hpm6800evk_dbgstress/fw.elf`，由 Wasm 一次烧录，13 个扇区共 53,248 字节回读一致。 [测试 ELF](../../web/tools/target-firmware/hpm6800evk_dbgstress/fw.elf)

测试通过工作台 CLI 运行。选用已有 HPM 压测的断点、停走、单步、变量及监视用例，并加入复位并运行和指令单步。13 组包含 19 步指令单步、19 步源码跳过、五次逐层跳出、四断点同时命中及 40 次连续命中。原 CDP 套件的 GDB oracle 和触发器 CSR 逐槽盘点未纳入本轮。页面断点表和实际停止地址均被检查。

| 项目 | 内置 JTAG | OpenOCD Wasm |
|---|---|---|
| 13 组结果 | 13 通过 | 8 通过，5 未通过 |
| 40 轮停走 | 40/40，均停在 `0x80005b1e` | 断开重连后 40/40，均停在 `0x80005b1e` |
| 19 步指令单步 | 通过 | 通过，两套后端的 PC 序列一致 |
| 源码跳过、逐层跳出 | 均通过 | 均触发 `this.probe.writeMem is not a function` |
| 常规 `reset halt`、`reset run` | 两项各两轮通过 | 当前调试会话明确拒绝两项命令 |
| XIP 常量结构体 | 通过 | 当前读取范围拒绝该 XIP 地址 |

Wasm 首轮在源码步进失败后仍有断点命中，页面清空后重新添加同一地址报重复断点。源码步进借用的临时摘断点流程走到了 ARM FPB 写入，异常发生时页面断点表已经变更。隔离复测在每次失败后断开重连，RAM 结构体、三类监视表达式及 40 轮停走均通过，剩下的是上述五项独立失败。常规调试复位与烧录后专用启动命令是两个入口，本轮固件安装后的专用启动通过。两套后端最终都已断开，靶子异常计数均为零，板上继续运行压力测试固件。 [机器结果](hpm6800-dual-stress-20261107.json) · [共用断点逻辑](../../web/app/dbg/session.js#L396)

复现前在板上烧入上述测试固件，打开已授权的工作台，再运行以下命令。输出文件路径需尚不存在。 [CLI 压测脚本](../../web/tools/selftest/dbg-hpm6800-dual-stress.py)

```powershell
python -X utf8 web/tools/selftest/dbg-hpm6800-dual-stress.py <运行时目录> <SDK的boards/openocd目录> <新结果JSON>
```
