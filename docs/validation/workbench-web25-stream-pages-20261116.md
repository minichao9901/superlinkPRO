# SuperLinkPRO web.25 三页优化与发布验收

2026-11-16（北京时间）。参考 ZynqLinkPRO 的 `5c4812fd`、`58037261`、`56c6a017` 和 `0c05d097`。工作台页面提交为 `de1a689`，Windows 源码提交为 `c5663df9a2f5c755d1d5922117fd843216bdd2bb`，发布提交为 `0b265f1b829859423bc14c97e9fa633e2a1497c0`。

## 页面与回归

| 模块 | 改动和结果 |
| --- | --- |
| V · RTT Viewer | 支持沿用调试页的 OpenOCD 目标。用 F103ZE 配套 ELF 解析出 `0x2000000c`。模拟探针实收上行，并核对界面显示的控制块。指向无标识的地址时报告失败地址，探针会话仍在线。手填确切地址和会话操作位于折叠区。 |
| R · RTT 转发 | 探针 HID START 仅匹配指定地址的 10 B 签名。浏览器测试覆盖唯一探针 CDC 自动选中、多个候选需手选、刷新保留已选端口及错口拒绝。模拟探针连续三轮转发、停止并恢复启动按钮，沿用 HPM5301 的 STOP 状态轮询。 |
| J · J-Scope | 连接详情、模拟和高级设置归入折叠区。侧栏显示建议起始周期和标定状态，完整依据可在读计划查看。模拟采样和停止通过。EP83、ZLP 与尾包处理的相关回归通过。 |

源码、Windows 冻结候选、固定 EXE 和 Pages 均在独立 Chromium 会话走上述三页。浏览器设备授权弹窗为 0。Node 定向回归 27/27，CLI 单测 13/13。Web 合并资源、CLI 参考和 ETM 线上副本检查通过。冻结包验证覆盖 3,570 个清单文件，含无额外 PATH 的 dfu-util 预检、CLI/HTTP 和离线 Trace。

本轮覆盖 V/R/J 的页面及软件协议回归。F103ZE ELF 用于离线解析，H743、F103ZE 和 HPM6800EVK 均未重新上板测速。ISSUE-166 的 H743 双变量稳定采样率仍待专项实测。

## 实际分发

| 项目 | 核对值 |
| --- | --- |
| 工作台版本 | `1.1.0-web.25` |
| Web 构建 UTC | `2026-11-15T16:24:10.493Z` |
| Windows 构建 UTC | `2026-11-15T16:28:46Z` |
| Windows ZIP | 104,118,014 B |
| Windows ZIP SHA-256 | `e6accaa4568a25a20cfd8e7c2fe75e46d2f15409f2f6de56031bde250519b0b1` |
| 固定 EXE | `dist/superlinkPRO/superlinkPRO.exe`，SHA-256 `27cc6ee57640f42bef991530c67abeb6103420eef369fa515af34e95668a047f` |
| 用户数据 | 固定安装替换前后 104 个文件的路径与 SHA-256 完全一致 |
| 线上验证 | Pages 和 Release 各匿名完整下载一次，长度和 SHA-256 一致。线上帮助及下载页均显示正确的版本和构建时间。两处 dfu-util 对应源码包也通过清单校验。 |

[GitHub Pages](https://minichao9901.github.io/superlinkPRO/) · [Release](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.25) · [Pages 工作流](https://github.com/minichao9901/superlinkPRO/actions/runs/43735965286)
