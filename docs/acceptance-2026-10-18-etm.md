# ETM 执行轨迹验收记录（2026-10-18）

环境：akaLinkPro 探针（CMSIS-DAP, `8F538633…`）· KingstVIS MIPI16（**500 MSa/s 上限、5M 点深度**）
· 网页 ETM 页（`tools/dev/serve-etm.py`，H743 走 `127.0.0.1:8878`）
· 目标与频率：**STM32H743** CPU 60 / TRACECLK 30 MHz；**STM32F103ZE** CPU 48 / TRACECLK 24 MHz

## 1. 型号状态

| 型号 | ETM | 选项能力 | 状态 |
|---|---|---|---|
| STM32F103ZE | ETMv3.4（4 位并行 PE2–PE6） | 分支广播 | **已适配**（2026-10-18 验收） |
| STM32H743 | ETMv4（4 位并行 → TPIU） | 分支广播 / 返回栈 / **周期计数** | **已适配**（2026-10-18 验收，本轮由"实验适配"升级） |
| STM32H7B0 | ETMv4 | 三项 | 已适配（更早） |
| H742 / H750 / H753 | 同 DEV_ID `0x450` | 同 H743 | **未单独验收**（同一份固件配置，未实板验证） |

判据来源：`etm/web/app/clock.js` 的 `traceValidated`；页面「目标与识别」卡片的状态文字。

## 2. F103ZE（ETMv3.4）全流程

网页烧录 `fw-ze.elf`（19.7 KB / 4.2 s）→ `ELF 校验通过：20168 字节目标 Flash 一致`
→ 断点 `trace_hook_entry · 0x08000bcc` → 联动采集 → 解码 → 记录保存（24 s）

| 指标 | 实测 |
|---|---|
| 协议 / 寄存器 | `protocol=ETMv3` · `ctrl=0x810` · `data=[1,2,3,4]` |
| 采样 | 500 MSa/s · 10 ms |
| 包 / 区间 / 指令 | 35,241 / 172,596 / **172,596** |
| 已执行 / 源码映射 | 155,349（90.0%）/ 172,596（100%） |
| 异常 / gaps / **包问题** | 12 / 1 / **0** · `decodeStatus=done` |
| 热点 | `cdc_poll` 77,488 · `main` 77,483 · `USBD_IRQHandler` 276 |

记录：`etm/records/capture-2d6da5829a8c4811b7c5ed2ca9e08cc3`

## 3. H743 周期计数（ETMv4 独有）

CC-only 采集（`TRCCONFIGR=0x11`：bit0 RES1 + **bit4 CC**），与"三项全开"对比见 §5。

| 指标 | 实测 |
|---|---|
| 周期元素（`OCSD_GEN_TRC_ELEM_CYCLE_COUNT`, type 12） | **2,126** 个（单区间最大 401 周期） |
| 总周期 | **569,211** |
| CPI | **3.12** |
| 覆盖时长（coreHz 60 MHz） | **9,486.9 µs** |
| 行为 | 210,706 路径 · 17,601 包 |

函数表（页面「函数」视图，**按自身周期降序**）：

| 函数 | 自身周期 | 占比 | 指令 | CPI | 实际调用 |
|---|---|---|---|---|---|
| `main` | 498,043 | 87.5% | 160,588 | 3.10 | 0 |
| `_svfprintf_r` | 16,244 | 2.9% | 7,880 | 2.06 | 35 |
| `probe_stage` | 7,604 | 1.3% | 5,644 | 1.35 | 2 |
| `_printf_i` | 6,016 | 1.1% | 3,569 | 1.69 | 36 |
| `memmove` | 5,941 | 1.0% | 5,351 | 1.11 | 186 |
| `SEGGER_RTT_WriteNoLock` | 3,689 | 0.6% | 700 | **5.27** | 35 |
| `dwc2_low_level_init` | 276 | 0.0% | 16 | **17.25** | 1 |

> "指令少但 CPI 高"就是 Ozone 里最有用的信号：`dwc2_low_level_init` 16 条指令花 276 周期（等硬件），
> 而 `memmove` 5,351 条只用 5,941 周期（CPI 1.11，接近理想）。

## 4. 周期加权火焰图

「按调用权重」视图新增「切换到周期 / 切换到记录数」：宽度 = 节点累计周期（`valueCycles`）。
同一份记录切换前后前 5 块宽度：

