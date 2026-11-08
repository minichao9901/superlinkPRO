# Windows 工作台 web.8 与探针固件收尾

2026-11-08（北京时间）。探针修复提交 `6962e413935464725ac5e8cfda96bb547ca594a1`。Windows 包以源码提交 `6292a59b2fd7a779aea777c8f0811f2a6d9ea762` 构建，ETM 运行时从 `6962e41` 重新同步并编译五个原生程序，保留既有示例记录。发布提交为 `2eeabd40638edf4f78e9be1825defa3bd8610be7`。

| 交付 | 校验 |
| --- | --- |
| EVKLite DFU 应用包 | `2026/11/08 16:19:57`，194,312 B，应用 CRC `62aadd00`，SHA-256 `e57cc5a86e09faa153bb83d5ad2c76aaac90dd16ccee3b992c320e80c6667901`，与 RV32 RTT 修复验收时从探针读回的字节完全相同。 |
| `superlinkPRO-win64.zip` | `1.1.0-web.8`，103,183,629 B，SHA-256 `868d3cfb65f9e36e0c1e06116f240d64b312b412906d4240bcc10fbb5bf88a51`。 |
| 包内 `superlinkPRO.exe` | SHA-256 `f60871f30470c328125de40da36c67e928f3964afe58447bb625a1ae5dd40961`。逐项核对 3,410 个包内文件的大小和指纹。 |

固件 19 个主机回归脚本、10 项 CLI 测试、Web bundle 与 ETM 页面副本检查通过。本机 GNU Make 通过 `cmd.exe` 调度首个脚本时解析失败，按相同清单用 Windows Python 逐项执行全部通过。冻结 EXE 默认队列通过令牌认证、请求提交、结果回传和回收，HTTP 资源、包内下载、原生示例重解、Trace 查询与火焰图导出通过。硬件验收沿用上述同字节探针固件的 RV32 记录，本次重打包验证范围为冻结软件。正在运行的本机旧工作台及其 `data` 保留，`dist` 中的正式 ZIP 和下载清单已更新。[RTT 真机](rv32im-rtt-cdc-throughput-20261108.md) · [J-Scope 真机](rv32im-jscope-throughput-20261108.md)

Pages 部署成功。从线上取回清单及完整 ZIP，大小和 SHA-256 与本地一致。线上 Web bundle 的指纹也与源码清单一致。[Pages 工作流](https://github.com/minichao9901/superlinkPRO/actions/runs/42468611078) · [Windows 下载](https://minichao9901.github.io/superlinkPRO/downloads/superlinkPRO-win64.zip)

移除临时构建工作树，按逐文件清单清理 8,781 项临时产物，逻辑字节数 1,232,008,821。删除日志逐项核对通过，涵盖前轮 `release-web7` 剩余副本、本轮 RV32 验收原始日志和冻结候选包。保留工作台用户数据、现有会话和历史基准。探针回退包和原应用区备份保存在本机独立的 `superlinkPRO-backups/20261108-rv32-cdc-probe-rollback/`。

ISSUE-107 随探针固件交付。ISSUE-103 的偶发只读传输错误、ISSUE-108 的原生 OpenOCD SBA 合批和 ISSUE-109 的偶发 J-Scope USB 缓冲丢样继续跟踪。[项目问题跟踪列表](../项目问题跟踪列表.md)
