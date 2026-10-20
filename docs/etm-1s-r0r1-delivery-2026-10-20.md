# ETM 1 s 下一轮：R0+R1 交付说明（第一批，2026-10-20）

- 分支：`ds/etm-1s-r0r3`（基线：Codex 审查 `1685c01` 后的指令文档 `1891916`；**未 push**）
- 最终 HEAD：`32b0bc1`（R1-A2 冷打开优化）
- 依据：`docs/etm-1s-next-steps-20261020.md`（Codex R0→R3 指令）；本批只交 **R0+R1**，R2 原生 builder 与 R3 前端接入留第二批
- 执行：AgentTeams（Lead + p1-store / p2-native / p0-baseline）
- 默认能力**未放开**：`captureMaxSeconds=0.2`、`captureMaxSamples=1e8`、`ETM_STORE_WRITE` 默认关；未重打包、未同步运行时快照、未操作硬件

Tested-Revision: `32b0bc1`（工作树干净）
Tested-Base: `1891916`
Tested-Dirty: false
Validation-Result: pass（R0 两项正确性 + 首屏口径；R1 冷窗口/时间线、50+ 窗口对照、导航 P95、结构检查、浏览器完整首屏）
Hardware-Test: not-run（全部为既有真实 200 ms 记录 + 显式标注的合成 1 s store）

## 1. 提交清单

| 提交 | 内容 |
|---|---|
| `7feff7d` | R0-A：store 换代缓存隔离 + 输入指纹全量核验 |
| `2e1e18c` | R0-B：首屏口径与加载顺序（基础行不被慢图阻塞） |
| `41172fd` | R1 设计：查询索引/聚合/导航 + 可行性原型（入库文档） |
| `ebe71d9` | R2 设计：原生 store builder（入库文档，供第二批） |
| `70b9f91` | R1-A：持久化查询索引（块 12）+ 冷 window/timeline 快路径 |
| `1765588` | R1-B：导航快速路径（instruction/PC/source + NAV） |
| `32b0bc1` | R1-A2：冷打开余量（后台整份 sha256 + nav/issues 惰性解析） |

## 2. R0：正确性与口径

### 2.1 R0-A 缓存换代 + 输入指纹（`7feff7d`）

- `window/timeline` 缓存键绑定实际 store 代（`generation|schema`）；换代/关闭清理旧代；在途查询登记，旧代结果不写入新代缓存；旧 store 延迟到最后一个在途查询释放后再关。
- 删除 `MAX_HASH_BYTES=64 MiB` 静默降级：凡 manifest 记录 sha256 的绑定输入都按 1 MiB 分块整份核验（记 bytes/seconds）；**同长度改写 + mtime 不变可检出**；`--no-hash-inputs`（skipped）产物显式 `verified=false` 并写入 `overview().traceStore.inputHash`，不当作已核验。
- 复现：Codex `probe_cache` stale true→false；67,108,865 B 错误 sha256 由"放过"变"拒绝"；nohash 产物 verified=false 且可浏览。

### 2.2 R0-B 首屏口径与加载顺序（`2e1e18c`）

- 默认轨迹图与 overview/首页行解耦（不再 `await`）；同 key 请求合并、整秒进度、失败/超时只标记图表可重试；**超时值与采集上限未改**。
- 口径：`objectLayerPartial`（open+overview+首页 page ≈0.51 s）与 `firstScreenWithTimeline`（含默认图、真实加载顺序）分开；后者在 R1 前判未通过。
- 真实浏览器 A/B（无头 Edge + Flask）：3× store 首批行 **192 ms**（旧 19,916 ms）；1 s 规模首批行 **954 ms**、45.9 s 图表超时后表格保留（旧 60 s 无行）。

## 3. R1：已有 store 的快速冷查询

### 3.1 实现

