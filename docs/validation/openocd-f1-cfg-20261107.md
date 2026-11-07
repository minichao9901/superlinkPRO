# STM32F1 CFG 导入与 WebUSB RAM 调试验收

2026-11-07（北京时间）。基线源码 `ee00aa3cce6ba02cdba4e12a360eba2dc0d34b3c` 加 RAM 调试和 CFG 导入工作区变更。对象为 Chrome 156、akaLinkPro 和 STM32F103ZE（512 KiB Flash、64 KiB SRAM）。测试覆盖工作台源码模块与 Web 合并包。

| 上传的原版配置 | 页面入口 | 导入与连接 | RAM 调试 |
|---|---|---|---|
| OpenOCD v0.12.0 `target/stm32f1x.cfg` | 源码模块 | 工作区 4 KiB，实际 SWD 1 MHz，DPIDR `0x1ba01477` | 停在 `main`，两次 F10 通过 |
| OpenOCD v0.12.0 `board/stm3210e_eval.cfg` | 合并包 | 由内置 F1 配置解析引用，工作区 32 KiB，实际 SWD 1 MHz，DPIDR 一致 | 停在 `main`，两次 F10 通过 |

两轮开始时手动时钟为 10 MHz。导入时探针仍断开，点击「连接」后实际使用 CFG 的 1 MHz。预期 DPIDR 故意改为 `0x2ba01477` 的负例被拒绝，清除 CFG 后可重新导入并连接。board 文件的外部 FSMC 事件列为跳过项，验证范围为 F103ZE 片上 SRAM。 [上游 target](https://github.com/openocd-org/openocd/blob/v0.12.0/tcl/target/stm32f1x.cfg) · [上游 board](https://github.com/openocd-org/openocd/blob/v0.12.0/tcl/board/stm3210e_eval.cfg)

两轮均拒绝把 Flash ELF 写入 RAM。使用 RAM ELF 时，PC 从 `0x20000160` 依次停到 `0x20000176`、`0x2000017C`，正文高亮从第 20 行移到第 22 行，测试变量从 0 变到 41、42，原指令已恢复。硬件通信全程为浏览器 WebUSB/CMSIS-DAP。每轮前后 512 KiB Flash SHA-256 一致，回写并校验原 SRAM 备份后重启原固件。 [机器摘要](openocd-f1-cfg-20261107.json)

## 复现

按 [RAM ELF 验收](ram-debug-f103ze-20261107/README.md) 构建 ELF，确认 F103ZE 没有进行中的调试任务，启动 Chrome 工作台并完成探针授权。下载两份配置到本次独立的 `tmp` 目录，并为每轮测试新建输出目录。

```powershell
$fixture = 'docs/validation/ram-debug-f103ze-20261107'
$cfgRoot = 'tmp/openocd-f1-FRESH'
New-Item -ItemType Directory $cfgRoot
New-Item -ItemType Directory "$cfgRoot/target-run", "$cfgRoot/board-run"
$upstream = 'https://raw.githubusercontent.com/openocd-org/openocd/v0.12.0/tcl'
Invoke-WebRequest "$upstream/target/stm32f1x.cfg" -OutFile "$cfgRoot/stm32f1x.cfg"
Invoke-WebRequest "$upstream/board/stm3210e_eval.cfg" -OutFile "$cfgRoot/stm3210e_eval.cfg"
@('set CPUTAPID 0x2ba01477', 'source [find target/stm32f1x.cfg]') |
  Set-Content -Encoding UTF8 "$cfgRoot/wrong_dpidr.cfg"
```

将 `$ramElf` 设为已构建的 RAM ELF。依次在刚加载的 `modules=source` 和 `modules=bundle` 调试页运行对应命令。

```powershell
node "$fixture/hardware.mjs" $ramElf "$cfgRoot/target-run/summary.json" 1000 "$cfgRoot/stm32f1x.cfg" "$cfgRoot/wrong_dpidr.cfg"
node "$fixture/hardware.mjs" $ramElf "$cfgRoot/board-run/summary.json" 1000 "$cfgRoot/stm3210e_eval.cfg"
```

本轮离线覆盖条件分支、变量和板卡覆盖顺序，并拒绝脚本执行、外部引用、Flash 写命令和未知初始化。后续扩展后的回归入口为 `node --test web/tools/selftest/dbg-openocd-cfg.test.mjs`。

Web 离线测试 264 项、CLI 单元测试 9 项通过。切换浏览器验收入口后，复用旧 CLI 运行时首次收到 HTTP 409。按单执行页规则重开独立运行时，`describe`、CFG 上传、回读和清除均通过。
