# USB 寄存器写入站点清单（静态扫描 + ETM 执行流）

目标：在不做故障注入、不改固件的前提下，回答“**程序到底在哪些地方写 USB_OTG 寄存器**”，并用现有 ETM 记录标出哪些站点实际执行、执行顺序如何。

## 结论

- 静态扫描 `usb_dc_dwc2.c` 的 `USB_OTG_*` 左值赋值：**147 个写入站点**。
- 用 kit 里 `capture-e4dac69c…`（2026-10-19 00:04:28，50 ms，1,035,584 条事件）做 ETM 关联：**10 个站点实际执行**（其余是初始化/其他路径，未进入这条 50 ms 窗口）。
- 完整 147 行清单见 [usb-register-write-sites.csv](usb-register-write-sites.csv)。
- 初始化阶段的**读写**（含 `dwc2_get_hwparams` 内联读）已用 10 ms 启动记录补齐：[USB 初始化寄存器读写清单](usb-init-register-access.md)。

## 方法

1. **静态站点**：扫描 `etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dc_dwc2.c`，取赋值号**左侧**含 `USB_OTG_GLB/DEV/INEP/OUTEP/FIFO/PCGCCTL` 的语句，记录 `文件:行 / 函数 / 操作符 / 寄存器表达式`。
2. **ETM 执行流**：现有 ETM 记录里每条指令事件都带 `pc / function / file / line / mnemonic`。流式扫描 `events.jsonl`，按 `file=usb_dc_dwc2.c` + `line` 聚合：命中次数、首个 `seq`、`str*` 的 PC。
3. **合并**：站点行号与 ETM 行号对齐；`executed=true` 表示这次记录里执行过；`first_seq` 即在这条记录里的先后顺序；`store_pcs` 是实际执行到的 store 指令地址。

复现：

```bash
python -X utf8 etm/tools/usb_write_sites.py \
  --source etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dc_dwc2.c \
  --record C:/Users/Administrator/Downloads/etm-bridge-kit/data/records/capture-e4dac69c27314f5788597506bcb2335f \
  --out docs/usb-register-write-sites.csv
```

## 本次记录实际执行的 10 个站点（按执行顺序）

记录：`capture-e4dac69c27314f5788597506bcb2335f`（50 ms，1,035,584 事件；ELF `c610192c…`）。

| 顺序 seq | 函数 | 源码行 | 操作 | 寄存器 | ETM 指令数 | store PC |
|---:|---|---:|---|---|---:|---|
| 72 | `dwc2_get_inep_intstatus` | 482 | `=` | `USB_OTG_INEP(epnum)->DIEPINT` | 3 | `0x08002AE2` |
| 162 | `dwc2_ep0_start_read_setup` | 294 | `=` | `USB_OTG_OUTEP(0)->DOEPTSIZ` | 5 | `0x08002D4C` |
| 389 | `usbd_ep_start_write` | 923 | `&=` | `USB_OTG_INEP(ep_idx)->DIEPTSIZ` | 9 | `0x08003B5A` |
| 398 | `usbd_ep_start_write` | 924 | `&=` | `USB_OTG_INEP(ep_idx)->DIEPTSIZ` | 6 | `0x08003B6C` |
| 416 | `usbd_ep_start_write` | 942 | `\|=` | `USB_OTG_INEP(ep_idx)->DIEPTSIZ` | 9 | `0x08003CFC` |
| 425 | `usbd_ep_start_write` | 943 | `\|=` | `USB_OTG_INEP(ep_idx)->DIEPTSIZ` | 7 | `0x08003D0E` |
| 454 | `usbd_ep_start_write` | 968 | `\|=` | `USB_OTG_INEP(ep_idx)->DIEPCTL` | 7 | `0x08003C28` |
| 463 | `usbd_ep_start_write` | 971 | `\|=` | `USB_OTG_DEV->DIEPEMPMSK` | 9 | `0x08003C4A` |
| 636 | `dwc2_ep_write` | 314 | `=` | `USB_OTG_FIFO(ep_idx)` | 203 | `0x08002DB8` |
| 1031 | `dwc2_tx_fifo_empty_procecss` | 404 | `&=` | `USB_OTG_DEV->DIEPEMPMSK` | 3 | `0x08002F78` |

这 10 个站点已经能还原 echo 写回的核心步骤：清 DIEPINT → 设置 DOEPTSIZ（EP0 setup）→ 改 DIEPTSIZ/DIEPCTL（CNAK/EPENA）→ 置 DIEPEMPMSK → 往 FIFO 写数据（`dwc2_ep_write` 循环 203 条指令/29 次 store）→ FIFO 空后清 DIEPEMPMSK。

## 静态站点分布（前 10 个函数）

| 函数 | 站点数 |
|---|---:|
| `usb_dc_init` | 26 |
| `USBD_IRQHandler` | 23 |
| `usbd_ep_start_write` | 18 |
| `usbd_ep_start_read` | 15 |
| `usbd_ep_close` | 12 |
| `usb_dc_deinit` | 9 |
| `dwc2_tx_fifo_empty_procecss` | 7 |
| `usbd_ep_open` / `usbd_ep_set_stall` / `usbd_ep_clear_stall` | 各 4 |

## 限制

- 这是**源码级**扫描，只覆盖显式 `USB_OTG_*` 左值赋值；通过 HAL 或其他宏间接写寄存器的路径不在内。
- 记录的行号必须与当前源码一致；本表使用 ELF `c610192c…` 对应的源码。
- **值仍然不可知**：ETM 只给了执行位置/顺序/PC，没有给写入值；要值需要固件访问器日志。
- 本次 echo 记录从路径中间开始，所以只命中 10 个站点；初始化阶段已用 10 ms 启动记录 `capture-4395612c…` 单独扫描，见 [USB 初始化寄存器读写清单](usb-init-register-access.md)。
