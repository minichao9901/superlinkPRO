# ETM 线上页 + 本地桥（GitHub Pages 静态页 ↔ 127.0.0.1 桥，跨源）

> 用户要求：**页面用 GitHub 的页面，纯 web、零依赖**（唯一例外是 ETM 要连本地桥）。
> 本文写清拓扑、桥地址怎么配、CORS 白名单、发布副本怎么生成，以及排障步骤。

---

## 0. 一句话

**页面（含 ETM 子页）全部静态发布在 GitHub Pages；只有 ETM 的数据面（`/etm/api/**`）跨源访问本机桥。**
ETM 子页必须与父页**同源**（子页要把 bridge 反向挂到 `window.parent.__tools.etm`），所以 ETM 页在
Pages 上用 `web/etm/**`（从 `etm/web/**` **生成**的静态副本）；跨源只发生在 `fetch` 上（由 CORS 放行）。

---

## 1. 拓扑

```
[GitHub Pages]  https://minichao9901.github.io/superlinkPRO/          ← 站点根 = web/
  index.html + app/**  （主工具；线上走 app/site/main.js 打包产物）
    └─ #tab-etm（web/app/etm/view.js）
         ├─ 探测：GET http://127.0.0.1:8899/etm/integration.json     ← 跨源（CORS）
         └─ <iframe src="./etm/index.html?embedded=1&api=http://127.0.0.1:8899/etm/api/">
              ↑ 同源副本（web/etm/**，生成自 etm/web/**）—— 这样 window.parent.__tools.etm 才读得到
                 └─ 数据面：fetch http://127.0.0.1:8899/etm/api/**（跨源，CORS）

[本机]  python web/tools/dev/serve-etm.py --etm-root etm --serial-root web --port 8899
         ├─ /etm/api/**            → etm/server.py（Flask；CORS 白名单）
         ├─ /etm/integration.json  → {"protocol":1,"backend":"kingst"}
         ├─ /etm/                  → etm/web（本地同源时可不用副本，页面注入 data-api-base=/etm/api/）
         └─ /                      → web/（本地同源工作台；此时 ETM 标签根本不跨源）
```

两条拓扑都要成立（实测都通过，见 §7）：

| 拓扑 | 页面来源 | ETM 子页 | 数据面 | 判定 |
| --- | --- | --- | --- | --- |
| **同源（本地工作台）** | `http://127.0.0.1:8899/` | `/etm/?embedded=1`（serve-etm 注入 `data-api-base=/etm/api/`） | 同源 `/etm/api/` | 同源 `integration.json` 命中即可 |
| **跨源（Pages / 任意静态页）** | 静态托管（站点根 `web/`） | `./etm/index.html?embedded=1&api=<桥>/etm/api/` | 跨源 `http://127.0.0.1:<port>/etm/api/` | 同源不中 → 探测候选端口命中 |

> 只想本地看：`cd web && make serve-etm`（8878）或直接
> `python tools/dev/serve-etm.py --etm-root ../etm --serial-root . --port 8899`。
> 想模拟线上：`python -m http.server 8901 --bind 127.0.0.1 --directory web` 然后开
> `http://127.0.0.1:8901/`（这就是「页面在 A 源、桥在 B 源」的等价拓扑）。

---

## 2. 桥地址可配置（子页 API base 的三级来源）

子页 `etm/web/app/view.js` 的 `resolveApiBase()`（优先级从高到低）：

| 顺序 | 来源 | 谁写它 |
| --- | --- | --- |
| ① | `document.body.dataset.apiBase`（`data-api-base` 属性） | 本地 `serve-etm.py` 注入 `/etm/api/`；**线上副本 bootstrap** 写查询参数/记忆值/默认值 |
| ② | `globalThis.__etmApiBase` | 静态副本 bootstrap（同 ① 的备用通道） |
| ③ | `?api=` 查询参数 | 主页面探测到桥后传给 iframe（**跨端口/跨源时走这条**） |
| ④ | `localStorage['etm.apiBase']` | 上次成功连接记下的桥；刷新后仍然记得（可在 DevTools 里删） |
| ⑤ | 默认值 | `*.github.io` → `http://127.0.0.1:8899/etm/api/`；其它 → `/api/`（独立 ETM 服务/同源） |

