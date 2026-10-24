# Trace 阅读导出 rc.2 交付与 H743 真机验收（2026-10-24）

> 历史交接说明：以下是 ds 留下的候选版工作记录，不代表 Codex 本轮独立复验结论。后续审查发现环境泄漏和元数据绑定问题并已修复；最终结果以 `docs/etm-reading-release-2026-10-24.md` 为准。

结论：交接文档里用户要的第 1、2 条（阅读折叠投影 + 源码表达/宏注释）与第 5 条中选做的只读 AI 接口已完成并合入 `main`；
第 3 条按用户指示忽略、第 4 条（预处理宏展开构建）暂不做。本轮补上 Codex 未完成的收尾：合并、全量回归、
H743 真机 20 ms 冒烟 + 200 ms 采集、`1.0.14-native.14-rc.2` 便携包构建与离线验收。**未推送、未发布、未替换正式下载。**

- 合并后提交：`2ca380a`（`4932941` 阅读投影 + `2ca380a` 宏注释/语法高亮）
- 运行时快照 provenance：`sourceRevision=2ca380ab6cf96e5742cae61a01e8871d2df6de28`、`sourceDirty=false`
- 证据目录：`docs/validation/20261024-rc2-reading-delivery/`（本地不入库）

## 1. 交付内容

| 项 | 实现 | 入口 |
| --- | --- | --- |
| 重复执行块折叠 | 严格 >10 次才折叠，保留前 2 后 1，按 PC/三态/调用上下文比较，有限缓冲 | `export --readability learning` |
| 无源码库调用折叠 | 已证实调用及内部子调用归组，标注入口/返回是否证实 | 同上（`--no-fold-libraries` 关闭） |
| 条件未知归组 | 同一 IT 块相邻未知合并，指令数/PC/序号保留 | 同上（`--no-group-unknown` 关闭） |
| 语句与宏 | C 词法语句边界、DWARF `.debug_macro`/枚举求值、函数宏有预算展开 | `--no-statements`、`--macro-view original\|annotate\|expanded` |
| 导出格式 | 阅读折叠 C / HTML + `*.projection.jsonl` 序号映射 + `*.manifest.json` | `export`；网页两个 ZIP 选项 |
| HTML 高亮 | 两栏词法高亮（关键字/类型/常量/字符串/注释/函数名），离线可用 | 导出即时生成 |
| 只读 AI 接口 | `capabilities`、`query --compact`、`query --fields` | CLI |

规则与边界写在 `docs/trace-reading.md`，AI 用法在 `docs/trace-cli-ai.md`。

## 2. 合并与快照同步

- `git merge --ff-only codex/trace-readable-export` → `main` 由 `65f6540` 前进到 `2ca380a`。
- `sync-etm-page.mjs`：线上副本 0 写 / 31 相同。
- `sync-etm-runtime.py --etm-root etm`：原生工具重建（`ninja: no work to do` 后重新链接），
  运行时快照 103 文件，provenance 记为 `2ca380a` + dirty=false；五个原生 EXE 内容随本轮重建更新。
- 同步后重跑网页自有回归 11/11 通过，确认新快照可用。

## 3. 回归

| 范围 | 结果 | 说明 |
| --- | --- | --- |
| 原生工具重建 `etm/build-decoder.ps1` | 通过 | `gcc 16.1.0`，`etm_export.exe` 含 `reading-v1` |
| 全量 `etm/tools/tests`（`test_*.py`） | 614 项，455 s，8 failures + 3 errors + 8 skipped | 见 §6 逐项归因 |
| 阅读投影专项 | 16/16 通过 | 修复前因本地 `etm_export.exe` 陈旧失败，重建后全绿 |
| 存储写出专项（单模块） | 25/25 通过 | 全量顺序下出现的 7 项为既有测试隔离问题 |
| 生命周期专项（单模块） | 22/23 | 唯一失败为既有测试夹具问题 |
| 网页自有回归 `test-etm.py` | 11/11 通过（同步前后各一次） | 覆盖 69+8+24+… 共 166 项 |
| HTML 高亮 selftest | 2/2 通过 | `etm-export-highlight.test.mjs` |

## 4. H743 真机验收

现场：akaLinkPro 探针 `8F538633E6AEC4CA8ABB6A36FEBB1AC2`、Kingst LA（500 MSa/s，实际深度 100,000,000 点）、
目标 STM32H743（CPU 60 MHz / HCLK 30 MHz / 物理 TRACECLK 30 MHz），板上固件 Flash 与 ELF 校验一致
（`de9edd01…`，UDP Echo Server）。采集配置沿用既有 200 ms 记录口径：CH0 时钟 + CH1～4 数据、1.65 V、
取样位置 0.25、解码前 4 ns 滤波、Trace ID 2、返回栈与周期计数关闭；起点为 `trace_hook_entry` 硬件断点
（`--reset-to-entry`，临时断点用后释放）。

| 窗口 | 实际深度 | 指令 | 质量 | 包问题 | 未知 | cleanup |
| --- | --- | --- | --- | --- | ---: | --- |
| 20 ms 冒烟 | 10,000,000 点（录满） | 1,274,562 | passed | 0 | 0 | targetRestored=true, laIdle=true |
| 200 ms 采集 | 100,000,000 点（录满） | 13,042,967 | passed | 1（末端 I_INCOMPLETE_EOT，单列保留） | 188 | targetRestored=true, laIdle=true |

