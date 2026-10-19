# 真机 1 s 采集与验收清单（供用户 / Codex 执行）

状态：P6b 交付（2026-10-20）｜分支 `ds/etm-1s-p3p6`｜依据：`docs/etm-1s-native-pipeline-plan-20261019.md`
§3.1/§8 P6/§9/§10、`etm/AGENTS.md`「硬件与服务」、用户既有约定

> **本清单是操作与判据说明书，本身不是验收证据。** 真机 1 s 采集、烧录、探针/目标复位与 Kingst
> 采集由用户 / Codex 按硬件租约执行；P6b 只提供离线合成数据的规模化预检
> （`docs/validation/20261020-ds-etm-p6-scale-b2d8148/`）与本文档。
>
> **在真机 1 s 全部判据通过之前，默认采集上限必须保持 200 ms（`captureMaxSeconds=0.2`、
> `captureMaxSamples=1e8`），不得因合成数据或单次冒烟而放开。**

---

## 0. 一句话流程

```text
租约确认 → 构建/固定版本 → LA 深度与预算核对 → 200 ms 冒烟 → 500 ms 递进 → 1 s 采集（KVDAT 保存成功）
→ 指纹与元数据落盘 → 新链路（etm_decode --binary + trace_store_write）首次处理计时
→ TraceStore 查询计时/语义核对 → 前后端能力核对 → 逐项判据 → 证据归档 → 决定是否放开 1 s
```

任何一步失败：**保留原始记录与日志，不修改产品默认值**，按 §8 回滚/停止。

---

## 1. 前置条件（执行前逐项打勾）

| # | 项 | 判据/命令 |
|---|---|---|
| 1 | 硬件租约 | 按 `etm/AGENTS.md`：SWD 探针、烧录、目标复位、Kingst 采集由 Codex 独占验收；使用前确认协调目录租约，**不并发复位/采集** |
| 2 | 代码版本 | 记录 `git rev-parse HEAD`、分支、`git status --porcelain`（Dirty 清单）；P3 融合写出器 / P4 适配器 / P5 前端是否已合入并验证 |
| 3 | 原生解码器 | `etm/build-native/etm_decode.exe` 存在，记录 SHA256；确认支持 `--binary`；OpenCSD 版本（本机 1.8.3） |
| 4 | 固件 ELF | 与目标板**实际烧录一致**的 `firmware.elf`（记录 SHA256）；`session.json`/SWD 快照里的 ETM 配置与 ELF 匹配 |
| 5 | LA 深度 | 500 MSa/s × 1 s = **500,000,000 samples**（< KVDAT 32 位位置上限），**必须实测确认** LA 选中通道/深度/内存支持并 `save` 成功；不能只凭参数合法性推断 |
| 6 | 磁盘 | 预估 1 s 记录体积（200 ms KVDAT ≈ 92 MB → 1 s ≈ 0.46 GB；CSV ≈ 2.5 GB；新 store ≈ 80–90 MB；处理期需要 native.bin 等临时文件）；预留 ≥10 GB 并记录实测 |
| 7 | 上载预算 | 记录包 1 GiB / 单成员 1 GiB / 整包 2 GiB 等现有预算；先测新格式包实际大小再决定是否调整，**不整体取消尺寸/解压保护** |
| 8 | 浏览器/EXE | 独立 EXE（如适用）与浏览器版本记录；便携包与运行时快照同源（`sync-etm-runtime.py`/`build-etm-package.py`） |
| 9 | 环境 | 不改 Defender/电源/系统设置；记录 CPU 型号/负载采样（用于解释秒数波动） |

---

## 2. 采集步骤（用户 / Codex 执行）

1. **连接与识别**：确认探针（CMSIS-DAP）与目标板；读取 SWD 快照（cpuid/device/ETM 配置）；
   目标复位后确认 ETM 已使能（`traceReady=true`）。
2. **LA 配置**：500 MSa/s、采样时长 1 s、时钟通道 0、数据通道 1–4、触发/窗口按业务场景固定
   （与 200 ms 基线同 `fraction`/`glitchNs`）；开启 `keepOnRestart` 与需要的通道。
3. **递进采集**（不要直接跳 1 s）：
   - 200 ms：确认整链路可用（对照 P0-C/P3/P4 基线）。
   - 500 ms：检查实际深度/时窗是否等于名义值、同步/溢出/未知源、事件首尾。
   - 1 s：同上，并记录 LA `save` 成功、实际采样深度、实际时窗。