| 口径 | 标签 | 宽度 |
|---|---|---|
| 周期 | `所选执行区间 · 569,211 周期` | 999 / 998.1 / 998.1 / **106.1** / **102** |
| 记录数 | `所选执行区间 · 210,706 条` | 999 / 998.2 / 998.2 / **202.2** / **181.5** |

窗口分析（`record_store.window()`）同样带周期，与整记录口径一致（都是 569,211 周期 / CPI 3.12）。

## 5. 长窗口（多切片聚合）

单窗受 LA 深度限制：5M 点 @500 MSa/s = **10 ms**。长窗口靠**顺序多切片**再聚合
（`python -X utf8 etm/tools/agg_cycles.py etm/records`）：

```
5 份 CC 切片 · 总周期 2,825,082 · 平均 CPI 3.19 · 总覆盖 47,084.7 µs（≈47.1 ms）
  569,211 / 559,122 / 568,946 / 568,946 / 558,857 周期 → 五片占比 19.8% ~ 20.1%
  main 2,441,595 (86.4%, 5/5) · _svfprintf_r 74,864 (2.6%) · usb_memcpy 46,200 (1.6%, 3/5)
  probe_stage 38,020 (1.3%) · _printf_i 27,978 · memmove 27,645 · memset 16,640 · SEGGER_RTT_WriteNoLock 16,379
```

五片总周期只差 **1.8%**，可直接作为性能回归基线。

## 6. TPIU 使用率与"带宽够不够"（详见 `docs/tpiu-utilization.md`）

| ETM 选项 | 配置 | trace 字节 | 使用率 | 解码 |
|---|---|---|---|---|
| 全关 | `0x1` | 16,868 | **5.62%** | ✅ |
| 返回栈 | `0x1001` | 15,306 | **5.10%** | ✅ |
| 周期计数 | `0x11` | 22,342 | **7.45%** | ✅ |
| 分支广播 | `0x9` | — | ≈18%（估算） | ❌ demux 位滑 |
| 三项全开 | `0x1019` | — | ≈19%（估算） | ❌ demux 位滑（3 次） |

结论：**带宽不是瓶颈**（最高≈19%）；BB/全开失败是采样余量（500 MSa/s 对 240 Mbit/s DDR = 2.08 样本/位），
两次失败都卡在同一物理偏移（16,028 / 16,032）。要稳定抓这两组需 ≥1 GSa/s。

## 7. 已知限制（诚实记录）

1. **周期计数只在"区间末指令"上**（OpenCSD 语义：类型 12 元素归属前一个指令区间）。
   同一区间内各指令共享这段周期，页面不假装能分摊到每条指令（Ozone 同样如此）。
2. **区间跨越函数边界**时该区间周期会算到末指令所在函数 —— 常规代码上极少，极端情况有偏移。
3. **无逐指令时间戳**：时间轴仍是执行序号；周期→时间靠 `coreHz` 折算（`timing.coreHz`）。
4. **BB/全开在 500 MSa/s 下解不出来**（见 §6）；`pin-speed` 降到 2 也没改善。
5. F103（ETMv3）**没有周期计数**，页面相关视图自动禁用并说明原因。

## 8. 测试

```
python -m unittest tools.tests.test_flow_cycles   → 6/6
python -m unittest discover -s tools/tests        → 125 项 · 120 通过（5 项缺未迁移样本，D-2 已知）
python tools/dev/test-etm.py                      → 4/4 PASS
node tools/selftest/etm-page-copy.test.mjs        → 42/0
```

## 9. 截图

* `docs/shots/etm-cycle-functions.png` —— 函数表（自身周期/占比/CPI）
* `docs/shots/etm-cycle-flame.png` —— 周期加权火焰图

## 10. 复现

```powershell
# 服务
python -X utf8 tools\dev\serve-etm.py --etm-root ..\etm --port 8878      # 在 web/ 下
# 采集（页面）：载入 ELF → 连接探针 → 勾「周期计数」→ 断点 trace_hook_entry → 联动采集
# 聚合
python -X utf8 etm\tools\agg_cycles.py etm\records
# 逐配置使用率
node tmp/tpiu-utilization.mjs 'none,bb,rs,cc,all'
```
