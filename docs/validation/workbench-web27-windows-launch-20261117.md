# SuperLinkPRO web.27 Windows 启动验证

2026-11-17。启动修复提交 `0fede48`，冻结包源码 `f4cfe3608718184eb692af1a0ff31ed2f6b0263d`。默认浏览器为 Chrome 156.0.8078.48。

web.26 的工作台服务已经健康运行时，普通权限 EXE 交给高权限 Chrome 的启动请求没有完成。新启动器核对 Windows 打开的进程结果。Chromium 报告配置目录占用时，使用当前工作台数据目录下的独立浏览器配置。系统无法打开浏览器时，启动器给出可在浏览器直接打开的本机地址。正常的已运行工作台继续由原进程提供服务。 [原问题](zynq-cm0p-ram-debug-20261117.md#本机双击启动诊断)

| 验证项 | 结果 |
| --- | --- |
| 冻结候选首次启动 | 8895 的实际 EXE 拉起 Chrome，服务和浏览器令牌完整性均为 Medium（RID `0x2000`）。Chrome 请求首页并建立工作台 CLI 会话。 |
| 冻结候选再次启动 | 第二次启动后增加一次首页请求。浏览器 PID 和 8895 工作台 PID 均与开测一致。默认配置可用，未创建备用浏览器配置。 |
| 配置目录占用处理 | 模拟 Chromium 退出码 21，确认第二次打开命令使用独立配置，保留同一个工作台地址及 CLI 片段。模拟正常交接及其它错误也通过。高权限 Chrome 冲突未在本轮候选重建。 |
| 旧服务处理 | 核对 8878 原监听者为本机 web.26 验收启动的 EXE，其 CLI 无排队任务、没有执行页在线，子进程只有系统控制台宿主。退出这一进程后换装。8880 服务未参与更换。 |
| 资源管理器双击固定 EXE | 8878 空闲时从资源管理器双击 `dist/superlinkPRO/superlinkPRO.exe`。Explorer 启动 PID 30588，首页提供 web.27，普通权限 Chrome 自动打开并完成 CLI 握手。工作台和 Chrome 的完整性级别均为 Medium。 |
| 用户数据 | 换装前后 `data/` 原位保留，104 个文件全部 SHA-256 一致。 |
| 冻结验证 | 3,570 项文件清单、CLI、HTTP 与离线 Trace 验证通过。此轮未进行板卡硬件操作。 |

固定 EXE SHA-256 为 `c7383f6ded83ea816168b736d3ab120cda93d396dd7b36f2ea489a767f0bbc6b`。Windows ZIP 大小为 104,127,846 字节，SHA-256 为 `d7f4bc45a833dbbfb5d9b16c6438cd80ccb314b06b2b8c2844dc40f16e2a7218`。本机验证保存在 `tmp/windows-launcher-20261117/`。

分发提交 `1291cdedbf398de9f28dbc2ec0f83c94c3b387aa`，标签 `workbench-v1.1.0-web.27`。Pages 工作流部署成功，公开工作台在浏览器加载 web.27。Release 已公开，服务端 ZIP 大小和 SHA-256 与本机清单一致。 [Pages 工作流](https://github.com/minichao9901/superlinkPRO/actions/runs/44014666357) · [公开工作台](https://minichao9901.github.io/superlinkPRO/) · [GitHub Release](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.27)

分别从 Pages 和 Release 匿名下载完整 Windows ZIP，重新计算的大小和 SHA-256 均与本机清单一致。
