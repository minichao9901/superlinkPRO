# 实验记录：放大 CMSIS-DAP 包尺寸（方案 A）— 已完成、已合并

记录：原实验分支 feat/dap-resp-size（已删除；本文件保留在 main 上）
日期：2026-09-28
状态：**已完成并合并进 main**（提交 `11c15dd`，最终 `DAP_XFER_SIZE = 2×mps = 1024`；
请求/响应缓冲、三处 `DAP_OUT_EP` 武装、`DAP_Info(PKT_SZ)` 四处同源）。
实测块读 127 → 254 ops/包、1..60 MHz 八档全部校验通过，但**端到端速度不变** ——
真因与解释见文末「二轮复现」：主机路径从来不是往返次数受限，OpenOCD 的 4 深流水
早已把每包开销隐藏。保留代码的价值：① `DAP_Info` 不再虚报"一次只能 508 B"；
② 去掉"OUT 只武装 mps ⇒ 大包命令被截断"这个陷阱。
（本文开头几节保留了当时**错误**的推断，作为记录；结论一律以「二轮复现」为准。）

## 动机

主机驱动那条路（`make sram-test` / `dump_image`）60 MHz 档只有 2443 KB/s，而同档
链路天花板是 3281 KB/s —— 差的 26% 全花在 **USB 往返次数** 上：

```
DAP_Info(0xFE) 报 max packet size = DAP_PACKET_SIZE = 512
⇒ 每次 DAP_TransferBlock 最多带 ~508 B 回来
⇒ 2443 KB/s ÷ 512 B ≈ 4770 趟/秒（每趟 ~210 µs，典型 HS 往返延迟）
```

思路：CMSIS-DAP 的 "packet size" 语义是**单条命令响应的最大字节数**（可跨多个 USB 包），
与端点 mps（HS 硬上限 512）无关。把它调大 ⇒ 主机自动用更大的块 ⇒ 往返次数降下来。

## 改动（三处，共 6 行）

1. `src/dap/DAP.h`：新增 `#define DAP_RESP_SIZE 2048U`（附语义说明与取值理由）
2. `src/dap/DAP.c`：`DAP_Info(DAP_ID_PACKET_SIZE)` 改报 `DAP_RESP_SIZE`
3. `src/usb/usb_composite.c`：`USB_Response[DAP_PACKET_COUNT][DAP_RESP_SIZE]`
   （`USB_Request` 保持 `DAP_PACKET_SIZE`，主机发来的命令都很短；
     EP 描述符的 `wMaxPacketSize` 也保持 512）

## 实测结果

| 取值 | 构建 | 现象 |
| --- | --- | --- |
| 4096 | ✗ 链接失败 | `.stack will not fit in region DLM`，DLM 溢出 **864 字节** ⇒ HPM5301 的 DLM 放不下 4×4096 |
| 2048 | ✓ 构建通过、能烧、HID/CDC 正常 | **OpenOCD 全挂**：`Error: error submitting USB read: Entity not found`（反复重试、设备复位都无效） |
| 回退到基线 | ✓ | **OpenOCD 依然报同一个错** ⇒ ⚠️ **不能归因于本次改动**，是主机侧/USB 句柄状态卡住了 |

关键旁证（说明**设备本身没坏**）：

- HID 通道正常（`evk_ident.py` 能读到固件编译时间）
- CDC 正常（COM52 在，串口可用）
- **DAP 引擎也正常**：走 HID 透传（`CMD_RTT` action 4/6）执行 CMSIS-DAP 命令有响应
  （`rtt_rawdap.py` 能跑完整流程，SWD 那几步返回 0x07 = 目标侧没 ACK，属目标未初始化）
- 唯独**主机经 bulk 端点读 DAP 响应**这条路失败

## 待办（探针 USB 重插之后继续）

1. **先重插探针 USB**，确认 `openocd ... -c "init"` 恢复正常、`make sram-test` 回到基线
2. 再做真正的 A/B：基线（512）vs `DAP_RESP_SIZE=2048`，看 60 MHz 档读是否从
   2443 → 接近 3281 KB/s
