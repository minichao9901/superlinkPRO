# R2 暂停交接说明（给 Codex/接手人）

- 时间：2026-10-20 晚｜分支 `ds/etm-1s-r0r3`｜**未 push**
- 用户要求：暂停工作、做 commit、由 Codex 接手；不再继续推进 R3。
- 团队：AgentTeams `etm-1s-r0r3` 已停止/归档，成员 p1/p0/p3 停止；目标已暂停。
- 本文件提交时 HEAD：见 commit（"R2 checkpoint"）；前一已提交提交 `1817b95`。

## 1. 已提交、已验证的部分

### R0+R1（第一批，已交付/独立验证）
- 提交：`7feff7d` R0-A、`2e1e18c` R0-B、`41172fd` R1 设计、`70b9f91` R1-A、`1765588` R1-B、`32b0bc1` R1-A2、`350b413` 交付说明。
- 文档：`docs/etm-1s-r0r1-delivery-2026-10-20.md`；证据 `docs/validation/20261020-ds-etm-r01-final-r1a2/` 等。
- 关键实测：19.86M 冷全段 window/timeline 低负载 0.23–0.35 s；61+10 窗口 0 差异；导航 P95 ≤11 ms；真实浏览器完整首屏 243 ms；后端 459 项 OK、JS 66 项 OK。默认 200 ms 上限未放开。

### R2（部分完成，性能目标未达）
- `a696e21` prescan/诊断：200 ms 0.090 s、19.86M 0.527 s，计数 0 差异。
- `94105bc` 静态字典（59/59、1719/1719 0 差异）+ 块 12 PC/line 表。
- `abc1abb` 块 12 v2 读取 + 发布编排（默认 Python、显式 native、原子发布、失败回退）。
- `2d0b8cf` **blocks 1–11 + analysis 原生达标**：200 ms 0.503–0.595 s（中位 0.517）、19.86M 2.82 s；analysis/80 navigate 0 差异；Lead 复跑 0.605/2.829 s。
- `6b505a2` 块 12 v2 容器/挂载（64B 目录；无参考转写；capabilities 只 build）。
- `5117448` 原生 PC/line + cell anchor（26/26 cell 0 差异、50 navigate 0）。
- `c961a3a` C1a facts（RUNS/IDS/STATES/CYCLES）：10 ms 2.17 MB/0.0066 s；200 ms 39.64 MB/0.0618 s。
- `1cf20cb` C1b EVENTS + decode 定位：200 ms 383 条与 v1 逐条 0 差异。
- `5b86d44` C2 NumPy 原型：10 张 cell/st/hotspot 与 v1 0 差异；200 ms 派生 0.31–0.43 s；并修复 v1 `flush_cell` 在 reset 后拷贝导致 6 张 cell 表恒零的缺陷。
- `01e0190` C2b facts v2 消费：fs/ps/node/cell_anchor/ev/h_file 派生，真实 10 ms/steady/boundary 0 差异；CLI 0.024/0.44/0.48 s。
- `1817b95` **C1d facts v2**：PATH_RUNS/NODES/LABELS、EVENTS 40B+EVTEXTS、RUNS 40B runSkip；四输入 problems=[]；**19.86M facts 235,905,674 B sha `ba5b780b…`**；19.86M facts 写 1.179 s、遍历+写 6.77 s、峰值 RSS 445 MB（base 80.3，含 IDS/RUNS 临时量）。
- t48 独立复核通过（facts v2 10 ms/200 ms steady/boundary 0 problems；C2 v2 25 张可比表 0 差异；1s facts 计数/状态/事件/ps/first_seq 0 差异）。证据 `docs/validation/20261020-ds-etm-r2c12-verify-939c87b/`。

## 2. 本次 checkpoint 中的 WIP（未验证，需接手）

1. **t49（p1，C2c，未完成）**：`etm/tools/native_facts_index.py` + `test_native_facts_index.py`
   - 目标：默认 window/timeline + nav + remaining tables（unknown_seq/not_taken_seq/call_pair_seq、cell_node_value*、node_self_*、hs_cycles_*），用 19.86M facts 全规模派生。
   - 状态：**未完成、未验证**；本次提交时 `test_native_facts_index*.py` 14 项中 **1 项失败**（WIP 中间态，勿当作通过）。
