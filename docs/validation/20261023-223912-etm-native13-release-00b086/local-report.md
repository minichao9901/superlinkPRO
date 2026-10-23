# native.13 本地发布验证（2026-10-23）

用户明确决定开放1s并更新远程/打包。本报告记录推送前实际执行的验证；上线结果另存同目录online-verification.json，不将未发生的部署写成成功。

- Run-ID: `20261023-223912-etm-native13-release-00b086`
- Tested-Revision: pre-commit-worktree
- Tested-Base: b86b860a44ad164b41db078277514106bf9a9f93
- Tested-Dirty: true（运行时provenance、EXE重建、ZIP、补充边界测试；CLI未暂存）
- Validation-Result: passed（以下本地发布项）；额外旧fixture测试限制单列
- 版本：1.0.14-native.13
- ZIP：78,774,775字节；SHA256 `c148b6b5f4c5ea1f633b90679261ef0c2a70fddaf368b812e6cefedda00108c7`
- 包来源提交：`b86b860a44ad164b41db078277514106bf9a9f93`
- 验证脚本/日志：`E:/superlinkPRO/docs/validation/20261023-223912-etm-native13-release-00b086`
- 独立运行包：`C:\Users\Administrator\.codex\validation\20261023-223912-etm-native13-release-00b086\portable-验收\etm-bridge-kit`；端口63764，默认engine=native

## 实际执行

1. 源页同步、web bundle current检查、Pages副本31文件一致；运行时93文件来自本轮源码，原生解码器重新编译，源码/provenance/包内文件指纹一致。
2. `python web/tools/dev/test-etm.py --output <evidence>/regressions`：10组通过，其中Node67/67、生命周期24项，以及原生导出/筛选/准备缓存等回归。新增采集实际深度4项、实际时长DOM3项通过。
3. 补充真实页面处理器测试14/14：新能力允许1000ms、拒绝1001ms且不操作硬件；旧服务声明200ms时仍限制200ms。新页面缺能力声明时仍用200ms提交校验。
4. 源端336数值边界/并发/元数据等矩阵6项通过。额外完整 `test_server_store_backend.py` 的能力检查通过，但旧10ms fixture仅有decoded/etm.bin/ELF、缺session/raw，导致3失败/1错误/2跳过；原日志 `source-store-backend.log` 保留，不作为发布成功项。真实1s记录验证见下一项。
5. 实际解压完整ZIP并核对1929个包内文件SHA256。EXE在仅System32的PATH、中文带空格目录运行；便携回归8/8（启动、默认native、1s/5亿capabilities、资源、端口复用、示例重解码、重新下载不夹用户数据、源码导出/缺源码错误、已建索引源码刷新）。日志 `portable-regressions.log`。
6. 原始真机1s记录包133051224字节实际导入；确认physical sample_depth=500000000、sample_rate=500000000、输入指纹verified=true；首/中/末150行可查询，101条CSV区间导出成功。完整重解码后summary/quality/analysis与导入前全部一致。此次墙钟23.96s，验证与便携回归并行，不能当作独占性能基准。
7. 完整包不含未完成CLI、用户新采集记录或SDK。所有原生优化默认保持开启；没有重写OpenCSD。本轮发布不重新做硬件采集，采用当日已保留的原始真机1s包验收新EXE。

## 产品限制

默认可选上限从200ms变为1000ms，最多5亿样本；默认输入仍10ms，不自动分窗。请求1s不保证LA录满：深度不足时明显警告并保留/解码实际窗口。报告显示实际采集时长，不以请求值或周期数替代。

1s先前真机处理约14～16s，首次部分导航约0.48s；20MHz下录满1s，30MHz本轮约927ms。用户决定先发布，**并非所有性能/硬件门禁已通过**。旧LA接线的信号质量、容量与采样率限制仍成立。

源码工作目录：`C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO`。关键证据见 `local-evidence-index.json`；待Pages完成后另外下载线上完整ZIP核对字节/SHA256。