3. 若仍复现 `Entity not found`，怀疑这两条（按可能性排序）：
   - **HPM5301 的 USB 端口驱动可能不支持「一次 `usbd_ep_start_write` 超过 mps」**
     （需要多包状态机）；查 `usb_dc_hpm.c` 里 EP 写长度 > mps 的处理，必要时改成分包发送
   - CMSIS-DAP**v2 over bulk** 的约定可能要求 `packet_size == 端点 wMaxPacketSize`，
     主机（OpenOCD 的 `cmsis_dap_usb_bulk.c`）会据此设置读缓冲；若是这样，方案 A
     在这条主机路径上行不通，得走"自写主机工具 + 多包/异步 URB"（方案 B）
4. 另一个可选方向：**固件只对 v1/HID 路径放大响应**，或干脆把批量读内存也做进
   CDC 流（方案 C，同 RTT 桥，能到链路 89%）

---

## 最终 A/B 结果（2026-09-28 12:3x，主机侧 USB 恢复后）

主机恢复后（换了 USB 口，探针 CDC 变 COM5）重测，同口径对比：

| SWD 档 | 基线 RESP=512 读 / 写 | 实验 RESP=2048 读 / 写 |
| --- | --- | --- |
| 1 MHz | 85.7 / 85.5 | **FAILED（数据校验错）** |
| 2 MHz | 176.0 / 176.6 | **FAILED** |
| 10 MHz | 760.7 / 799.3 | （未继续） |
| 20 MHz | 1296.5 / 1347.3 | （未继续） |
| 36 MHz | 1905.5 / 2070.3 | （未继续） |
| 45 MHz | 2146.3 / 2333.9 | （未继续） |
| **60 MHz** | **2428.2（复测 2523.2） / 2906.8（复测 2868.7）** | — |

**结论：方案 A（按下述改法）不可行** —— 把 `DAP_Info(0xFE)` 报成 2048、响应缓冲放大到
2048 之后，`load_image`/`dump_image` 的数据**校验直接失败**（1/2 MHz 就错，与 SWD 时序无关，
是结构性错误）。最可能的原因：本工程的 USB 栈（CherryUSB + HPM5300 端口驱动）**不支持
一次 `usbd_ep_start_write` 超过端点 mps**，超过 512 字节的响应无法正确分包发出。

⇒ 要救活方案 A，必须先把「单次写超过 mps」这条路径做对：
  1. 查 `usb_dc_hpm.c` / CherryUSB 的 EP 写实现，确认是否支持多包（多数实现需要调用方
     自己按 mps 分包续传，或用 `usbd_ep_start_write` 的链式长度）
  2. 或者改成分包发送 + 完成回调续传（像 CDC IN 那条链的做法）
  3. 若驱动确实只支持 ≤ mps，则方案 A 在 bulk 路径上没有意义，只能走方案 B（自写主机工具
     + `DAP_ExecuteCommands` 批处理 / 异步 URB）或方案 C（把批量读也下沉进 CDC 流）

基线固件已重新烧回（CRC 0xDF382B3F，60 MHz 读 2523 / 写 2869 KB/s 复核正常），板子可用。

---

## 二轮复现（2026-09-28 13:0x，分支 feat/usb-fifo）：**真因是 OUT 方向没放大**

上面"最可能的原因：USB 栈不支持一次写超过 mps"**是错的**。逐层查证结果：

| 猜想 | 证据 | 结论 |
| --- | --- | --- |
| USB PHY FIFO 可配 | `usb_glue_hpm.c` 只有 IRQ/OTG 钩子，**没有任何 FIFO 配置**；该控制器是 dQH/dTD 架构（`hpm_usb_device.c`），没有 Synopsys 式 `DIEPTXF` | ✗ 不存在这个旋钮 |
| QTD 太少 | `hpm_soc_feature.h:87 USB_SOC_DCD_QTD_COUNT_EACH_ENDPOINT = 8`（且 `#ifndef` 可覆盖），1 个 QTD 管 16 KB，**单次最多 128 KB** | ✗ 不是瓶颈 |
| 驱动不支持 >mps 的单次写 | CDC IN 就是一次 `usbd_ep_start_write(…, size)` 写整个线性窗（RTT 桥按 2048 B 入环），实测 2.9 MB/s 无损跑了 12 s | ✗ 早就支持 |