- 200 ms 记录含 191 个异常事件、1 处 gap，均在投影中原样保留；188 条条件未知与既有 night-01 记录同口径。
- `verify --require-decoded` 两份记录均通过；输入指纹、store 绑定与 `verify` 深度回读一致。
- 采集耗时 29.3 s（含解码与初始 store 构建），本机多次运行会因缓存不同而波动，不作性能基准。

### 阅读导出（同一份真机 200 ms 记录）

| 指标 | 数值 |
| --- | --- |
| 原始事件 / 指令 | 13,043,352 / 13,042,967 |
| 投影节点 | 13,983 |
| 重复块 / 隐藏指令 | 333 / 12,832,801（98.4%） |
| 库片段 / 未知归组 | 29,096 / 135 |
| C 正文 | 3.77 MB，84,032 行 |
| HTML | 14.67 MB |
| 序号映射 JSONL | 6.93 MB |
| 宏注释 | manifest `annotationCount=521`，C 正文宏名后 `/*0x..*/` 出现 10,169 处，未解析 112 |

浏览器渲染实测（`hw-200ms-reading.png`）：紧凑双栏、两栏语法高亮、宏值紧随名称、重复只用一行注释、翻页与序号定位正常。
对照说明：68 MB / 399 MB 来自 night-01 历史初始化采集，和本节新采记录不是同一次窗口，不能据此计算严格的优化倍数。相同记录、相同区间的独立对比见最终交付报告。

## 5. rc.2 便携包

当时仅完成 rc.2 构建：117,512,919 字节，SHA256 `f678da2eefa48331bb91b9535f127b1be85cc2ad999a4c9037c1ce1af3b0b1b9`，见 build-package.log。该包超过 GitHub 单文件 100 MiB 上限，不能直接作为 Pages 下载提交；后续正式交付已另行处理。这里没有可核实的 rc.2 便携验收完成记录。

## 6. ds 当时的失败归因（后续已重新审查）

1. `test_lifecycle…test_stop_during_saving_is_consumed_before_the_long_decode`：测试桩给 `Vis.cmd` 返回
   `'0 0.01'`，而 `capture_job` 对该命令取 `int()`，桩与实现口径不一致；本次合并只给 `etm/server.py`
   增加 `capabilities` 字段与 `/api/download` 的阅读分支，未触碰 `capture_job`。
2. `test_trace_store_adapter…test_index_and_history_show_capability_hint`：断言页面 `max="200"`，
   而源码自 `b86b860`（开放 1 s）起为 `max="1000"`；该页面内容在合并前的 `65f6540` 与本轮完全一致，属陈旧断言。
3. `test_trace_store_adapter…test_input_verification_covers_binding_inputs`：适配器 `input_verification()` 未改动，
   换用快照原生二进制复跑同样失败，后续 Codex 查明原生构建遗漏 session/sources 元数据绑定，已补齐绑定与回归；不能仅归为夹具问题。
4. 全量顺序下 `test_trace_store_write` 的 3 errors + 4 failures：单模块运行 25/25 通过，属既有测试隔离问题
   （同一进程内先跑其它模块后复现），后续 Codex 已定位 CLI.main 泄漏原生引擎环境的具体原因并修复，不能仅以单模块通过代替隔离审查。

## 7. 限制

- 阅读折叠是**展示投影**，不重新解码、不改变三态与质量结论；函数宏展开是编译期替换表达式，不代表运行时取值。
- HTML 词法高亮不做完整 C 语义分析；单文件离线 HTML 只能展开摘要/样例，完整明细查配套记录。
- 本轮未做 1 s 长窗真机验收，未测试拔插探针/强制杀进程的清理路径。
- 硬件只覆盖 STM32H743 + Windows x64。
- 观察到：同一 CLI 运行时内先执行 `target inspect` 再 `capture`，会以「仍有待恢复的Trace配置」拒绝；
  新建运行时直接 `capture` 正常。该行为属既有网页事务状态语义，本轮未修改，仅记录。

## 8. 证据索引

- `hw-capture-20ms-b.json` / `hw-capture-200ms.json`：两次真机采集结果（含 extent、cleanup、quality）。
- `hw-verify-200ms.json`、`hw-200ms.session.json`、`hw-200ms.target-state.json`、`hw-200ms.cleanup.json`。
- `hw-200ms.c`、`hw-200ms.c.manifest.json`、`hw-200ms.c.projection.jsonl`、`hw-200ms.html`、`hw-200ms-reading.png`。
- `final-regression-etm.log`、`isolated-store-write.log`、`re-run-*.log`、`regression-web-aftersync.log`、`selftest-snapshot/`。
- `sync-runtime.log`、`sync-page.log`、`build-package.log`、`portable-acceptance.py`、`portable-acceptance-*/`。
- 真机原始记录：`C:\Users\Administrator\.dsh\trace-runs\20261024-rc2\{smoke-20ms-b,hw-200ms}`（约 0.6 GB，含 KVDAT/ELF/store）。
