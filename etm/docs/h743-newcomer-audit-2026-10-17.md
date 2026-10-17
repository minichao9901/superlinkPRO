# 新用户视角审计：桥包 / 反复启动 / 全新 clone（2026-10-17 晚）

审计人：DSH（session-4d688a58）· 目标：从"另一个用户"的角度走一遍，找出会卡住人的地方。

---

## 一、桥包要不要修？→ **要，已经修了**

**发现**：仓库里发布的 `bridge/etm/downloads/etm-bridge-win64.zip`（14:02 构建）里内嵌的
`bridge/etm/backend/web/app/shared/rtt/dap-webusb.js`（84.7 KB）**落后于源码**（86.1 KB）：

- 包里**没有** `resetDevice()` 的"永不抛"修复（`非致命，继续认领` 这段文案不存在）；
- 当时包里还是 `target.js` 传 `skipClearHalt: true` 的旧行为。

**也就是说：即使我们修好了网页，用户下到的包仍然是坏的（探针还是会卡"已连接但无通信"）。**

**修法（已做）**：

```powershell
cd E:\web-serial-rtt-tools
python -X utf8 tools/dev/sync-etm-runtime.py --etm-root E:\web-etm-trace   # 把 ETM 仓源码同步进包快照
python -X utf8 tools/dev/build-etm-package.py                              # 重打包
```

重建结果：`sourceRevision=9806a67`，包内 `dap-webusb.js` 已含修复，`target.js` 已不再传 `skipClearHalt`。

> ⚠️ **发布流程缺口**：`build-etm-package.py` 只打**工具箱里的快照**，不会自动从 `web-etm-trace` 拉最新。
> 改了 ETM 网页就**必须**先 `sync-etm-runtime.py` 再 `build-etm-package.py`，否则发出去的包是旧的。
> 建议把这两条串成一个 `make etm-package` 目标（未做）。

---

## 二、独立包跑完整流程：**走到了采集，但卡在一个确定性缺陷**

**环境**：包解压到 `E:\web-etm-package-test\etm-bridge-kit\`，用自带 `etm-bridge.exe --config etm.config.json --port 8878 --no-browser` 启动；
端口用 **8878（包自带默认）** 而不是另开 8888 —— 因为 **WebUSB 授权是按 origin 记的**，换端口就要重新授权。

**页面路径**：`http://127.0.0.1:8878/` 是**工具箱**，ETM 页在 **`http://127.0.0.1:8878/etm/`**（这点用户很容易猜错）。

**能走通的步骤**（每一步都实测）：

| 步骤 | 结果 |
|---|---|
| 包自带桥启动 | ✅ `/etm/api/status` 返回 `captureProtocol:2, connected:true, decoder:true` |
| 载入 ELF（`trace_hook_entry` 所在固件） | ✅ 日志 `ELF 已载入` |
| 连接探针（复用已有 WebUSB 授权） | ✅ 1.5s 连上 |
| 加硬件断点 | ✅ 日志 `添加断点 trace_hook_entry · 0x08000b50；目标暂停` |
| 核对时钟（HSE=25） | ✅ 页面显示 `CPU 60.000 / HCLK 60.000 / TRACECLK 30.000 MHz` |
| **启动采集** | ❌ **`启动 LA 并采集失败：目标仍暂停，未命中本页断点，请检查其他断点或异常`** |

**反复启动测试**：连续 3 次（每次都重载页面 + 重新走 ELF→连接→断点→时钟），
**3 次全部在同一处失败**，页面日志每次都是：

```
ELF 已载入
添加断点 trace_hook_entry · 0x08000b50；目标暂停
采集步骤：暂停本页断点
启动 LA 并采集失败：目标仍暂停，未命中本页断点
本次 trace 配置与断点已恢复；目标状态已刷新
```

⇒ **不是偶发**，是包路径上的确定性缺陷。可疑点（最可能的两个）：

