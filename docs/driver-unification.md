# 探针驱动去重（索引）

> 正文在 **[`web/docs/driver-unification.md`](../web/docs/driver-unification.md)** —— 驱动真源在 `web/`，
> 文档跟着驱动走（D-4：根 `docs/` 只留跨仓索引）。

一句话：**7 个探针驱动 + 1 个依赖**的唯一手写真源在 `web/app/**`；`etm/web/app/shared/**` 是由
`web/tools/dev/sync-shared-drivers.mjs` 生成的**逐字节副本**（禁止手改），由
`web/tools/selftest/shared-drivers.test.mjs` 与 `sync-shared-drivers.mjs --check` 用 sha256 守卫；
`web/bridge/etm/backend/**` 是 ETM 便携包的**运行快照**（本次未重生成，原因与重生成命令见正文 §7）。

合并保留/移植的要点：`OperationBudget`、`DAP_SESSION_LOST`、5 字节 `WriteABORT`（toolbox 独有）；
`resetDevice()` 永不抛、禁短超时 drain、复位不可用提示（ETM 独有）；**不抄** ETM 的 4 字节 `abort()` 缺陷、
不用其锁外 FAULT 自愈、不用其宽松 CPUID 判定。详见正文 §2。
