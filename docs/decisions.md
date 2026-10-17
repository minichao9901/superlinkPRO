# 三仓合并：已批准的决策（Captain 裁定，2026-10-17）

来源：`docs/merge-map.md` §8 的 6 个 Decision（mapper 在 t1 提出）。
以下为**最终决定**，下游任务（t2–t7）照此执行，不再回头询问。

---

## D-1 akaLinkPro 原板板级 → **不迁**

删除 akaLinkPro 专有板级（mapper 实测 17 个文件 + 3 个原板 `.bat`）。理由：本仓只服务
**HPM5301EVKLite**，用户明确要求"去掉原来 akaLinkPRO 的部分"。在 `firmware/README-migrated.md`
里列清删了哪些文件，便于回溯。

## D-2 ETM 的 firmware：**只搬 2 个靶子工程（136 文件 / 12.5 MB）**

ETM 仓 `firmware/` 共 ~678 MB / ~385 文件，其余 249 个文件与 toolbox 的 HAL/CMSIS **逐字节重复**，
不搬。**必须包含 H743 靶子**（本会话真机验证用的 `stm32h743_cdc_trace`），以及 H7B0 那个靶子。
在 `etm/README-migrated.md` 写明"firmware 只保留这两个靶子工程，其余按 D-2 丢弃"。

## D-3 `.opencode/skills` → **不迁**

属于 agent 工具链配置，不是产品内容。

## D-4 `docs/` → **不收敛**

保持 web / etm / firmware 各自的 docs 子树，不强行合并到根 `docs/`（收敛需改 web 侧 25 处引用）。
根 `docs/` 只放**跨仓**文档：`merge-map.md`、本文件、`merge-verification.md`、`driver-unification.md`。

## D-5 `script_test` 的 115 个重复靶子 → **暂不瘦身**

按 mapper 的结论整体保留（akaLinkPro 的 Makefile 里有 20 处引用、`APP_DIR=firmware\application_5301`
是相对根的，瘦身容易破坏零改动构建）。留作后续独立优化项。

## D-6 git 历史 → **文件搬迁 + 记录源 commit，不做 subtree**

原计划默认是 subtree 保留历史，但 t1 证明：**裁剪式迁移**（D-1/D-2 要删文件）与 subtree
"整树跟踪"冲突——subtree 会把 678 MB firmware 和原板板级一并带进来，再删就是"迁进来又删掉"，
历史反而更乱。

因此改为：**逐文件复制（mapper 已验证 680/680 的 `Copy-Item` 方法）**，并在每个
`README-migrated.md` 里记录：

```
来源仓库：<绝对路径>
来源分支：<branch>
来源 commit：<40 位 SHA>
迁移时间：<ISO 时间>
迁移方式：逐文件复制（按 docs/merge-map.md 的取舍清单），不含 git subtree
```

**代价说明（诚实记录）**：合并仓里拿不到原仓的逐文件 `git blame`；需要考古时回原仓（用户可能删除，
所以 commit SHA + 原仓路径是最后凭据，务必写全）。这一点已在 `docs/merge-verification.md` 里作为已知限制列出。

---

## 两条必须传给下游的踩坑（t1 实测）

1. **`core.autocrlf=true`**：三仓都是。跨仓判断"内容相同"必须用**工作区文件的 SHA-256**，
   直接比 `git blob`（LF）会误判为不同。
2. **`git ls-files` 不加 `-c core.quotepath=false`** 会把中文文件名转义，照它生成清单会
   **静默漏 2 个文件**。另外本机 `bsdtar 3.8.9` 只搬了 678/680 —— 最终选定 **`Copy-Item` 逐文件**，
   实测 680/680。清单生成与搬运用同一份（加 `-c core.quotepath=false` 的）列表，搬完**数量必须相等**。

---

## 对账口径（t7 用）

t1 已给出精确期望：**3188 文件 / 160,855,216 B（153 MB）**，并附逐文件 sha256。
t7 验收时用同一份清单比对；数字对不上就是没搬全（不要用"大致差不多"结案）。

---

## 唯一的"真源码改动"清单（t3 必须带上，subtree/copy 都不会自动带）