1. **FPB 重命中**：复位到起点后 CPU 停在入口，而入口上还装着比较器 → 放行立刻再命中 → 表现为"目标仍暂停"。
   这正是本项目文档里记过的坑（"命中断点后必须摘比较器再放行"），网页这条路径可能漏了某一步。
2. **桥与页面的放行分工**：`api('capture')` 之后由桥负责放行，而页面在 `暂停本页断点` 阶段已经把比较器挂起；
   两边对"谁负责 resume、断点何时重装"的理解可能不一致。

⚠️ 注意：**同一套流程在工具箱服务（`serve-etm.py` 起的 8878）上曾经成功过**（生成过
`records/capture-ab4f7bdcac984a758085cee69930b544`：1,098,860 条指令、溢出 0、包问题 0）。
所以差异更可能在**包的打包内容/版本**或**多次启动后的目标状态**，需要下一步对比定位。

---

## 三、全新 clone 审计（模拟其它用户）

```powershell
git clone --depth 1 file:///E:/web-serial-rtt-tools E:\tmp-clone-audit\web-serial-rtt-tools
```

clone 下来 2032 个文件，关键项：

| 项 | 有/无 | 说明 |
|---|---|---|
| `bridge/etm/downloads/etm-bridge-win64.zip` | ✅ | 便携桥包在仓库里（23.8 MB），README 推荐的正是这条路 |
| `bridge/etm/backend/web/...`（内嵌 ETM 页） | ✅ | |
| `tools/dev/serve-etm.py` | ✅ | 但需要 `--etm-root <web-etm-trace>` |
| **`web-etm-trace` 兄弟仓库** | ❌ | **我的 `start-etm.bat` 正依赖它** → 客户 clone 后跑不起来 |
| **任何 H743 固件 / ELF** | ❌ | H743 的固件和 ELF 只在 `web-etm-trace` 仓里 → 客户没法"开箱试一下" |
| `node_modules` | ❌ | `start-web.bat` 用 `node tools/dev/serve-nocache.mjs`（无需依赖，OK） |

**客户痛点清单（按遇到的先后）**

1. **首页不是 ETM**：`/` 是工具箱，ETM 在 `/etm/`；README 只写了"更多功能中的完整内嵌页面"。
2. **`start-etm.bat` 在客户机上会失败**：它 `--etm-root ..\web-etm-trace`，而客户没有兄弟仓库。
   （本机可用；对客户应走包自带 `start-etm-bridge.bat`。）→ 已给 bat 加护栏：检测不到兄弟仓库就明确指路。
3. **HSE 必须手填**（H743 没有默认值）：不填就在采集第一步报"核对实际时钟失败"，错误信息对新手不友好。
4. **WebUSB 授权按 origin/端口**：换个端口就要重新点一次设备选择框。
5. **没有 H743 示例固件/ELF**：客户拿到的是"工具"，但没有"能立刻跑出结果的靶子"。
6. **探针互斥**：浏览器占着探针时，OpenOCD/pyOCD 烧录会 `CMD_INFO failed` —— 双工具用户必撞。
7. **包与源码会不同步**（本次就踩到）：发布前必须 `sync-etm-runtime.py` + `build-etm-package.py`。

---

## 四、下一步（优先级）

1. **定位第二节那个"未命中本页断点"**（包路径确定性失败）：先对比包内 `runtime/backend` 与工具箱服务下的同名文件是否完全一致，再看 `capture` 时桥侧日志（`--verbose`）。
2. 把 `sync-etm-runtime.py` + `build-etm-package.py` 串成一个发布目标，避免再发包里的旧代码。
3. 给 H743 profile 一个 HSE 默认值（25 MHz），或至少在时钟页把"必须填 HSE"做成显式告警。
4. README 里把"ETM 在 `/etm/`"、"必须填 HSE"、"浏览器占用探针时不能烧录"三条写进快速上手。
5. 在仓库里放一个 H743 的示例固件 + ELF（或从 `web-etm-trace` 同步一份），让客户能立刻看到效果。

