# 审查整改记录（2026-10-18，Codex 两份报告）

输入：
- `docs/validation/20261018-project-review-972fb8ab/review.md`（离线审查，12 项：6×P1 + 6×P2）
- `docs/validation/20261018-hardware-review-5fc491c1/hardware-review.md`（H743 真机补充：R02 真机复现、R03 源码/固件分离确认）

本文件记录**逐项处置**：改了哪里、怎么复现、还剩什么。验证目录本身是 gitignored，不进仓库。

## 结论速览

| 编号 | 级别 | 结论 | 改动 |
|---|---|---|---|
| R01 | P1 | **已修** | 长窗记录契约：`streamed.schema=3`（只合并事件流）+ 顶层不再引用单片 `capture.csv` 指纹 + `seq` 重编 + 索引成功后才标 done |
| R02 | P1 | **已修**（真机复现过） | `reset()` 放行判定：命中即成功；PPB 不落地才重试；停错处直接报错 |
| R03 | P1 | **已修（源码合入）** | 从外部仓 `2dc273c` 合入 nRESET(PA27)/nTRST(PA08) 分离 + 默认推挽；新增源码级回归 |
| R04 | P1 | **已修** | 同步清单补 `tools/slice_merge.py` |
| R05 | P1 | **已修** | LA 租约覆盖整个采集+分窗解码；解码期禁止新采集；stop 改为延后取消 |
| R06 | P2 | **已修** | 周期按每元素保留、按片 `analysis.timing.totalCycles` 求和；analysis 整段合并 |
| R07 | P2 | **已修** | 重建 web bundle；Pages 发布前加 `build-web --check` + `sync-etm-page --check` 守卫 |
| R08 | P2 | **已重建** | 按新快照重打便携包并更新 `downloads/manifest.json` |
| R09 | P2 | **已修** | 同步脚本不再先复制 `build-native/etm_decode.exe` |
| R10 | P2 | **已修** | 桥启动器在"包内无 demo"时跳过 seed 并提示 |
| R11 | P2 | **已修** | 页面补 `#etm-bridge` 地址输入（控制器早就读它） |
| R12 | P1 | **已修** | 旧 resumeack 不再当单步执行证据；无法确认时明确报错 |

## R01：长窗记录读取契约（最重）

**现场**：`export_slices()` 只把各片 `events.jsonl` 拼到顶层，`decoded.json` 直接继承第一片：
顶层没有 `capture.csv`、没有 `packets.jsonl`/`elements.jsonl`，`streamed.events` 仍是第一片的事件数。
于是 `result()` 校验指纹时找不到文件 → `/api/records`、`/api/browse/*` 全部 400；
即使补上 CSV，事件遍历也会因"预期 N 实际 M"失败。

**额外发现（比报告更严重）**：合并后的 `seq` 在片边界**撞号**。
`merge_slice_events` 用"片内最大 seq"推偏移，而 `decode_stream` 的 seq 从 0 起：
1 s 记录实测片 0 末条 `seq=4371169`，片 1 首条也是 `4371169` —— 浏览索引主键会直接冲突。

**修法**：
- `merge_slice_events()`：`seq` 按输出顺序重编（连续、无洞），`slice` 字段标注片号；`cycles` 原样保留；
- `stream_store.attach_streams()`：新增 schema 3 —— 只有合并后的 `events.jsonl`，packets/elements 留在各片；
- `export_slices()` 拆出 `finalize_slices()`：写 `decoded.json`（schema 3、无单片指纹）→ 建索引 → **最后**才
  `decodeStatus=done`；索引失败不会留下"看似完成"的记录；
- `record_store`：签名加 `slices.json`；schema 3 不做"整段输入字节 vs 根目录 packet 覆盖"的假缺口；
- `get_record`/`attach_sources`/`download` 全部按 schema 2|3 分流；
- 长窗下载契约（写进 `docs/long-window-slicing.md`）：`/raw?slice=K` 逐片取原始 CSV，
  `/json` 取分析结果，`/record` 明确拒绝并说明原因（单片数百 MB×N 片）。

**回归**：`etm/tools/tests/test_slice_contract.py`（2 项）——真驱动 `finalize_slices()`，
断言 records/browse/page/download 全通、事件数一致、周期求和正确、坏事件数会被拒。

## R02：复位放行把"正确命中"当失败

真机证据（`hardware-review.md`）：main / cdc_acm_init 两个起点最终 PC 都精确命中，
生产 `reset()` 仍报"放行失败：C_HALT 清不掉"并额外复位一次 —— 因为放行后 30 ms 只看 `S_HALT`。

