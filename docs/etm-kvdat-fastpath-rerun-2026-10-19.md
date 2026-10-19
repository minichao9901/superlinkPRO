# KVDAT 1.0.13 首屏/重解码耗时复跑（2026-10-19）

目的：核对 Codex 报告里“便携 EXE 首次 105.4 s、第二次 67.2 s”是否可复现，并判断是否 EXE 首次启动/冻结运行时导致。

## 输入与方法

- 记录：`etm/records/capture-de9b8e0ed24c41c68e5b374f3cb3c9c5`（200 ms、500 MSa/s、3,675,477 指令；把原始 KVDAT 复制到临时记录目录，`session.input` 改为 `capture.kvdat`，删掉 decoded/events/index 后重解）。
- EXE：仓库 1.0.13 便携包 `web/bridge/etm/downloads/etm-bridge-win64.zip`，解压到独立目录，`--port 8895 --no-browser --data-dir <temp>`。
- 源码后端：同一份记录，`python etm/server.py --port 8896`（非冻结 Python，使用同一套 `etm/` 源码）。
- 计时：`POST /api/decode/<sid>` 后轮询 `/api/jobs/<jid>` 到 `state=done`；同时记录 `events.jsonl`、`browse-v3.sqlite` 的 mtime。
- 环境：本机 Windows，E: 盘为仓库所在盘；未修改 Windows Defender 设置。

## 实测

| 运行 | 起 | 止 | 耗时 |
|---|---|---|---:|
| EXE 1.0.13 · 第 1 次 | 10:50:21.108 | 10:52:08.413 | **107.3 s** |
| EXE 1.0.13 · 第 2 次（同进程完整重解码） | 10:52:14.168 | 10:54:01.864 | **107.7 s** |
| 源码后端 · 第 1 次 | 10:54:52.358 | 10:56:37.736 | **105.4 s** |
| 源码后端 · 第 2 次 | 10:57:55.494 | 10:59:38.501 | **103.0 s** |

阶段观察（EXE 第一次）：`start` → `events.jsonl` 写满 **72.7 s** → `browse-v3.sqlite` 完成 **34.6 s** → job done；第二次同样是 events 写满约 **59 s** + 索引约 **30 s**，总时间仍约 107 s。两次都重建了 `browse-v3.sqlite`（mtime 10:53:45，晚于第二次 start）。

## 结论

1. **没有复现“首次 105 s、第二次 67 s”的差异**：同一 EXE 进程连续两次完整重解码都是 107 s 左右。
2. **EXE 并不比源码后端慢**：同一记录、同一环境，源码后端 105.4/103.0 s，EXE 107.3/107.7 s，差异在噪声内。
3. 因此“EXE 首次慢”不是冻结运行时/首次启动的固定特性；Codex 报告的 105→67 更像是**当时环境的一次性因素**（Windows Defender 对 1.68 GB events/293 MB 索引的扫描、磁盘缓存、后台负载）或**测量口径差异**，不是可以靠代码热启动消除的稳定差异。
4. 当前真正的耗时在**事件展开 + 派生索引**：约 60 s + 30 s；索引在每次完整重解码都会重建，不能用“第二次”跳过。

## 建议

- 若要做稳定优化，下一步应减少 `events.jsonl` 重写/索引重建（增量或复用已有 sidecar/index），而不是针对“首次启动”做预热。
- 需要确认 EXE 差异时，先在同一轮里加 `expand/ensure` 分项计时（Codex 的 `benchmark.py` 已有阶段输出），并在临时目录加入 Windows Defender 排除后复跑；不要只看总时长。
- 在排除环境因素前，不对用户承诺“第二次会变快”。
