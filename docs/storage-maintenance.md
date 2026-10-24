# 工程存储与验收资产维护

## 2026-10-24 盘点与执行状态

本轮只读盘点发现：

| 范围 | 逻辑体积 | 主要内容 |
|---|---:|---|
| 根 docs | 172.15 GiB | 历史验收运行、解码JSONL、运行包和源码快照副本 |
| 根 tmp | 38.71 GiB | 性能实验、参考输出、巨型合成JSONL |
| web/tmp | 1.29 GiB | 历次PyInstaller工作目录和候选包 |
| web/docs | 约20.6 MiB | 正文和历史验证摘要，以保留为主 |
| etm/docs | 约2.3 MiB | 协议/适配/交接文档，以保留为主 |

清理计划还包括 `etm/records` 中自动生成的 `_p4_*`、`_r0_*`、`_r1_*` 和
`.p4_test_cache`，不包括 `capture-*` 原始采集。候选共114,302个文件、253.30 GiB逻辑体积；
其中176.94 GiB对应的文件数据，其全部硬链接均在候选清单内。
硬链接、文件压缩及清单生成后的变化都会影响最终磁盘差值，不能把逻辑大小当成已释放空间。

**已由用户在本机完成删除，并通过清理后核对。** 代理最初的删除调用被工具以
`blocked by policy` 拦截；用户随后执行已预检脚本，15:25完成。
删除日志与原清单逐项一致：114,302项全部记录，无重复、无遗漏、无候选残留，Git已跟踪文件无缺失。
正式native.14包大小与SHA256未变；两份旧参考记录的ELF绑定/解码输入长度及独立参考Store的文件SHA256核验通过。
清理后正文/临时目录逻辑体积：根docs约9.21 GiB、根tmp约0.55 GiB、web/tmp接近空；
其余保留内容主要是必要回归基准。E盘当前可用约180 GiB；没有以逻辑删除总量冒充精确磁盘释放量。
证据位于本地 `docs/maintenance/cleanup-check.json`、`protected-baseline-check.json`、
`storage-after-20261024.json` 及 `cleanup-applied-20261024-152149.jsonl`。
保留策略5项测试通过；PowerShell只读预检114,302项全部通过，缺失项0，退出码0。
计划SHA256：`be551d684eb6c31df3f5fa03b3d44e82141ce5bf19942a56bc1c194dc35f0eb8`。

## 保留规则

- 保留全部Git已跟踪文件，包括核心文档、发布固件、正式桥包和已有的关键验证摘要。
  仅约几十MiB的正文不是此次占用的主因，不批量改动文档链接。
- 保留运行目录中的原始报告、复现实验脚本、小型JSON/日志/CSV和关键截图。
  复制的完整产品树、第三方运行时及它们附带的文档/脚本按副本处理。
- 保留现有测试依赖的steady/boundary两份旧参考记录、绑定核验探针、合成前端的首/中/末三段，
  以及独立Python长记录参考Store。这部分约9.2 GiB，是后续差分回归资产。
- 保护所有Git checkout/worktree；`tmp/perf-baseline`也在保留清单内。
- 保护 `etm/records/capture-*` 和正式 `web/bridge/etm/downloads`，不自动结束进程、操作硬件、
  删除Git历史或修改外部 `E:/h743_trace_proj` 等工程。

旧参考资产待CDC四档基准建档后可单独迁移/替换；本次计划保留其原路径，现有测试不必因清理而跳过。

## 本地操作

从工程根目录运行。第一、二步均只读；第三步才真正删除，需要操作者在本机执行。
计划记录每个候选的确切相对路径、大小、时间戳，并配套SHA256。

```powershell
# 1. 重新盘点并生成清理计划
python -X utf8 tools/repo_storage_inventory.py --out docs/maintenance/storage-before.json
python -X utf8 tools/maintenance/artifact_retention.py

# 2. 查看汇总、完整候选清单并预检；默认绝不删除
Get-Content docs/maintenance/storage-cleanup-summary.json
pwsh -NoProfile -File tools/maintenance/clean-generated-artifacts.ps1

# 3. 本机按已审阅清单执行删除
pwsh -NoProfile -File tools/maintenance/clean-generated-artifacts.ps1 -Apply

# 4. 核对删除日志/候选残留/受保护资产，再盘点空间
python -X utf8 tools/maintenance/check_cleanup.py
python -X utf8 tools/repo_storage_inventory.py --out docs/maintenance/storage-after.json
Get-PSDrive E
```

执行前停止自己启动的临时验收任务，保留用户正在使用的服务。脚本先逐项预检，拒绝路径越界、
目录链接、嵌套Git仓、已跟踪文件、保护资产、Git提交变化及计划后改动的文件。
执行时只删除清单中的普通文件，不递归删除目录、不使用通配符或 `git clean/reset`；
每项成功删除都有JSONL日志。空目录可保留，不影响释放大文件空间。
预检中发现文件变化时重新生成计划，不使用Force绕过保护检查。

本地清单与执行日志放 `docs/maintenance/` 并由Git忽略；策略、工具、测试和本说明入源码管理。
纯策略测试：

```powershell
python -X utf8 -m unittest discover -s tools/maintenance -p test_artifact_retention.py -v
```

## 后续目录约定

- `docs/`：当前用户手册、协议/架构、发布结论、精简证据索引；不再嵌入运行包或源码全树。
- `tmp/`：可再生工作目录。性能测试、导出、临时编译/解压均在独立run目录，验收结束后按计划清理。
- `docs/validation/<run>/`：保留小型报告、命令、源码/工具/输入指纹、统计和必要失败摘要。
  原始大输入用稳定基准ID引用，报告明确哪些派生产物已清理；Git提交中记录的旧证据路径不代表永久保留所有输出。
- `etm/records/baselines/`：独立基准输入及compact Store，避免为每次优化重复存一套波形、JSONL和完整包。
- 正式完整包保留当前发布的一份；源码历史由Git管理，旧包需要时从明确的历史发布恢复。

ETM主线验收改用 [CDC四档规范](etm-cdc-acceptance.md)，10/200/500/1000ms各有一组可溯源基线。
异常、IT未知、ETMv3、损坏拒绝等小型兼容/负例单独保留。

## 关键文档入口

- [Trace CLI手册](trace-cli-ai.md)
- [阅读导出规则](trace-reading.md)
- [native.14交付与验收](etm-reading-release-2026-10-24.md)
- [原生引擎架构](etm-native-engine.md)
- [TraceStore格式](../etm/docs/trace-store-v1.md)
- [CDC固件与接线](../etm/firmware/stm32h743_cdc_trace/README.md)