- **块 12 `BLK_QUERY`**（`optional_features 0x0001` + `meta.queryIndex` + manifest 元数据）：57 张表——函数/路径区间、4096 指令单元格聚合（热点/火焰/周期/调用/IRQ/事件）、稀疏未知/未成立/周期/调用、事件表、PC/行区间导航表、默认全段视图；旧记录无块 12 回退慢路径，索引损坏记 issue 并回退。
- **导航**：instruction ±1 用事件位次/单元格直接定位（0 次 `_expand`）；NAV 用稀疏表；PC 用 `PC_CELLS` CSR + ≤2 单元格有界解码；source 用 `LINE_RUNS` 二分。PC/line 表以独立 nav sidecar（`<store>/nav-index/<generation>.nav`，magic/version/代/CRC）承载，缺失/损坏显式回退；**R2 builder 需合并进块 12**。
- **构建命令**：`python etm/tools/trace_store_index.py build <store>`；`python -m trace_store_nav build --store <store>`。

### 3.2 查询与冷启动实测（19.86M 事件合成 1 s store）

| 项 | 结果 |
|---|---|
| 全段 window（慢→快） | **326.1 s → 0.0238 s**（慢路径 oracle 完全相等） |
| 全段默认 timeline | **339.7 s → 0.0114 s** |
| 200 ms 真实记录 window | 42.75 s → 16.7 ms |
| 纯查询/热查询 | 0.029–0.047 / 0.014–0.023 s；热 0.9–1.4 ms |
| 冷 open+window（t11，低负载 10 样本） | **0.230–0.347 s**（≤0.45，无 >0.6） |
| 冷 open+timeline（t11，低负载） | **0.213–0.326 s**（≤0.5） |
| 冷（中负载 5 样本） | window 0.321–0.346 s；timeline 0.298–0.317 s（≤1.0） |
| 高负载（t10 26 样本） | 无一 >0.8 s、无一 >1 s |

> 说明：t6 在 t10 优化前发现中高负载 15 样本中 3 个 1.007–1.132 s（open 校验主导）；t10 优化后 t11 独立复验全部 ≤0.347 s。t11 绝对值略高于 t10（OS 缓存/共享机），但门槛均有充足余量。

### 3.3 语义、结构、可用性

| 项 | 结果 |
|---|---|
| 窗口对照 | 每 store **61 window + 10 timeline**（局部/跨块/跨检查点/跨调用/异常/首尾/无指令/IRQ 起点/事件+1/call 起点）与 `TRACE_STORE_SLOW=1` **0 未解释差异**；19.86M max 0.270–0.333 s |
| 导航对照 | 292 条 fast / 64 对慢路径 **0 不符**；重复 P95 最大 PC **5.9 ms**、首次 PC **11.0 ms**、instruction 0.007–0.008 ms、source 0.075 ms（≪200 ms） |
| 结构检查 | fast 全段 window/timeline 的 `_expand`、逐指令事件字典、`CallTracker.feed_instruction` **全 0**（慢路径控制组计数正常） |
| 完整性 | 1 字节篡改被整份 sha256 拒（普通/eager）；块 CRC 损坏、索引损坏、sidecar 缺失/代不符均正确回退；`--no-hash-inputs` 标未核验 |
| 可用性 | 首中末页/搜索/隐藏未成立/event/source/packets 可用，fast-slow 0 差异 |
| 浏览器（真实） | **完整首屏（含默认轨迹图）243 ms**（rows 243 / chart 243）；人为延迟 `/timeline` 5 s 时 rows 仍 213 ms（基础行不被阻塞） |

### 3.4 构建成本（R2 必须纳入，不计入查询门槛）

- 19.86M：查询索引 **115.5 s / 352.2 MiB / 5.59 MB**；nav sidecar **38.4 s / 407.9 MiB / 30.8 MB**（独立文件，未并入块 12）。
- 3×：索引 3.66 s / 230 KB；sidecar 1.51 s / 0.94 MB；200 ms：索引 15–21 s、sidecar 4.5 s / 6 MB。

