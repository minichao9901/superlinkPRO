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
