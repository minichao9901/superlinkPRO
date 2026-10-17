# etm/web/app/shared —— **生成副本，别手改**

这里放的 **8 个** `.js` 是 `web/app/**` 里探针驱动的**逐字节副本**（ETM 页通过 `./shared/...` 引用它们）：
7 个驱动 + 1 个驱动依赖（`core/operation-budget.js`，toolbox 版 `dap-webusb.js` 的 OperationBudget）。

| 副本路径 | 真源（唯一手写） |
| --- | --- |
| `rtt/dap-webusb.js` | `web/app/rtt/dap-webusb.js` |
| `core/usb-device.js` | `web/app/core/usb-device.js` |
| `core/probe-bus.js` | `web/app/core/probe-bus.js` |
| `core/pace.js` | `web/app/core/pace.js` |
| `core/bin.js` | `web/app/core/bin.js` |
| `core/operation-budget.js` | `web/app/core/operation-budget.js`（dap-webusb 的依赖） |
| `dbg/bp.js` | `web/app/dbg/bp.js` |
| `dbg/source.js` | `web/app/dbg/source.js` |

重新生成 / 校验（在 `web/` 目录下）：

```powershell
node tools/dev/sync-shared-drivers.mjs           # 复制真源 → 本目录
node tools/dev/sync-shared-drivers.mjs --check   # 只校验（漂移则退出码 1）
node tools/selftest/shared-drivers.test.mjs      # 守卫（已并入 make test / make check）
```

**为什么保留副本，而不是让 ETM 页跨子树 import**（依据 `docs/merge-map.md` §4.1.3）：

1. ETM 页要能**独立启动**：`etm/start-web.ps1` 只服务 `etm/` 这一棵树；
2. 它还要进**便携桥包**：`web/bridge/etm/backend/**` 是独立快照，包内不该出现 `../../web/app`；
3. `web/tools/dev/serve-etm.py` 只改写 HTML 的 `href`/`src`，**不改写** JS 的 import 说明符。

**一次改动的正确顺序**：改 `web/app/**` 真源 → 跑 `node tools/dev/sync-shared-drivers.mjs` →
跑 `node tools/selftest/shared-drivers.test.mjs`（或 `make check`）。
合并了哪些差异、为什么这么取舍：见 `web/docs/driver-unification.md`。