## 五、修复与复测结果（2026-10-17 19:10 更新）

### 5.1 那个"未命中本页断点"已定因并修复（提交 `66b758e`）

补诊断后拿到的确切数据（`target.js` 抛错前打印 PC/DFSR/DHCSR/DEMCR/同地址断点状态）：

```
PC 0x08000B50   ← 正是业务起点 trace_hook_entry
DFSR 0x00000003 ← bit0 断点命中 + bit1 BKPT
DHCSR 0x00030003 ← S_HALT=1
同地址断点：0x08000B50(未租用)
```

**根因**：`target.reset({address})` 为了"复位后停在业务起点"，会临时在入口装一个 FPB 比较器，
并在 cleanup 里 `clearEntryBreak()` 摘掉；但**这颗探针的 PPB 写偶发丢失**（本项目文档记过），
比较器残留 → 放行瞬间又命中同一地址 → `run()` 判"仍暂停"，而该地址的页面断点已被 `suspend()`
（未租用）→ 落到"未命中本页断点"分支，**采集永远起不来**（连续 3 次可复现）。

**修法**：`run()` 增加防御性自愈 `clearStaleComparator(pc)` —— 按 `FP_CTRL` 现场回读比较器
个数/版本，找出与该地址匹配（忽略 match 位与使能位）的残留比较器，写 0 并**回读确认**（失败补一次），
然后重新放行一次；仍跑不起来才抛错。抛错信息里保留全部诊断字段。

### 5.2 复测：独立包反复启动 **3/3 稳定**

用**重新构建的包**（`sourceRevision=66b758e`，解压到 `E:\web-etm-package-test`，包自带 bridge 跑在 8878）：

| 次 | 时间 | 指令 | 溢出/包问题 | gaps |
|---:|---|---:|---:|---:|
| 1 | 19:01:29 | 945,686 | **0 / 0** | 1 |
| 2 | 19:04:17 | 945,677 | **0 / 0** | 1 |
| 3 | 19:07:25 | 945,677 | **0 / 0** | 1 |

每轮都完整走了 **载 ELF → 连探针 → 加断点选起点 → 核时钟（HSE=25）→ 联动采集 → 解码 → 记录已保存**。
指令数几乎完全一致（945,677 出现两次）= 采集链路是**确定性**的，不是"碰运气跑通"。

### 5.3 磁盘教训（重要，会重复踩）

清理 trace 数据时**不能整目录删 `records/`**：`baselines/spi-70-35-v1/manifest.json` 的
`previewRecord` 指向 `records/capture-926a4f2b…`，`sync-etm-runtime.py` 要拿它的 `decoded.json`
做内置 Demo；删了之后 `sync` 直接 `FileNotFoundError`，`build-etm-package.py` 跟着报
「ETM 快照与来源清单不一致」。

- 本次已从工具箱的验证副本
  `docs/validation/20261016-web-review-062721/etm-records/demo-spi-70-35-v1/`（其
  `spi-validation.json` 里 record 正是该 ID，逐字节同源）**恢复**到 `records/capture-926a4f2b…/`。
- 以后清理：`records/` 里**只删时间戳早于最后一个 baseline 的**，或先跑一次
  `sync-etm-runtime.py` 确认能过再删。`captures/` 可以放心删（不在任何清单里）。

### 5.4 桥包发布流程（补一条）

`build-etm-package.py` 打的是**工具箱里的快照** `bridge/etm/backend/**`，不会自动从
`web-etm-trace` 取最新代码。**改了 ETM 网页必须两步**：

```powershell
python tools/dev/sync-etm-runtime.py --etm-root E:\web-etm-trace
python tools/dev/build-etm-package.py
```

（本次就是因为漏了第一步，第一版重建出来的包内 `dap-webusb.js` 还是旧代码。）