## 4. 未达标 / 限制（如实）

1. **R2/R3 未实现**：原生 C++ store builder 与原生前端接入是第二批；当前 19.86M 首次处理仍需 Python 索引构建（115.5 s）与 sidecar（38.4 s），远未到 §6 的 ≤4 s/≤10 s。
2. **冷门槛依赖负载**：t10/t11 后低/中负载余量充足；本轮未做"任何负载下都 ≤1 s"的承诺（高负载样本由 t10 覆盖，无一 >0.8 s，但共享机波动存在）。
3. **非默认参数**：全段算法路径 0.763 s（空闲）/1.06 s（负载）>1 s；`search/hideSkipped` 的 timeline 仍走慢路径（19.86M 209.8/173.3 s），只保证语义不变、不计入门槛。
4. **导航覆盖边界**：19.86M 全 store 的 PC 无命中/回绕慢 oracle 未直跑（O(store) 数百秒），用 3× 全量 + 有界 scope + 结构验证覆盖；R2 重测。
5. **导航 sidecar 独立于块 12**：符合上游"可兼容新增索引文件/块"，但 R2 需合并进块 12 并删除 sidecar 步骤。
6. **发布链未动**：未重打包、未同步 `web/etm` 页面副本与运行时快照；R3 实验包时统一处理（版本/标识不得复用 1.0.13）。
7. **真机 1 s 未采集**：全部为合成规模 store（拼接丢帧 −5.74%），不能外推为真机性能/无损保证。

## 5. 回归与打包

- 全量后端：结果见 §6（`tests-summary.json`）。
- ETM JS：`node --test etm/tools/tests/*.test.mjs`（66 项，65 通过/1 既有 skip/0 失败）。
- 定向：index 19、adapter 37（skip 1）、test_trace_store* 196（skip 1）、server backend 27（skip 2）、nav 13、native_query 3 均 OK。
- 未重打包、未同步运行时快照（按批次约定留到 R3）。

## 6. 证据（本地，按仓库约定不入库）

- `docs/validation/20261020-ds-etm-r0-storefix-1891916/`（R0-A）
- `docs/validation/20261020-ds-etm-r0-firstscreen-1891916/`（R0-B）
- `docs/validation/20261020-ds-etm-r1-design-1891916/`（R1 设计/原型）
- `docs/validation/20261020-ds-etm-r1-protorepro-1891916/`（原型独立复现）
- `docs/validation/20261020-ds-etm-r1-core-1891916/`（R1-A）
- `docs/validation/20261020-ds-etm-r1-nav-1891916/`（R1-B）
- `docs/validation/20261020-ds-etm-r1-verify-1891916/`（t6 独立验证）
- `docs/validation/20261020-ds-etm-r1-coldopen-1765588/`（R1-A2）
- `docs/validation/20261020-ds-etm-r1-coldverify-r1a2/`（t11 复验，绑定 32b0bc1）
- `docs/validation/20261020-ds-etm-r2-design-1891916/`（R2 设计）
- `docs/validation/20261020-ds-etm-r01-final-r1a2/`（本批收口：report/commits/tests/sha256）

## 7. 下一步（第二批）

1. **R2 原生 builder**：按 `docs/etm-r2-native-builder-design.md` 实现 `etm_storebuild`（prescan → 静态字典 → build），一次遍历产出 store 块 1–11 + 块 12（含 PC/line 表）+ 默认视图；目标 200 ms ≤1.5 s、合成 1 s ≤4 s（设计估算 4.18–4.78 s，有超风险，需批量化/静态缓存或 R2b 融合）。
2. **R3 实验全链路**：接入原生物理前端（显式开关、仍 200 ms 上限、阶段计时/引擎标识），验证 200 ms ≤3 s、1 s ≤10 s；1 s 前端当前 8.35 s，需进一步优化。
3. 真机 1 s 验收在软件门槛通过后按清单执行；默认 200 ms 不提前放开。
