# Zynq Cortex-M0+ RAM 下载验证

2026-11-17，目标为 Zynq 上的 `cm0p.cpu`。探针为 akaLinkPro CMSIS-DAP 2.1.2，SWD 1 MHz，DPIDR `0x0bc11477`，CPUID `0x410cc601`。输入使用 Cortex-M0+ 工程的 `debug/cortex_m0p.cfg` 和 `build/rtthread_nano.elf`。ELF SHA-256 为 `4df569047b958ebdb8f1c45d95b9eff72d5577eaeeff5cbd50eeac1d61721d66`。

## web.25 复现

8878 为本机 `1.1.0-web.25`，包来源 `c5663df9a2f5c755d1d5922117fd843216bdd2bb`。原版 CFG 导入、SWD 连接、CPUID 与 SRAM 读取均成功，ELF 符号可解析。连上后「下载到 RAM 并调试」仍禁用。调用 `launchRamElf` 时在操作硬件之前返回「请先通过 WebUSB 连接支持 RAM 调试的目标」。

`f2fa8ca` 的 ARM RAM 下载入口对 CFG 仅启用 STM32F1、nRF52。通用 RV32 的 RAM 声明和启动在另一个会话实现中。web.25 调试器的近期提交没有移除通用 Cortex-M RAM 支持的证据。

## 修正版实板

在 `d00d2e5` 基础上使用本轮工作区源码。实板运行生产调试会话、页面 `configureRamWindow` 和 `launchRamElf`、相同的 OpenOCD Wasm，USB 由仓库的 Node 原生适配器提供。Wasm SHA-256 为 `1977164ce62a8595753be9dfa741751c0b3f2459a95aa6bd03f25afe72d554fe`。

| 步骤 | 结果 |
| --- | --- |
| 目标和 RAM 声明 | 原版 CFG 核识别通过。未声明 RAM 时按钮禁用。拒绝 RV32 地址 `0x80000000`。声明 `0x20000000`、65,536 字节后可下载。 |
| ELF 写入和回读 | 初始化 VTOR 为 `0x20000000`，镜像 48,784 字节逐字节一致，含 16,976 字节清零 BSS。停在 `main = 0x20000250` 后再次比较整个加载段，读回 SHA-256 为 `821cf16a025687af18038883c940b220136ca860811b211222a75ade9d5e24a2`。 |
| 单步和执行 | 单步到 `0x20000252`。继续后目标处于运行态，RTT 读到 `M0+ RT-Thread Nano` 和 `msh >`，GPIO0 读到 `1/0/0/1`。 |

测试前保存了整片 64 KiB SRAM，备份 SHA-256 为 `62699a5a2dae2c3f7feb8d0a337913ab088af4ac7985c2fa402e0be46522bc7d`。板上保留已启动的 Nano。原始输出和备份位于本机 `tmp/cm0p-web-ram-20261117/`。实板验证覆盖 SRAM 和调试寄存器操作。

## 浏览器和回归

Edge 分别测试源码模块和合并包。模拟目标中，侧栏 ELF 和 Intel HEX 在文件内地址回读一致。BIN 显示地址字段，地址留空时拒绝加载，填写后回读一致。镜像目录命令、键盘加载、12 KiB 回读、导出中断和页面布局也通过。浏览器的硬件连接验证使用 8878 原包，修正版的一键启动验收使用上述原生 USB 适配器。

33 项调试器与 Wasm 测试、13 项工作台 CLI 测试通过，含 Cortex-M0+ 未声明、镜像越界、不可重定位 VTOR，以及 RV32 RAM 下载和软件断点回归。

## Chrome WebUSB 实板

8880 修正版通过 Chrome 的 WebUSB 访问同一探针和板卡。工作台 CLI 通过网页控件选择和加载本地 ELF，在相同页面独立回读目标。开测保存整片 64 KiB SRAM，结果与原始数据保存在本机 `tmp/cm0p-web-ram-20261117/chrome-webusb/`。