主页面（`#tab-etm`）里有 **「桥地址」输入框 + 「重新检测服务」按钮**：

- 留空 = 自动探测（同源 → 127.0.0.1 的 8878/8890/8899）；
- 填 `127.0.0.1:9001` 或 `http://host:port` = **只探它**（换端口自救，不再扫默认端口）；
- 成功后会记住（`store('etm.bridge')`）；「打开本地 ETM 工作台」链接也跟随该地址。

---

## 3. integration.json 探测策略

`web/app/etm/view.js` 的 `probeBridge()`：

1. **同源优先**：先 `GET ./etm/integration.json`。命中 = 本地工作台（**不骚扰其它端口，控制台零噪声**）。
2. 同源不中：
   - 用户填了桥地址 → **只探它**；
   - 否则**并发**探测 `127.0.0.1:8878 / 8890 / 8899`（每个 **3 s** 超时，`Promise.any` 命中即用）。
3. 全部不中 → 面板给出可操作提示（含候选列表 + 「启动桥 / 查说明 / 下载桥包」链接），
   不让 iframe 白屏。

跨源命中后还会 **先验证两件事**再挂 iframe（失败就看到明确提示，而不是子页报错）：

- `./etm/index.html` 是否存在（线上部署漏了 `web/etm/` 时提示"请确认部署的是合并后的 web/"）；
- `<桥>/etm/api/status` 是否可访问（CORS/预检有问题时提前暴露）。

> 说明：探测候选端口时，**未监听的端口在 DevTools 里必然出现 `net::ERR_CONNECTION_REFUSED` /
> `404 integration.json`** —— 这是探测的固有噪声，不是应用报错。跨源验收里「0 报错」指
> **应用级 0 报错**（无未捕获异常 / 无 `console.error` / 子页 0 错误）；噪声只有两条已分类的 404：
> `…/etm/integration.json`（同源探测）与 `…/etm-launcher.json`（工程生成页**既有**的桥检测，与本功能无关）。

---

## 4. CORS（`etm/server.py` 与 `web/bridge/etm/backend/server.py` 同款）

```python
CORS_PAGE_HOSTS = {'minichao9901.github.io'}          # 线上页面 origin（https）
CORS_LOCAL_RE   = r'^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$'   # 本机回环任意端口
ETM_CORS_ORIGINS = 'https://example.org,http://192.168.1.10:8080'         # 可选：追加白名单（逗号分隔）
```

- 只对 **`/api/**` 与 `**/integration.json`** 加头（静态资源不需要）；
- 允许方法 `GET,POST,OPTIONS`、允许头 `Content-Type`、`Access-Control-Max-Age: 600`、`Vary: Origin`；
- **`OPTIONS` 预检返回 204**（`cors_preflight`）；
- 来源匹配才回 `Access-Control-Allow-Origin: <该来源>` —— **不用 `*`**；不在白名单就不发头（浏览器拦掉）；
- **同源不受影响**：`local_only()` 只把"非 GET/OPTIONS 且 Origin 不在白名单"继续拒掉；
  同源页面（`http://127.0.0.1:<port>`）本来就在本机白名单里；无 `Origin` 的请求照旧放行（curl 可用）。

---

## 5. 放宽 `attach()` 的来源断言（仍拒绝非白名单）

`web/app/etm/view.js`：

- 父页 origin ∈ { 本站 origin, `https://minichao9901.github.io` }；
- 子页 origin ∈ { 本站 origin, `http://127.0.0.1:*`, `http://localhost:*`, `[::1]` }；
- 不在白名单 → 抛"ETM 页面来源不在白名单（父 … / 子 …）"。

**保留原保护**：`if (this.bridge && this.probe)` —— 旧子页还连着探针时，重新载入会被拒绝，
必须先释放（`retry()` 会先 `requestStop` 再 `release`）。

---

## 6. 发布副本：`web/etm/**`（生成，不是搬家）