2. **t51（p3，packets 修复）**：`etm/native/storebuild.cpp` + `test_native_storebuild.py`
   - 内容：`on_packet` raw 偏移 `body.data()+22` → `+17`（body 已去掉 5B 记录头；原实现越界 5 字节，packets 块字节不确定）。
   - 状态：13 项 native 测试 OK；**尚未在 10 ms/200 ms 上做 packets raw 与参考逐条 0 差异的最终核对**（p3 已给出参考样本验证，但未完成证据目录）。

## 3. 关键未完成项（R2 收口前需做）

1. **C2c**：默认 window/timeline + 导航 + 剩余表；默认/50 窗口/5 timeline/50 navigate 0 差异、快路径命中、无 sidecar。
2. **C3**：新输入闭环——native.bin → facts → NumPy 派生 → v2 块 12 → manifest 发布 → 现有查询/网页；记录 200 ms 与 19.86M 端到端墙钟（含首开、发布、索引）。
3. **t50**：C2c 独立验证（默认视图/导航/19.86M 派生）。
4. **t51 最终核对**：packets raw 0 差异 + 确定性。
5. **R2 收口**：全量回归（后端 459+ 新增、JS 66）、R2 交付文档（真实数字/未达标项/剩余优化）、提交；默认仍 Python 引擎、200 ms 上限不放开。
6. R3（原生前端接入）**已按用户要求暂停**，未开始。

## 4. 关键风险（如实）

- **19.86M ≤4 s 目标目前看很难达成**：blocks+analysis 2.82 s + facts 遍历/写 6.77 s（已超 4 s）+ NumPy 派生/编码/默认视图；Codex 已明确 4 s 是阶段预算，未达可保留中间实现并继续 profile，不得虚报。
- R3 的 1 s 前端当前 8.35 s，即使 R2 达标，全链 ≤10 s 仍需要前端优化。
- v1 参考 store 的 6 张 cell 表在修复前恒零；对 19.86M 的比较需用“自洽/结构/抽样+慢路径”或重建参考，不能直接逐表相等。
- 原生“完整 store”尚未发布：capabilities 仍只 `prescan/diagnose/build`；t17 的 native 发布接入仍是显式 unavailable 骨架（默认 Python）。

## 5. 证据与输入（本地、gitignored）

- Codex 路线文档：`docs/etm-r2-unblock-20261020.md`
- facts 输入：`tmp/r2c1a1-10ms/facts.bin`、`tmp/r2c1a1-200ms/facts.bin`、`tmp/r2c1b-*/facts.bin`、`tmp/t45/evidence/scale-1s-facts/facts.bin`（235,905,674 B，sha `ba5b780b…`）
- 证据目录：`docs/validation/20261020-ds-etm-r2a-native-350b413/`、`...-r2a2-blocks-350b413/`、`...-r2b3d2-anchorfix-350b413/`、`...-r2c1-facts-939c87b/`、`...-r2c1a1-facts-939c87b/`、`...-r2c1b-facts-939c87b/`、`...-r2c1d-facts-939c87b/`、`...-r2c2-numpy-939c87b/`、`...-r2c2b-v2-5b86d44/`、`...-r2c12-verify-939c87b/`、`...-r2c1e-packets-939c87b/`（t51，若已生成）。
- 参考 store/输入：`tmp/p3-audit/stores/cli-full-1`、`tmp/r2-bench/runs/{steady,boundary,1s}/native.bin`、`docs/validation/20261020-ds-etm-r2b-static-350b413/static-dict-*.bin`。

## 6. 接手指引

1. `git status` 应为干净（本 checkpoint 已提交）；先跑 `python -X utf8 -m unittest discover -s etm/tools/tests -p "test_native_facts_index*.py" -q`，修掉那 1 项 WIP 失败。
2. 完成 t49 的 C2c（默认视图/导航/19.86M 派生），再按 C3 打通新输入闭环；期间保持“不逐指令 Python、不调用旧 build_tables、不用参考表当输入”的纪律。
3. 用 `docs/validation/20261020-ds-etm-r2c12-verify-939c87b/verify_v2.py` 作为独立验证框架；最终 R2 验收绑定固定 HEAD、重建 native exe。
4. 不 push、不重打包（用户尚未要求发布）；默认 Python 引擎与 200 ms 上限保持不变。