4. **保存与导出**：保存 `capture.kvdat`（优先）；导出 `capture.csv`（可选，用于交叉核对）；
   保存 `capture.csv.la.json`、`physical.json`、`framing.json`、`session.json`、`sources.json`。
5. **指纹**：对上述文件计算 SHA256 与字节数；记录采集开始/结束时间、LA 序列号、固件 ELF SHA256。
6. **只读归档**：原始记录放只读目录（不改写），处理在副本/新目录进行。

> 采集阶段**不要**同时运行解码/基准，避免 USB 与 CPU 争用污染时间窗（性能样本另起安静窗口）。

---

## 3. 新链路首次处理（离线，可脚本化）

计时边界与 P3/P6b 一致：**`etm.bin` 可读 → store + decoded.json 已发布、manifest 可打开**
（不含 LA 采集/保存、不含上传/导出、不含语义对照；语义对照在计时结束后单独跑）。

```powershell
# 1) 生成 store（t1 融合写出器：etm_decode --binary + 单遍落盘）
python -X utf8 etm/tools/trace_store_write.py write <记录目录> <记录目录>\store `
  --json --quiet | Tee-Object -FilePath run-1s-write.json

# 2) 打开并抽查（t4 适配器，与服务端 browse_store 同一对象）
python -X utf8 -c "import sys;sys.path.insert(0,'etm/tools');import trace_store_adapter as a; ^
v=a.adapter(r'<记录目录>');print(v.overview()['summary']);print(v.page('events',0,150)['total']);v.close()"