**修法**（`etm/web/app/target.js`）：读到停机先看 PC ——
命中入口 = 成功收尾；仍停在复位 PC = PPB 写没落地，重写；停在第三处 = 直接 `unexpectedHalt`。

**回归**：`web/tools/selftest/etm-reset-hit.test.mjs`（3 例：命中/写不落地/停错处）。

## R03：固件源码漏合入 nRESET/TRST 分离

审查认定"板上固件已是新行为、仓库源码仍是旧行为"，两者不矛盾。现已把外部仓
`10adbc7 → 96e5c6f → 2dc273c` 的固件改动正式合入本仓（板级 4 个文件 + 两处注释同步）：

- `board.h`：删 `BOARD_PIN_nRESET2`，加 `BOARD_PIN_JTRST=PA08`、`BOARD_HAS_JTRST=1`、
  `BOARD_JTRST_OPEN_DRAIN=1`、`BOARD_HAS_NRESET_OD=1`；
- `DAP_config.h`：`PIN_nRESET_OUT` 只驱动 PA27；`PIN_nTRST_OUT/IN` 真驱动/回读 PA08（输出锁存）；
  PA08 pad 固定开漏 + 上拉；`DAP_APPLY_RESET_PAD_CONFIG` 不再碰 TRST；
- `DAP.c`：能力宏换 `BOARD_HAS_NRESET_OD`，**上电默认推挽**（`dap_nrst_open_drain=0`）。

**验证**：`python -X utf8 script_test/dap_reset_pins_test.py`（源码级独立性/默认模式/能力位），
并已用新源码完整重建 APP（93 步、链接/pack 成功）。

⚠️ 接线含义未变：目标 NRST 接 **J3[23]（PA27）**；J5.3（PA08）只接 TRST。
本次没有烧录真机（会改变正在用于验收的探针固件），电气/波形验收仍按原计划单独做。

## R04/R09/R10：便携运行时同步链路

- R04：`FILES` 补 `tools/slice_merge.py`（否则快照加载即 `ModuleNotFoundError`）；
- R09：不再从源树复制 `build-native/etm_decode.exe`（gitignored），改为最后一步直接链进快照，
  这样裸克隆的 cmake 自动构建真的能被执行；
- R10：`seed_demo()` 先看包内有没有 `demo/sample.etmrecord` + `decoded.json.gz`，缺则跳过并提示
  （与同步脚本"跳过内置示例，包仍可用"的降级口径一致）。

## R05：LA 租约

- `capture_job` 从 `Vis()` 起置 `_laLease`，任务结束才释放；
- 新采集的排他判据改为"是否持有租约"（含 `decoding` 阶段）；
- `stop` 在分窗导出/解码阶段**不下发 LA stop**（会从第二条连接打断导出），只置 `_stopped`，
  由分片循环在片边界收尾后清理。

**回归**：`etm/tools/tests/test_lifecycle.py` 新增 2 例。

## R06：周期语义

- 事件里的 `cycles` 是**每元素量**（区间末指令归属），不再加偏移；
- `slices[].cycles` 取该片 `analysis.timing.totalCycles`；
- `summary.totalCycles` = 各片求和（1 s 记录从 912 修正为约 3,600 万）；
- `decoded.json.analysis` 按片合并：函数统计/热点/火焰树/调用边/周期占比/CPI 全部重算。

## R11：桥地址输入

`web/index.html` 的 ETM 提示区补 `<input id="etm-bridge">`（控制器 `view.js` 一直在读它并写 localStorage），
回车即重新检测；占位与提示给出默认探测端口。

## R12：RISC-V 单步

`_waitResumed(pc0, timeoutMs, ackProof)`：旧 ack 清不掉时 `ackProof=false`，只认 running/不停机/PC 变化；
仍无法确认就明确报"resumeack 是上次残留……请先暂停再重试"，不再假报 `step` 成功。

**回归**：`web/tools/selftest/dbg-riscv-step-ack.test.mjs`（3 例）。

## 测试与跳过项（诚实口径）

- ETM Python：129 项通过（5 项 skip —— 依赖 gitignored 的封存样本/构建产物，干净克隆里没有）；
- ETM JS：44 项，43 通过 + 1 skip（裁剪后的 SDK 源码树）；
- 网页离线：`make check`（语法/液体/bundle/共享驱动/ETM 副本）+ `make test` 全绿；
- 固件主机测试：`make test-host` 全绿（含新增 `dap_reset_pins_test.py`），APP 重建成功。

**未做（需要真机/会改变现场）**：烧录新固件、TRST 波形实测、断电后首次采集的复位验收、
真机浏览器全流程。这些属于"验收"，不是"未修代码"。
