# web/ 迁移记录（web-serial-rtt-tools → superlinkPRO/web）

| 项 | 值 |
| --- | --- |
| 来源仓库 | `E:\web-serial-rtt-tools` |
| 来源分支 | `main` |
| 来源 commit | `089e38392127029025c9c4bcd6d803ad60df3742` |
| 迁移时间 | 2026-10-17 22:04（+08:00） |
| 迁移方式 | **逐文件复制**（.NET `File.Copy`，清单来自 `git -c core.quotepath=false ls-files`），**不是** `git subtree` / `git archive` |
| 依据 | `docs/merge-map.md` §2.2B / §5.1 / §6.2；`docs/decisions.md`（D-4 不收敛 docs、D-6 不带 git 历史） |
| 目标位置 | `E:\superlinkPRO\web\`（**整树平移**，内部 `tools/ app/ bridge/ samples/ docs/` 相对引用零改动） |

> 为什么必须整树平移：本目录的 `Makefile` 有 293 处 `tools/` 相对引用，`tools/**` 里还有 416 处
> `app/`、94 处 `index.html`、38 处 `bridge/`、32 处 `samples/` 等相对引用 —— 把它们拆到仓库根
> 就要改 ≈600 处。整树平移后这些引用**全部保持有效**。

---

## 1. 搬了什么（对账数字）

| 项 | 文件数 | 字节 |
| --- | ---: | ---: |
| `git ls-files` 跟踪文件 | 2035 | 112,425,879（107.22 MiB） |
| ＋ 未跟踪但必须带走的文档 `docs/h743-web-review-2026-10-17.md` | 1 | 11,967 |
| **合计** | **2036** | **112,437,846（107.23 MiB）** |

对账手段（可复核）：

1. **零覆盖**：复制前对每个目标路径断言不存在 → 实测 `collisions=0`；
2. **零差异**：复制后对 2036 个文件逐个 `Get-FileHash -Algorithm SHA256` 与**源工作区**比对 → `mismatches=0`；
3. **集合相等**：目标实际文件集 == 期望文件集（`extra=0 / missing=0`）；
4. 含 **11 个非 ASCII 文件名**（`docs/交接-HPM6800EVK-RTT与烧录.md`、`docs/逻辑分析仪攻略.md`、
   `samples/anim/花园.mp4`、`samples/panel_init_many/extra_parts/*荣耀屏*.png` 等），全部到位。
   ⇒ 生成清单必须 `git -c core.quotepath=false ls-files`，否则中文名被转义成 `"\344\272\244…"` 会漏文件。
5. **入库校验**：`git add -- web` 之后 `git diff --cached --name-only` = **2037 / 2037**（含本文件），
   `web/` 下「磁盘上有但没被 add」的文件 = **0**（详见 §5.1 的 121 个 `.log`/`.csv` 问题）。

## 2. 8 个「未提交改动」是按工作区状态带过来的（不是 HEAD 版本）

源仓 `git status` 在迁移时是 `8 modified + 4213 untracked`。`git ls-files`/`subtree` 都只认 HEAD，
所以这 8 个文件是从**工作区**复制的（目标文件 SHA-256 == 源工作区；与源 HEAD 版本不同，源仓 `git diff` 可复核）：

| 文件 | 差异 | 归属 |
| --- | --- | --- |
| `app/etm/style.css` | +1 / −1 | ETM 内嵌页样式 |
| `docs/etm-integration.md` | +13 | ETM 集成说明 |
| `start-etm.bat` | +17 / −6 | ETM 启动脚本 |
| `tools/dev/build-etm-package.py` | +8 / −5 | ETM 打包器 |
| `tools/dev/sync-etm-runtime.py` | +3 / −3 | ETM 快照生成器 |
| `tools/selftest/etm-history-races.test.mjs` | +9 | ETM 测试 |
| `tools/selftest/etm-ui-harness.mjs` | +1 / −1 | ETM 测试 |
| `tools/selftest/etm-ui-sequences.test.mjs` | +10 | ETM 测试 |

合计 **+62 / −16 行**，全部是 ETM 相关真源码（不是同步/打包产物）。

另带 1 个**未跟踪的真文档**：`docs/h743-web-review-2026-10-17.md`（H743 网页侧审查）。

## 3. 刻意没搬（以及依据）

| 路径 | 规模 | 依据 |
| --- | --- | --- |
| `tmp/` | 5.2 GB / 12,409 文件（含 `etm-package-*` 打包产物、两个 `old-*` detached worktree） | 源仓 `.gitignore: tmp/`；merge-map §1.3 |
| `node_modules/` | 13 MB / 1,066 文件 | 源仓 `.gitignore` |
| `docs/validation/` **未跟踪**部分 | 4,206 文件 / 2,494 MB | merge-map §5.1B（验收产物，可重新生成） |
| `.agent-teams/` | 2 目录 | **团队状态，绝不入库** |
| `.git/` | — | D-6：不带走 git 历史（源 commit 见本文头部） |
| `validation/etm/`、`codex-log.txt`、`ds-log.txt` | 5 项 | merge-map §5.1B |

⚠️ 注意区分：`docs/validation/` 下 **894 个「已跟踪」验收记录是照搬的**（它们是证据，不是产物）；
未跟踪的 4,206 个才不搬。仓库根 `.gitignore` 已按这个口径配置（放行已跟踪记录、忽略未跟踪产物族），
所以 `git add web` 不会丢这 894 个文件。

## 4. 怎么起服务 / 跑测试（都不需要硬件）

| 用途 | 命令（在 `web/` 目录下） | 说明 |
| --- | --- | --- |
| 起静态服务 | `make serve` | 8899，`node tools/dev/serve-nocache.mjs` |
| 起测试浏览器 | `make open` | CDP 9333，profile 在 `%TEMP%\edge-rtt-tools-test` |
| 纯 Node 套件 | `make test` | rtt / gen-parity / hid / dwarf / scope / bridge / flash / hpm / spi / i2c / dbg … |
| 语法与守卫 | `make check` | 语法检查 + `check-liquid`（所有 `.md` 不得含 Liquid 定界符） |
| 单个套件 | `node tools/selftest/flash-image.test.mjs` | 本次已跑通：**28 通过 / 0 失败** |
| ETM 集成 | `make serve-etm`（8878）/ `make test-etm` / `make test-etm-integration` | **依赖 `../etm/` 落地**（需先完成 ETM 迁移） |

CDP 页面套件共用同一套 8899/9333 服务与浏览器，**一次只跑一个**（并发会报
`Inspected target navigated or closed`）。

## 5. 本次校验记录（t3）

- `node --check` 抽样 **12 个**文件（`app/core`、`app/rtt`、`app/spi`、`app/dbg`、`app/etm`、`app/main.js`、
  `app/flash`、`tools/dev`、`tools/selftest`、`bridge`）→ **全部通过**；
- `node tools/selftest/flash-image.test.mjs` → **28 通过 / 0 失败**（exit 0）；
- 关键路径在位：`index.html`、`app/**`（含 `app/app.css`、`app/site/main.js`）、`tools/selftest/**`、
  `tools/dev/**`、`bridge/**`（含 `bridge/rtt-bridge.mjs`、`bridge/etm/**`）、`samples/**`、`docs/**`、
  `Makefile`、`package.json`、`package-lock.json`、`start-web.bat` / `start-etm.bat` / `kill-web.bat`、
  `.gitignore` / `.gitattributes`；
- 目标树内**没有** `tmp/`、`node_modules/`、`.agent-teams/`、`captures/`、`records/`、`__pycache__/` 等产物目录；
- 小笔误记录：merge-map §1.2 把 `app.css` 列在根级，实际它在 **`app/app.css`**（以 `ls-files` 为准，已搬入）。

### 5.1 对源文件的一处最小改动：`web/.gitignore` +2 行（**必须知道**）

第一次 `git add -- web` 只暂存了 **1916 / 2037** 个文件，**121 个被静默跳过**：
`web/docs/validation/` 下 120 个 `.log` + 1 个 `.csv`（这 121 个都是**已跟踪**的验收记录，不是产物）。

原因：`.gitignore` 的优先级是"**越深越优先**"。根 `.gitignore`（t2 写的）确实放行了
`**/docs/validation/**/*.log|*.csv`，但搬进来的 `web/.gitignore` 里原有的 `*.log` / `*.csv` 规则
**优先级更高**，于是这 121 个文件被重新忽略 —— `git add` 不会报错，只会少加。

处理：在 `web/.gitignore` 末尾追加 2 行放行规则（带注释说明原因）：

```gitignore
!docs/validation/**/*.log
!docs/validation/**/*.csv
```

- 这是本次迁移**唯一**一处对源文件的修改（源仓该文件没有这两行），目的是避免 t7 对账时
  894 个已跟踪记录里的 121 个凭空消失；未跟踪产物仍由根 `.gitignore` 的产物族规则压制。
- 复核：改后 `git add -- web` → **2037 / 2037**，`git ls-files --others --ignored --exclude-standard -- web` = **0**。
- 若以后把该文件恢复成源仓版本，必须重新确认这 121 个文件仍被跟踪（或改用 `git add -f` 明确加一次）。

## 6. 留给后续任务的事（不在本次范围）

1. ~~**§2.4 的路径改写（5 文件 / ≈10 行）**~~ → **t6 已完成**：`tools/dev/serve-etm.py`（docstring + 默认
   `--etm-root`）、`tools/dev/start-etm.ps1`、`tools/selftest/etm-integration-hw.mjs`、`start-etm.bat`
   （5 处路径 + 4 处文案）、`bridge/etm/README.md` 的 `..\web-etm-trace` → `..\etm`；另修了
   `tools/dev/sync-etm-runtime.py` 的 provenance 名称、`docs/etm-integration.md` 的开发运行说明与
   `etm/tools/tests/test_server.py` 的上游目录（`ROOT.parent/'web'`）。
2. ~~**探针驱动去重**~~ → **t6 已完成**：真源 `web/app/**`（7 驱动 + 1 依赖），生成副本
   `etm/web/app/shared/**`（`node tools/dev/sync-shared-drivers.mjs`，= `make gen-shared`），
   sha256 守卫 `tools/selftest/shared-drivers.test.mjs`（已并入 `make test`）+ `--check`（已并入 `make check`）。
   取舍与理由见 **`web/docs/driver-unification.md`**。
3. **ETM 页面快照**：`bridge/etm/backend/**` 是 ETM 的**派生快照**（provenance 指向 ETM `a2922f8`），
   **不要手工编辑**；合并后统一用 `python tools/dev/sync-etm-runtime.py --etm-root ../etm` 重新生成，
   再用 `build-etm-package.py` 出包。
4. **行尾**：本仓沿用源仓的 `core.autocrlf=true`；`tools/fixtures/**`、BMP/GIF/WebM、`*.elf/*.bin` 等
   在根 `.gitattributes` 里标成 `-text`/`binary`，不要用会改字节的工具整体重写它们。