# 3) 服务端能力（确认默认未放开）
python -X utf8 -c "import sys;sys.path.insert(0,'etm');import server;print(server.CAPTURE_MAX_SECONDS,server.CAPTURE_MAX_SAMPLES)"
```

计时与资源：至少 **3 次**，报告全部样本 + 中位/范围；同时记录进程树峰值 RSS、磁盘写入量、
解码库/ELF/输入指纹；与合成 1 s 预检（`docs/etm-performance-baseline.md` §9）和 P3 的
200 ms（steady 42.95 s / 边界 41.04 s 中位）并排，但**不把 200 ms 数字外推到 1 s**。

---

## 4. 查询/语义核对（逐接口）

用 t4 适配器（`trace_store_adapter.adapter()`）或服务端 HTTP 接口，至少核对：

| 项 | 判据 |
|---|---|
| `overview` | 与旧解码摘要逐字段一致（白名单：`streamed` 事件源键、`traceStore` 块、store 新增索引键；包说明文本缺失已声明） |
| `page` 首页/中页/末页 + 随机页 | 行数/total/字段与旧 `RecordStore` 一致（150 行口径，末页不足 150 行允许） |
| `event(seq)` | 首/中/末/随机 seq 逐字段一致（含 callPath） |
| `navigate` | instruction/call/return/exception/issue/source 六模式 × 双向，与旧后端一致，记录 P95 |
| `window` / `function_timeline` | 全段与局部（如 `[0,200000]`、中段 200k）首开与缓存耗时；整段窗口不得退化为"扫 1,900 万条指令" |
| `issues`/`source` | 计数与内容一致（包文本缺失按白名单说明） |
| 浏览器/EXE | 实际页面首末页、跳转、源码、调用栈、包视图、导入导出、复解（独立 EXE 与浏览器分开列） |

语义门禁口径沿用 P1/P3/P4：事件全字段、analysis 重放、overview、查询逐接口 **0 差异**（白名单外）。

---

## 5. 验收判据（方案 §3.1，逐项打勾）

| # | 指标 | 目标 | 判定 |
|---|---|---:|---|
| T1 | 200 ms 新输入完整处理 | ≤3 s | 用同一构建实测；不达标写数字与原因 |
| T2 | **1 s 新输入完整处理** | **≤10 s，争取 5 s** | 同上；不计 LA 上传/保存/可选文本导出 |
| T3 | 首次打开已处理记录 | ≤1 s | HTTP 请求开始 → 概览+首 150 行+默认轨迹图响应完成 |
| T4 | 1 s 全区间首次统计 | ≤1 s | 冷窗口计算；HTTP 传输/JSON 解析另列 |
| T5 | 已缓存相同区间 | ≤100 ms | HTTP 请求至解析完成 |
| T6 | 局部定位/翻页/导航 P95 | ≤200 ms | 自动化查询序列；单列冷启动 |
| T7 | 1 s 全流程峰值 RSS | ≤2 GiB | 含子进程（进程树），不只 Python |
| T8 | 新派生数据体积 | ≤512 MiB | 代表性 1 s 样本；不含 KVDAT/原始流/ELF/可选完整导出；报告总落盘 |

**质量/完整性同时判**：未知执行/缺口/溢出/边界事件首尾、字节核算、包问题、同步丢失、
调用栈失效规则、区间裁剪（半个动态块）均与旧实现一致；不允许为了达标静默删数据或把
索引/统计挪出计时。

> 任何一项未达标 → **不放开 1 s**；保留 200 ms 默认与实验开关，报告实际分项数字。

---

## 6. 前后端能力修改点（**仅在全部判据通过后**由维护者执行）

1. 后端 `etm/server.py`：`CAPTURE_MAX_SECONDS = 0.2 → 1.0`（或按验收值），
   `CAPTURE_MAX_SAMPLES = 100_000_000 → 500_000_000`；`/api/capabilities` 自动跟随；
   同步检查 `capture` 参数校验、`_decode_job` 预算、上传/成员/整包尺寸预算。
2. 前端：`etm/web/index.html` 的 duration 最大值/说明文案；`etm/web/app/view.js` 已改为
   读取 `/api/capabilities`（缺失时回退 0.2 s/1e8），无需再写死；`history.js` 能力提示。
3. 测试：`etm/tools/tests/test_server_store_backend.py` 等能力断言、`web/` 的 ETM 页面同步
   （`node web/tools/dev/sync-etm-page.mjs`）、后端运行时快照
   （`python -X utf8 web/tools/dev/sync-etm-runtime.py --etm-root etm`，需 build-native）、
   便携包（`build-etm-package.py`，校验 provenance）。
4. 回归：`python -X utf8 -m unittest discover -s etm/tools/tests -p "test_*.py" -q`；
   前端 node 自测（62 项，1 项既有 skip）；`make test-etm`（注意是快照测试，不等于源码门禁）。
5. 发布材料：版本号、运行快照、二进制 SHA256、已知限制、回滚方式；**不 push、不部署**，
   按用户既有授权范围执行。

---

## 7. 必须记录的证据字段（缺一项都不得宣称达标）

| 字段 | 说明 |
|---|---|
| `Tested-Revision` / `Tested-Base` / `Tested-Dirty` | commit、基线、未提交清单 |
| `Validation-Result` / `Run-ID` | 每轮结论与唯一 ID |
| 输入指纹 | KVDAT/CSV、etm.bin、ELF、config、session/sources、SHA256+字节数 |
| 采集条件 | LA 序列号、采样率/深度、实际时窗、通道、fraction/glitch、触发、`save` 成功标志 |
| 环境 | 机型、CPU、内存、磁盘余量、负载采样、Defender/电源未改 |
| 计时 | 全部样本（≥3）、中位/范围、计时边界、是否与其它任务并发 |
| 资源 | 进程树峰值 RSS、磁盘写入量、派生数据体积 |
| 语义 | 对照脚本/结果、差异数、白名单逐项证明、未覆盖项 |
| 界面 | 浏览器/EXE 版本、实际页面操作记录（截图/JSON） |

---

## 8. 回滚与停止条件

- **回滚**：`CAPTURE_MAX_SECONDS/CAPTURE_MAX_SAMPLES` 恢复 0.2/1e8；前端 duration 与文案恢复；
  实验开关 `ETM_STORE_WRITE` 保持默认关闭（仅显式 `=1` 启用）；运行快照/便携包回退到上一已验证版本。
  已生成的 store 可保留（只读派生数据），**不删除原始记录**。
- **立即停止**（不继续采集）：LA 实际深度/时窗与设置不符、`save` 失败或截断、
  同步/溢出/未知源异常、USB 不稳定、磁盘不足、目标板复位异常、
  任一项语义对照出现白名单外差异。
- **不得**为了通过而：改宣传口径、把索引/统计移出计时、筛掉慢样本、用文件回放/计数回调
  冒充完整处理、用合成/拼接数据冒充真机连续 1 s。

---

## 9. 与离线预检的关系

- 合成 1 s 预检（`docs/validation/20261020-ds-etm-p6-scale-b2d8148/`、
  `docs/etm-performance-baseline.md` §9）只能说明"新链路在 1 s 量级输入下的阶段结构、
  内存/磁盘与查询行为"，**不能替代真机 1 s**；合成数据段间是重复 capture 的拼接。
- 真机验收通过后，才把 1 s 上限作为**该目标/该引擎**的已验收能力开放；其它目标/旧引擎维持
  其已验证上限。
