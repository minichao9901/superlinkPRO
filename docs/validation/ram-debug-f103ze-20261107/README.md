# F103ZE 的 WebUSB RAM 调试验收

2026-11-07（北京时间）。基线源码为 `ee00aa3cce6ba02cdba4e12a360eba2dc0d34b3c`，在该基线加本次工作区变更后验证。对象是 akaLinkPro、STM32F103ZE（DEV_ID `0x414`、512 KiB Flash、64 KiB SRAM）及 Chrome 156 的工作台页面。没有构建或发布 Windows 分发包。

| 实测项目 | 源码模块（1 MHz SWD） | 合并包（10 MHz SWD） |
|---|---|---|
| 将 Flash ELF 交给 RAM 入口 | 预检拒绝，64 KiB SRAM 哈希一致 | 同左 |
| 用 RAM ELF 从复位入口启动 | 停在 `main`，PC `0x20000160`，源码高亮第 20 行 | 同左 |
| 两次点击「跳过」 | PC `0x20000176` → `0x2000017C`，源码高亮 21 → 22，`marker` 0 → 41 → 42 | 同左 |
| 临时断点 | 三个停点的半字均与 ELF 原指令一致 | 同左 |
| 恢复 | SRAM 备份回写并全量回读，目标重新启动原固件 | 同左 |
| Flash | 全量 512 KiB 的 SHA-256 前后一致 | 同左 |

两轮都通过实际 Web 调试页的文件输入和按钮，目标通信全程为 WebUSB/CMSIS-DAP。测试程序只访问 SRAM。首轮试跑已验证停在 `main`、逐行执行和 Flash 哈希，测试脚本把文件列表传给了目录输入，漏验源码正文。改为提交目录路径后，最终两轮都确认了正文和高亮。 [机器摘要](summary.json)

## 复现

在仓库根目录用 Clang 15 和 LLD 构建 [测试源文件](ram_debug.c) 与 [SRAM 链接脚本](ram_debug.ld)。输出目录需为本次验收独立的新目录。

```powershell
$fixture = 'docs/validation/ram-debug-f103ze-20261107'
$out = 'tmp/ram-debug-FRESH'
New-Item -ItemType Directory $out
clang --target=arm-none-eabi -mcpu=cortex-m3 -mthumb -gdwarf-4 -O0 -ffreestanding `
  -fno-unwind-tables -fno-asynchronous-unwind-tables -nostdlib -fuse-ld=lld `
  "-Wl,-T,$fixture/ram_debug.ld" '-Wl,--nmagic' "$fixture/ram_debug.c" -o "$out/ram_debug.elf"
```

确认 F103ZE 当前没有调试任务、Flash 固件处于运行状态。按 [CLI 指南](../../toolbox-cli-ai.md) 启动工作台，在 CDP 端口 9333 的 Chrome 打开该工作台调试页。浏览器需要已有的探针授权，首次授权从页面完成。源码模块用 URL 查询参数 `modules=source`，合并包用 `modules=bundle`。每轮使用刚加载的调试页及新的输出目录。

```powershell
node "$fixture/hardware.mjs" "$out/ram_debug.elf" "$out/hardware.json" 1000
```

使用 10000 作为最后一个参数可复现默认 10 MHz 档。完整 SRAM 备份、临时 ELF 和日志保留在本机 `tmp/`。
