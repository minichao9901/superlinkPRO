# Web 与 Windows 工作台 web.11 发布记录

2026-11-09（北京时间）。修复提交 `f6c5dff`，Windows 打包源码 `8086f961317148089c81bd4ae26f1d5744698e76`，快照来源 `f6c5dff4c0c7caf628025c87118ee37378d62a0b`。构建时打包输入干净。

| 对象 | 实际验证 |
| --- | --- |
| Windows `1.1.0-web.11` | ZIP 103,184,830 B，SHA-256 `fd91891161d761f0b030d2553406fe7442404375441ffc349090d7c021ec6667`。ZIP 内 3,410 个资源逐个按清单核对，安装目录逐文件验证通过。 |
| 本机 EXE | SHA-256 `16ee5bd34d05466974050cf42d1e4b4d2f8e4f7ac7d12dad009e09cabaccaf52`。8878 HTTP 和 CLI 均识别为 `1.1.0-web.11`，packageId `f4959eff07c5d62eee1d48256aeed29207f37ac908085c00ec7ff215bdc87bb1`。 |
| 冻结服务与 CLI | 冻结预检、HTTP 下载、认证队列、原生示例解码、查询和导出通过。另起隔离冻结服务，Chrome 在旧租约过期后 17.219 秒领取原只读 job 并完成回收。试验队列终态 pending/retained 均为 0。 |
| 实板异常面板 | 源码与最终安装包分别在 Chrome 156 / WebUSB / HPM6800EVK 通过 ECALL、非法指令、JSON/Markdown 导出、历史标记。两次验收均回写读回临时 ILM 和原寄存器，恢复运行并释放探针。 |
| 软件回归 | 18 项 JS、8 项队列、10 项统一 CLI。Web 合并资源及 ETM 页面副本检查通过。 |

安装包的两种异常面板报告实测如下。测试在原异常向量入口暂停，`pc=0x000001e4`、`mepc=0x00018000`。固件变更范围为空，目标测试仅临时写入已备份的 32 B ILM。

| 类型 | `mcause` | `mtval` | JSON SHA-256 |
| --- | ---: | --- | --- |
| ecall | 11 | `0x00000000` | `9a32d95d11f2069f5cdd0c6fc3f1f64609d3fdbdf50d12a7244b784c638dac06` |
| illegal | 2 | `0xffffffff` | `008831b062db143167e6e47869ecc980da2c95272f5365de47dc6abe9a238869` |

本机安装曾因 Windows 目录移动中途失败而留下两段旧目录。完整核对原包 3,410 个文件与 64 个持久数据文件，逐文件归并数据和安装候选。重新启动后数据清单与安装前一致。ISSUE-135。

[实板验收和源代码面板补测](hpm6800evk-web10-acceptance-20261109.md) · [问题跟踪](../项目问题跟踪列表.md)

## Pages 与清理

发布提交 `97779546dee0af6c2fea1afe833ba85891af9ccd` 推送至 `main`。[Pages 工作流 42556888276](https://github.com/minichao9901/superlinkPRO/actions/runs/42556888276) 的 build 和 deploy 均成功。线上下载清单与提交一致。首页、合并脚本及清单、OpenOCD Wasm、ETM 入口共五项都按发布提交的 Git 字节核对一致。ETM 入口的 Windows 检出行尾是 CRLF，Pages 的 LF 与 Git 对象一致。

通过 25 个连续且无重叠的 HTTP 206 字节区间取回完整线上 ZIP。总计 103,184,830 B，SHA-256 `fd91891161d761f0b030d2553406fe7442404375441ffc349090d7c021ec6667`，与本机正式 ZIP 一致。分段下载规避了单条 TLS 连接间歇中断及慢速。[线上下载清单](https://minichao9901.github.io/superlinkPRO/downloads/manifest.json)

回收本轮三处仓库内生成目录，共 8,374 个文件、1,340,476,628 B。仓库外发布工作目录另有 433 个文件、362,490,053 B，统一移入回收站。检查构建、暂存、候选验证目录均已消失。8878 保持 `web.11` 运行，浏览器调试已断开，队列无未完成任务，硬件协调锁已释放。对比本轮起始提交，固件和历史 ETM 独立下载线均无变更。