`etm/web/**` 是**唯一源码**；`web/etm/**` 是给 Pages 用的**静态副本**：

```powershell
cd web
node tools/dev/sync-etm-page.mjs            # 生成（= make gen-etm-page）
node tools/dev/sync-etm-page.mjs --check    # 校验（make check 里有）
node tools/selftest/etm-page-copy.test.mjs  # 守卫 42 项（make test 里有）
```

只对 `index.html` 做三处改写（其余 26 个文件逐字节相同）：

1. 绝对路径 → 相对：`/style.css` → `./style.css`、`/app/view.js` → `./app/view.js`（否则挂在
   `/superlinkPRO/etm/` 下就 404）；
2. 插入 bootstrap：解析 API base（§2）、`?embedded=1` 打内嵌标记、注入与本地工作台一致的内嵌样式；
3. **无桥降级**：非内嵌时 3 s 内 ping `<api>/status`，失败就在页面顶部显示
   「需要本地 ETM 服务（桥）」+「下载完整 ETM 桥包 / 启动与排障」链接（`window.onerror` 也会走到这里），**不白屏**。

**为什么是生成副本而不是把 `etm/web` 物理搬进 `web/`**（D-7 的三条真实依赖）：

1. `etm/start-web.ps1` 要能**独立启动** ETM 页（只服务 `etm/` 树）；
2. `web/bridge/etm/backend/**` 是**便携包快照**，必须自洽、不能引用跨子树路径；
3. `serve-etm.py` 只改写 HTML 的 `href/src`，**不改写 JS import** —— 物理搬迁会让 import 解析失败。

副本里的 8 个共享探针驱动（`app/shared/**`）也随副本生成；它们与 `web/app/**` 真源的一致性由
`etm-page-copy.test.mjs` 第 6 节（8/8 逐字节）与 `shared-drivers.test.mjs` 双重看守。

---

## 7. 验收记录（2026-10-17，本机实测；不接硬件）

**静态检查**

| 检查 | 结果 |
| --- | --- |
| `node tools/dev/sync-etm-page.mjs --check` | **27 个文件一致**（index.html 按副本规则改写） |
| `node tools/selftest/etm-page-copy.test.mjs` | **42 通过 / 0 失败** |
| `node tools/selftest/shared-drivers.test.mjs` | 24 通过 / 0 失败 |
| `make check`（check-syntax 411 模块 / check-liquid 158 篇 / build-web 170 modules / shared + etm-page） | 全绿 |
| `python -m py_compile`（etm/server.py、快照 server.py、serve-etm.py） | 全过 |

**CORS（curl/Node fetch 实测，桥在 8899）**

| 请求 | 结果 |
| --- | --- |
| `GET /etm/integration.json`，`Origin: https://minichao9901.github.io` | **200** + `ACAO: https://minichao9901.github.io` + `Vary: Origin` |
| `GET /etm/api/status`，同上 Origin | **200** + 同上 CORS 头 |
| `OPTIONS /etm/api/status`（预检，`Access-Control-Request-Method: POST`） | **204** + `Allow-Methods: GET,POST,OPTIONS` + `Allow-Headers: Content-Type` |
| `GET /etm/api/status`，`Origin: http://127.0.0.1:8901` | **200** + `ACAO: http://127.0.0.1:8901` |
| `GET /etm/api/status`，无 Origin | 200，**无** CORS 头（同源/命令行不受影响） |
| `GET /etm/api/status`，`Origin: https://evil.example` | 200，**无** CORS 头（浏览器会拦）；`OPTIONS` 同 |

**浏览器端（CDP，Chrome 155；`web/` 静态服务于 **8901**，桥 **8899**，两个不同端口 = 跨源）**

