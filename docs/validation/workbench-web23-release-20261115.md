# SuperLinkPRO 1.1.0-web.23 正式发布验收

2026-11-15。发布包括三板验收工具、ARM/JTAG 原生 CLI、Web 工作台和 Windows x64 便携包。Web 的目标入口为 ARM/SWD 与 RISC-V/JTAG。实机证据沿用 [三板交接](board-acceptance-offline-20261115.md#2026-11-15-收尾交接)；本轮完成的是正式发布、安装和分发验证，新增三板整板与 ETM 实机仍待验。

| 项目 | 已核对的值 |
| --- | --- |
| 构建源码 | `aebe813073c82ee30382ee7c1d49117cb479acfb`，构建时源码无未提交变更 |
| ETM 运行时来源 | `b474b93f7daf0ff6b1d53137712c8f7531d3dbde` |
| 发布提交 | `0314cea326109df2c2f1e1bdcf6dfcc670577ad4` |
| Web 构建 UTC | `2026-11-15T05:44:44.648Z` |
| Windows 构建 UTC | `2026-11-15T05:47:53Z` |
| Windows ZIP | 103,899,767 B |
| ZIP SHA-256 | `e7cd2da36010e6fb0e1f9893513ab5abad2b65d661df80cedd200b08f3d06d64` |

- 固定工作台 `dist/superlinkPRO/superlinkPRO.exe` 已更新。从该路径实际启动，在隔离数据目录打开页面，确认「帮助」显示新版本和两项构建时间。固定安装的 71 个用户数据文件在更新前后逐一路径、大小、SHA-256 对比一致。固定 EXE SHA-256 为 `8c3304be6d6b03499f55c2a811d08749382e2e6f08d2728a42fc1b6f4e083be5`。
- Windows 冻结候选核对了 3,562 个包文件，冻结 CLI、HTTP 和 Trace 回归通过。ZIP 内部包标识与固定安装一致。源码侧 142 项 Python 回归通过，Web bundle、ETM 线上页面副本与 CLI 清单检查通过。
- Pages 已部署发布提交。匿名打开页面时确认版本和构建时间可见，并核对首页引用的 bundle 与线上 manifest 哈希一致。按照公开下载清单完整下载 ZIP，字节数和 SHA-256 与本地一致。 [Pages](https://minichao9901.github.io/superlinkPRO/#download) · [部署记录](https://github.com/minichao9901/superlinkPRO/actions/runs/43682821937)
- GitHub Release 正式公开。标签 `workbench-v1.1.0-web.23` 指向发布提交，清单与本地一致。ZIP 经匿名完整下载核对，字节数和 SHA-256 与 Pages、本地一致。TLS 中断后先读回资产状态，再发布已上传并校验的 ZIP。 [Release](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.23)

本机验收证据和匿名下载副本在 `tmp/workbench-web23-release-20261115/`。冻结候选验收及上一固定安装的程序回退副本保存在 `tmp/workbench-package-2594e862/`。历史问题状态沿用 [项目问题跟踪列表](../项目问题跟踪列表.md)。
