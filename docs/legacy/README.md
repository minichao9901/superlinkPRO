# 三个源仓库的归档（删本地副本前抢救出来的东西）

2026-10-18 把合并前的三个仓库本地副本删掉（约 27.1 GB），删前按"能不能再生成"筛过一遍。
真正的源码与文档已经在 `E:\superlinkPRO`（合并仓）里，这里只放**不可再生成或成本较高**的文本证据。

> 删除结果：`E:\Share\github\akaLinkPro` 与 `E:\web-etm-trace` **整目录已删**；
> `E:\web-serial-rtt-tools` 的**内容已全部删除**（0 文件），只剩一个空目录被 DSH 宿主进程的
> 启动 CWD 占着（删不掉是 Windows 的 CWD 语义，不是还有数据）——重启 DSH 或换个会话后
> `rd /s /q E:\web-serial-rtt-tools` 即可清掉。
> 三仓的提交在删除前都已推送远端（akaLinkPro `2dc273c`、web-serial-rtt-tools `089e383`、
> web-etm-trace `12746fc`），没推送过的只有下面这些补丁里的未提交改动。

| 源仓库 | 处置 |
|---|---|
| `E:\Share\github\akaLinkPro`（113 MB） | 全部提交都已推送远端（`5301evk_akaLinkPro`，含今天的 `10adbc7`/`96e5c6f`/`2dc273c`），本地副本可安全删除 |
| `E:\web-serial-rtt-tools`（9.8 GB） | 已推送；未提交的 10 个已跟踪改动 → 本目录 `web-serial-rtt-tools-uncommitted.patch`；500 个未跟踪项（2.4 GB 验收产物：`.kvdat`/`.elf`/`decoded.json`）**未保留**（可由对应 validation 脚本重跑） |
| `E:\web-etm-trace`（17.8 GB） | 已推送；那条 stash（"H743 prior Codex experiments"）→ `web-etm-trace-stash-H743-codex.patch`；未提交的 `fw.hex` 差异 → `web-etm-trace-uncommitted.patch`（只是重新编译产物） |

## 文件说明

| 文件 | 来源 | 为什么留 |
|---|---|---|
| `web-serial-rtt-tools-uncommitted.patch` | 该仓 `git diff` | 10 个已跟踪文件的未提交改动（含 ETM 集成、`start-etm.bat`、CDP 套件调整），远端没有 |
| `web-etm-trace-stash-H743-codex.patch` | 该仓 `stash@{0}` | 前一阶段 H743 Codex 实验（`main.c` +82 行、`usb_dc_low_level.c`、`boot_probe.c`、`ld`、web `clock/target/trace-options`）——**合并前保留的实验版本**，远端没有 |
| `web-etm-trace-uncommitted.patch` | 该仓 `git diff` | 仅 `fw.hex`（重编译产物） |
| `serial-h743-web-review-2026-10-17.md` | 未跟踪文档 | H743 网页侧审查结论（11.7 KB） |
| `serial-worktree-before.patch` / `serial-final-worktree.patch` / `serial-dbg-image-20261016-worktree.patch` / `serial-ram-fill-20261016-worktree.patch` | 验收目录 | 各次验收当时的**工作区快照**（文本差异），用于回溯"当时测的是哪份代码" |
| `serial-ds-log.txt` / `serial-codex-log.txt` | 仓库根 | 会话日志（很短） |
| `akaLinkPro-ai-guides/*.md` | 未跟踪 | 双 AI 结对编程指导 / Git 日志与验证证据约定 |
| `akaLinkPro-zcodeignore.txt` | 未跟踪 | 原 `.zcodeignore`（换名成 .txt 以便普通浏览） |

## 没留的东西（及理由）

- 2.4 GB `docs/validation/**` 二进制产物（`.kvdat`、`.elf`、`decoded.json`、`site.pyc`）：都是**可由脚本重跑**的验收证据；文本结论仍在合并仓 `docs/validation/` 与 `docs/*.md` 中。
- 各仓的 `build/`、`node_modules/`、`tmp/` 中间件。
- 三个仓库的 `.git` 历史：远端都还在（GitHub），需要时重新 clone。

## 若要恢复某个补丁

```bash
# 例：recover the pre-merge H743 experiment
cd E:/superlinkPRO
git apply --check docs/legacy/web-etm-trace-stash-H743-codex.patch   # 先干跑
git apply docs/legacy/web-etm-trace-stash-H743-codex.patch
```

注意：补丁是基于**当时仓库的根目录**生成的，应用到合并仓时路径可能已经变化（`web/etm/...` vs `etm/web/...`），必要时用 `-p` 调整或手工挑。