| 项 | 结果 |
| --- | --- |
| 跨源：ETM 就绪（`window.__tools.etm.summary().ready`） | ✅ |
| 跨源：iframe = `./etm/index.html?embedded=1&api=http%3A%2F%2F127.0.0.1%3A8899%2Fetm%2Fapi%2F` | ✅（同源副本） |
| 跨源：子页 attach 成功（`__etmBridge`）、`body.dataset.apiBase = http://127.0.0.1:8899/etm/api/` | ✅ |
| 跨源：子页拿到记录列表 → `#service-state = 本地服务在线`（`GET /etm/api/records` 200） | ✅ |
| 跨源：`integration.json` / `api/status` / `api/records` 均 **200** 且 `ACAO: http://127.0.0.1:8901` | ✅ |
| 跨源：应用级报错 **0**（未捕获异常/console.error/子页错误）；网络噪声仅 2 条预期 404 | ✅ |
| 同源：iframe 仍为 `/etm/?embedded=1`，子页 attach 成功、服务在线 | ✅（不回归） |
| 同源：应用级报错 0、**网络噪声 0**（同源优先探测，不再扫端口） | ✅ |
| CDP 验收合计 | **13 通过 / 0 失败** |

> 「完成一次采集」需要 LA + 目标板 + KingstVIS —— 按任务边界（不接硬件）本次只验证到
> 「桥连通 + 记录列表可用 + 子页与主工具同一个探针所有者」；真机采集由 Captain 另排轮次。

---

## 8. 排障（按顺序）

1. **桥没启动**：页面提示"没有找到本地 ETM 服务"。启动：
   `python tools/dev/serve-etm.py --etm-root ../etm --serial-root . --port 8899`（在 `web/` 下）。
2. **换端口**：在「桥地址」填 `127.0.0.1:<port>` → 「重新检测」。（留空才会扫 8878/8890/8899。）
3. **CORS 被挡**（DevTools 有 `blocked by CORS policy`）：
   - 确认桥是**新代码**（旧版桥/旧便携包不发 CORS 头）：`curl -D - -o NUL http://127.0.0.1:8899/etm/integration.json -H "Origin: https://minichao9901.github.io"` 看有没有 `access-control-allow-origin`；
   - 自定义域名（如自己的 Pages 域名）→ 启动桥时加 `set ETM_CORS_ORIGINS=https://your.domain`（或写进服务环境）。
4. **副本没发布**：提示"本页没有 ETM 页面副本（./etm/index.html 404）" → 确认 Pages 部署的是合并后的
   `web/`（含 `web/etm/`）；本地静态演示时 `--directory web` 要对准 `web/`。
5. **白屏/页面报错**：静态副本顶部会有「需要本地 ETM 服务（桥）」条；内嵌页错误会写进
   `#job-state` 并弹提示（`window.onerror` 兜底）。
6. **端口被旧桥占用**：候选里若有个**旧版** `serve-etm.py`（例如合并前仓库起的 8878），它的
   `integration.json` 没有 CORS 头 → 探测会**正确拒绝**它，继续找 8899；排障时用
   `Get-NetTCPConnection -LocalPort <port>` + 命令行核对是哪个仓库起的。
7. **缓存**：静态副本/页面脚本都带版本（`index.html` 的 `?v=`）；服务端 `Cache-Control: no-store`
   （serve-etm 与静态副本 bootstrap 的探测都带 `cache: 'no-store'`）。

---

## 9. 已知限制

- **便携包 zip 需要重打包才带 CORS**：`web/bridge/etm/downloads/etm-bridge-win64.zip` 是构建产物
  （清单在 `manifest.json`）。本次已重生成快照（`sync-etm-runtime.py`，59 文件）并重打整包，
  `manifest.json` 的 `sourceRevision` 与 `sha256` 一并更新；若以后只改了 `etm/**` 又忘了重打包，
  下载页给出的包仍是旧行为 —— 重打包命令：
  ```powershell
  cd web
  python tools/dev/sync-etm-runtime.py --etm-root ../etm   # 需 etm/build-native 与 etm/records
  python tools/dev/build-etm-package.py                    # 需 PyInstaller + msys64 工具链
  ```
- 采集本身仍需要 LA（KingstVIS Socket）与目标板；本功能只解决「页面在哪、桥在哪、怎么跨源」。
- 旧的 `etm-launcher.json` 探测（工程生成页）在静态托管下必然 404，与本功能无关。