| 验证 | 结果 |
| --- | --- |
| 侧栏加载 | 在停机态向 `0x200000e0` 写入并读回 4 字节哨兵，再点「加载镜像」。未填写地址，按 ELF 的 `PT_LOAD` 写入 `0x20000000` 起 31,808 字节，整个文件段回读一致并覆盖哨兵。BSS 16,976 字节保持原值，PC、MSP、xPSR 和停机状态一致。 |
| RAM 下载并调试 | 声明 64 KiB 后使用下载按钮。镜像及 BSS 共 48,784 字节回读一致，VTOR 为 `0x20000000`。停于 `main = 0x20000250`，指令单步到 `0x20000252`。继续后调试页 RTT 显示 `M0+ RT-Thread Nano` 和 `msh >`。 |

本轮板卡为 Zynq Cortex-M0+ / SWD 1 MHz。S 组确认镜像范围和回读，D 组确认建链、暂停、单步和继续，V 组在调试页由 ELF 定位并实收 RTT。F 组 Flash、E 组 ETM 不适用于本轮目标和镜像。R 组转发、J 组 J-Scope 及各组性能用例本轮未测。

### 合并文件选择器复验

源码提交 `94d3ec4`。在 8880 的 Chrome WebUSB 会话中将 `rtthread_nano.elf` 选入一次，先点击「仅写入内存」，再沿用该文件点击「下载并停在 main」。

- 在 `0x20000000` 回读 ELF 的 31,808 字节文件段，逐字节匹配且覆盖 `0x200000e0` 的哨兵。文件段 SHA-256 为 `c3d6dbdde06020b2233d3eb8d78fe69ac55481a15beb28461c2b6969a0a2b4c6`。BSS、PC、MSP、xPSR 和停机状态与写前一致。
- 声明 64 KiB RAM 后使用同一文件运行到 `main = 0x20000250`，含清零 BSS 的 48,784 字节再次回读一致，VTOR 为 `0x20000000`。单步到 `0x20000252`，继续后在调试页 RTT 实收 `M0+ RT-Thread Nano` 和 `msh >`。
- Edge 的源码和合并包回归确认 ELF 两项动作接收同一 File，BIN 选中后出现地址框，HEX 和 BIN 只显示内存写入口。冻结 EXE 的浏览器回归重复通过。

实板回读和日志位于本机 `tmp/cm0p-web-ram-20261117/unified-chrome-webusb/`。

## web.26 分发

Windows 打包源码 `a931e7265a3b4b929a425884e415f32efe13e889`，运行时来源 `94d3ec4f9b3e1e130005f571b2354bbc370981db`，分发提交 `d78690e`。Web 构建为 `2026-11-17T01:58:44.530Z`，Windows 构建为 `2026-11-17T02:02:09Z`。

| 对象 | 结果 |
| --- | --- |
| 冻结候选 | 3,570 个条目逐一校验，CLI 队列、HTTP 下载、原生 Trace 解码与查询导出通过。 |
| 本机 EXE | `dist/superlinkPRO/superlinkPRO.exe`，SHA-256 `a9bdbd04e6ee459a7b49054dd7ccd57de0b3f7de8ecffb39175f38221d4bd4ae`。8878 在 Edge 通过合并选择器、三种镜像格式和内存回读回归。 |
| 本机数据 | `data/` 原位保留 107 个文件，104 项逐文件 SHA-256 一致。3 个正在占用的临时文件核对为原路径、大小、修改时间和文件标识。 |
| Windows ZIP | 104,123,583 字节，SHA-256 `cc01bf808623f26254d19b665d23f98cc4be9f73b45dae43e6f08e69bc95e94b`。 |
| Pages | [工作流 44000882368](https://github.com/minichao9901/superlinkPRO/actions/runs/44000882368) 部署成功。公开页面在 Edge 载入 web.26 合并包，共用选择器及 ELF、HEX、BIN 回读通过浏览器模拟目标验证。 |
| Release | [SuperLinkPRO 1.1.0-web.26](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.26) 已公开，ZIP 资产大小及 GitHub SHA-256 与本机清单一致。 |

从 GitHub Pages 和 Release 分别匿名下载完整 ZIP，各 104,123,583 字节。两份文件重新计算的 SHA-256 均与本机清单一致。
