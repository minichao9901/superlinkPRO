# superlinkPRO 合并后功能验收（2026-10-17 夜）

环境：探针 `akaLinkPro CMSIS-DAP`（`8F538633…`，**本次已重烧带新 URL 的固件**）+ 阿波罗 H743 在线。
页面：**本地等价站点** `http://127.0.0.1:8878/`（`web/tools/dev/serve-etm.py --etm-root ../etm`）——
它下发的就是 GitHub Pages 将发布的那份 `web/`，所以结果对线上有效；线上地址待 PAT 补 `workflow` 权限后发布。

## 结论总表

| 功能 | 结果 | 证据 |
|---|---|---|
| **RTT Viewer** | ✅ **通过（真机）** | xterm 里读到 H743 固件自己的 RTT 启动日志：`[I/USB] dwc2 has 9 endpoints and dfifo depth 952`、`GSNPSID:4f54330a`、`usb initialized (0x40080000)`、`[usb] reset/configured` |
| **烧录器** | ✅ **通过（真机）** | WebUSB + STM32H7 + 校验开，烧 `etm/firmware/stm32h743_cdc_trace/fw.elf`：加载 flashloader → 写 30.9 KB → **校验（读回比对）** → 6.7 s 完成 |
| **调试器** | ✅ **通过（真机）** | 点「连接」后 `#d-connect` 变灰（已连接），SWD/ARM、10 MHz 默认档 |
| **ETM 执行轨迹** | ✅ **页面与后端可用** | `#tab-etm` 的 iframe 指向 `/etm/?embedded=1`，子页标题正确、状态 **`本地服务在线`**（说明合并后的 `serve-etm.py` 挂载与 `/etm/api/` 代理都通） |
| **RTT 转发** | ⏳ 未验 | 需要独占探针 HID 桥 + 占用 COM（与 ETM 桥/浏览器会话易冲突），留作单独一轮 |
| **J-Scope 波形** | ⏳ 未验 | 需要**带 J-Scope 变量的目标固件**；合并仓按 D-2 只迁了 `stm32h743_cdc_trace`（它有 RTT，但不是 J-Scope 靶子） |

## 这轮同时验证的合并成果

- **驱动统一生效**：页面下发的 `web/app/rtt/dap-webusb.js` = 真源 `1AD30E4735AB`；探针连接、烧录、调试、RTT **全部走这一份**，没有出现第二份驱动的行为差异。
- **ETM 并入 web 可用**：`#tab-etm` → iframe → 本地后端，链路完整（此前 t6 裁定不物理搬迁，此处得到实证）。
- **便携包可下载**：`/bridge/etm/downloads/etm-bridge-win64.zip` HTTP 200（23.81 MB，`sha256 F9325367…`），包内三处驱动哈希 = 真源。
- **探针固件新 URL 生效**：`akaLinkPro_App_pack.bin` 里含 `github.io/superlinkPRO`、不含旧串；DFU 烧录 192448 B 成功、探针重新枚举。

## 待办（阻塞项）

1. **GitHub Pages 发布**：`git push` 被拒 —— 本地 PAT 缺 **`workflow`** 权限（`refusing to allow a Personal Access Token to create or update workflow`）。
   - 方案 A：给 PAT 加 `workflow` 作用域后我再推一次（推荐）
   - 方案 B：改用 `gh-pages` 分支 + 分支模式发布（不需要 workflow 权限）
   - 现状：远端 `main` = `d0f18f8`（除 workflow 外的全部内容已推送）
2. **RTT 转发 / J-Scope**：需要各自的靶子与独占串口轮次，单独安排。