toolbox 512 行未提交改动 = **8 个已修改文件（全是 ETM 相关真源码）+ 4213 未跟踪**，其中：

- 必须搬：那 8 个已修改文件 + 1 个真文档 `docs/h743-web-review-2026-10-17.md`
- 必须**不搬**：`.agent-teams/`（本团队状态）、`docs/validation/`（4206 个未跟踪验证产物）

详见 `docs/merge-map.md` §5。

---

## t2 实测偏差（2026-10-17，webapp 发现并已按正确方式实现）

**`docs/validation/` 不能整目录 ignore。** mapper 的清单把它当"未跟踪产物"，但实测那里有
**1086 个已跟踪文件**（toolbox 894 / akaLinkPro 139 / ETM 53）——目录一旦被 `.gitignore` 排除，
`git add -A` 会**静默丢**这 1086 个文件，而 t7 的 §7 对账会因此变红。

当前实现（**接受，不改**）：
- 默认忽略 `docs/validation/` 整层，**放行其中的已跟踪目录与文件类型**，再对未跟踪产物族压制；
- 实测压掉 4206 个未跟踪产物里的 **3897 个（92.7% 文件 / 98.3% 字节）**；
- 剩 **309 个 / 43 MB** 是零散截图/文本/补丁，与已跟踪证据同类型、无法按类型安全忽略，
  且**不在迁移清单内** —— 允许留在磁盘上但**不要入库**（t3/t4 搬完用 `git status` 确认无残留未跟踪）。

**因此**：t3 / t4 **不需要**对 `docs/validation` 用 `git add -f`。若后续有人改成"整目录 ignore"，
必须同步把 t3/t4 改成对该目录下的已跟踪文件 `git add -f`，否则必丢文件。

另：`.agent-teams/`（团队状态）整目录忽略，**绝不入库** —— 这条不变。

**git 身份**：初始提交原为 `webapp <webapp@superlinkpro.local>`；Captain 已设仓库本地
`user.name=minichao` 并 `--amend --reset-author` 修正（全局 `user.name` 里带字面引号 `'minichao'`，
是个历史遗留 bug，本仓已用本地配置覆盖；**其它仓如要提交，需各自设本地身份**）。

---

## t3 实测偏差（2026-10-17，webapp 发现）—— `.gitignore` 越深越优先

**现象**：`git add -- web` 第一次只暂存 **1916 / 2037**。原因是 toolbox 自带 `web/.gitignore`，
它的规则**优先级高于根 `.gitignore`**（git 规则"越深的文件越优先"，根里写的放行赢不了子目录里的忽略）。
被静默吞掉的是 `web/docs/validation/` 下的 **121 个已跟踪 `.log` / `.csv`**。

**危害等级：高** —— git **不报错**，只是少加；只有 t7 的对账（2037 期望 vs 实际）才会暴露，
而且如果 t7 用"就是这些文件"的自我口径，就会**把丢文件当成正常**。

**修法（已做）**：在 `web/.gitignore` **末尾**补两条否定规则并写进 `web/README-migrated.md §5.1`：

```
!docs/validation/**/*.log
!docs/validation/**/*.csv
```

这是本次迁移**唯一对源文件的改动**（+2 行 + 注释）。复查：staged = 2037/2037、
`web` 下已跟踪 validation 记录 894（120 log + 1 csv）、`ignored-but-present under web/` = 0。

**对下游的硬要求**：
- **t6**：`web/.gitignore` 的这两行**必须保留**，合并子目录时不要覆盖；
- **t7**：对 web 的期望是 **2037 个文件**；对账若用 `git add -A` 式结果做基线，必须先去重"被忽略但存在"的文件
  （用 `git ls-files --others --ignored --exclude-standard -- web` 自检，应为 0）；
- **t5（ETM）/ t4（akaLinkPro）不会踩这个坑**（它们的 `.gitignore` 不在迁移清单里），但**搬完仍要做同一自检**。

**通用纪律（写进最终 README）**：凡整树平移带 `.gitignore` 的子目录，`git add` 之后必须
**用 `git ls-files` 的数量与源清单对账**，不能只看 `git status` 干净与否。