**真因**：方案 A 只放大了响应侧，**请求侧仍是 512**：

```c
usbd_ep_start_read(0, DAP_OUT_EP, USB_Request[i], DAP_PACKET_SIZE);   // ← 应为 DAP_XFER_SIZE
```

`DAP_TransferBlock` 的**写**命令 = 3 + 4×N 字节。主机把 packet size 当 2048/1024 后，
一次会发上千字节的 OUT 传输，而设备只武装了 512 ⇒ 只收下头 512 B 就完成一次 dTD，
而 `DAP_SWD_TransferBlock`（`DAP.c:1929`，循环内**没有任何长度检查**）仍按请求头里的
`count` 去读 `USB_Request[512..N]` 的越界内容当写数据 ⇒ 写进目标是垃圾 ⇒
**数据校验必错，且与 SWD 时钟无关**（1 MHz 也错）—— 与一轮观测完全吻合。

### 改法（`feat/usb-fifo`，3 个文件）

`DAP_config.h` 新增 `DAP_XFER_SIZE`（默认 `2×DAP_PACKET_SIZE`=1024，带 64..32768 与
mps 整数倍两条 `#error` 守卫），然后三处必须**同源**：

1. `usb_composite.c`：`USB_Request`/`USB_Response` 第二维 → `DAP_XFER_SIZE`
2. `usb_composite.c`：**三处** `usbd_ep_start_read(0, DAP_OUT_EP, …)` → `DAP_XFER_SIZE`
3. `DAP.c`：`DAP_Info(DAP_ID_PACKET_SIZE)` 报 `DAP_XFER_SIZE`（端点描述符仍是 512 的 mps）

### 复现结果：放大生效了，速度却不变

OpenOCD 自报 `Packet Size = 1024 / Packet Count = 4`；用 `openocd -d4` 数每包事务数
（`Executing N queued transactions`）：一次 20 KB dump 的 **20 个包各带 254/255 ops
（≈1016 B/包）**，基线是 ~40 个 127-ops 包 ⇒ 块尺寸确实翻倍。1..60 MHz 八档**全部
`verified`**（结构性问题消失）。

| SWD 档 | 基线512 读/写 | 1024 读/写 |
| --- | --- | --- |
| 10 MHz | 760.7 / 799.3 | 766.4 / 810.9 |
| 20 MHz | 1296.5 / 1347.3 | 1282.2 / 1375.3 |
| 36 MHz | 1905.5 / 2070.3 | 1840.2 / 1900.5 |
| 45 MHz | 2146.3 / 2333.9 | 2194.8 / 2431.0 |
| **60 MHz** | 2428.2(2523.2) / 2906.8(2868.7) | **2503.9 / 2896.0** |

⇒ **端到端速度与包大小无关**。所以"4770 趟/秒 × 210 µs"那条推理不成立：OpenOCD 的
4 深 pending FIFO + 异步 URB **已经把每包固定开销隐藏掉了**，包变小并不会变慢——
主机路径从一开始就不是往返次数受限，而是
`max(探针 SWD 位翻转率, USB 传输率) ⊕ 少量固定开销`：同档 **in-probe 纯 SWD 读 3281 KB/s**，
dump 只到 2504（79%），load 2896 ≈ 同档交付出力的上限。

**结论：块大小不是杠杆。** 60 MHz 档真正还能挖的是那 21%：SWD 引擎本身
（60 MHz 已经是抖动边缘，80 MHz 不可用）或每比特的指令数，不是 CMSIS-DAP 包大小。
保留本分支的价值：`DAP_Info` 不再虚报"一次只能 508 B"，且高 SWD 档下少了 20 次往返/20 KB；
代价是 DLM 从 89.6% 涨到 **92.8%**（130304 B 里用 120928 B）。
