# USB 初始化寄存器读写清单（静态扫描 + ETM 执行流）

目标：回答“USB 初始化过程中，程序读了哪些 USB_OTG 寄存器、写了哪些、顺序如何”。之前担心的 trace 记录还在：使用 `capture-4395612c313f47ce97af1e1c17e0a0d5`（10 ms，210,706 条指令）从启动阶段覆盖 USB 初始化；不是 echo 记录。

## 结论

- 静态扫描初始化函数：**76 个访问点**（纯读 27、纯写 22、读-改-写 27）。
- 该 10 ms 记录实际执行：**52 个访问点**（纯读 24、纯写 14、读-改-写 14）。
- 初始化顺序很清晰：先读 `CID/GSNPSID/GHWCFG1..4` 和 `dwc2_get_hwparams` 的能力参数，再写 `GAHBCFG/DCTL/GCCFG/GUSBCFG`，做 core reset/mode，分配 RX/TX FIFO，配置端点/中断掩码，flush FIFO，最后打开全局中断和 DCTL。
- 完整 76 行（含未执行）见 [usb-init-register-access.csv](usb-init-register-access.csv)；下表是实际执行的 52 行。

## 方法与覆盖范围

1. **静态**：扫描 `usb_dc_dwc2.c` 的初始化函数（`usb_dc_init`、`dwc2_core_init`、`dwc2_reset`、`dwc2_set_mode`、`dwc2_set_turnaroundtime`、`dwc2_set_txfifo`、`dwc2_flush_*`、`usb_dc_deinit` 等）里的 `USB_OTG_*` 访问。
2. **分类**：赋值号左侧的 `=` 记为**写**；`|=`/`&=`/`^=` 记为**读-改-写**；条件、右值、函数参数里的寄存器记为**读**。
3. **补内联读**：`dwc2_get_hwparams()` 在 `usb_dwc2_param.h` 里通过 `reg_base + GSNPSID_OFFSET` / `GHWCFG*_OFFSET` 读 `GSNPSID/GHWCFG1..4`，单独补入（否则只扫 `USB_OTG_*` 会漏掉这 5 个读）。
4. **ETM**：每条指令事件带 `seq/pc/function/file/line/mnemonic`；按源码行聚合，`seq` 给执行顺序，`ldr*`/`str*` 给读/写 PC。ETM 没有数据值，所以只能还原“读/写哪里、什么顺序”，不能还原读到的值或写入的值。
5. **未覆盖**：HAL/GPIO/时钟初始化里的间接寄存器访问、`USB_OTG_FS` 之外的 CMSIS 访问、中断路径；本表只覆盖 USB_OTG 初始化本身。

## 实际执行的 52 个访问点（按执行顺序）

记录：`capture-4395612c313f47ce97af1e1c17e0a0d5`（10 ms，210,706 指令）。`seq` 是 ETM 执行序号；`ld/st` 是源码行上实际出现的读/写指令 PC（最多列前两个，完整列表在 CSV）。

| seq | 函数 | 源码 | 操作 | 寄存器 | ETM 指令 | ld/st PC |
|---:|---|---|---|---|---:|---|
| 8519 | `usb_dc_init` | `usb_dc_dwc2.c:498` | 读 | `USB_OTG_GLB->CID` | 8 | ld `0x8003022` `0x8003028` |
| 9914 | `usb_dc_init` | `usb_dc_dwc2.c:499` | 读 | `USB_OTG_GLB->GSNPSID` | 6 | ld `0x8003038` `0x800303a` |
| 11219 | `usb_dc_init` | `usb_dc_dwc2.c:500` | 读 | `USB_OTG_GLB->GHWCFG1` | 6 | ld `0x8003048` `0x800304a` |
| 12781 | `usb_dc_init` | `usb_dc_dwc2.c:501` | 读 | `USB_OTG_GLB->GHWCFG2` | 6 | ld `0x8003058` `0x800305a` |
| 14086 | `usb_dc_init` | `usb_dc_dwc2.c:502` | 读 | `USB_OTG_GLB->GHWCFG3` | 6 | ld `0x8003068` `0x800306a` |
| 15427 | `usb_dc_init` | `usb_dc_dwc2.c:503` | 读 | `USB_OTG_GLB->GHWCFG4` | 6 | ld `0x8003078` `0x800307a` |
| 16740 | `dwc2_get_hwparams` | `usb_dwc2_param.h:298` | 读 | `USB_OTG_GLB->GSNPSID` | 1 | ld `0x80026f6` |
| 16741 | `dwc2_get_hwparams` | `usb_dwc2_param.h:300` | 读 | `USB_OTG_GLB->GHWCFG1` | 1 | ld `0x80026f8` |
| 16742 | `dwc2_get_hwparams` | `usb_dwc2_param.h:301` | 读 | `USB_OTG_GLB->GHWCFG2` | 1 | ld `0x80026fa` |
| 16743 | `dwc2_get_hwparams` | `usb_dwc2_param.h:302` | 读 | `USB_OTG_GLB->GHWCFG3` | 1 | ld `0x80026fc` |
| 16744 | `dwc2_get_hwparams` | `usb_dwc2_param.h:303` | 读 | `USB_OTG_GLB->GHWCFG4` | 1 | ld `0x80026fe` |
| 19206 | `usb_dc_init` | `usb_dc_dwc2.c:519` | 读+写 | `USB_OTG_GLB->GAHBCFG` | 6 | ld `0x80030ec` `0x80030f2` / st `0x80030fa` |
| 19212 | `usb_dc_init` | `usb_dc_dwc2.c:521` | 读+写 | `USB_OTG_DEV->DCTL` | 4 | ld `0x80030fc` `0x80030fe` / st `0x8003106` |
| 19216 | `usb_dc_init` | `usb_dc_dwc2.c:524` | 写 | `USB_OTG_GLB->GCCFG` | 3 | ld `0x800310a` `0x800310c` / st `0x800310e` |
| 19227 | `dwc2_core_init` | `usb_dc_dwc2.c:86` | 读+写 | `USB_OTG_GLB->GUSBCFG` | 7 | ld `0x8002be6` `0x8002bec` / st `0x8002bf4` |
| 19245 | `dwc2_reset` | `usb_dc_dwc2.c:51` | 读 | `USB_OTG_GLB->GRSTCTL` | 6 | ld `0x8002b08` `0x8002b0e` |
| 19253 | `dwc2_reset` | `usb_dc_dwc2.c:55` | 读+写 | `USB_OTG_GLB->GRSTCTL` | 3 | ld `0x8002b1a` / st `0x8002b20` |
| 19269 | `dwc2_reset` | `usb_dc_dwc2.c:63` | 读 | `USB_OTG_GLB->GRSTCTL` | 6 | ld `0x8002b40` `0x8002b46` |
| 19286 | `dwc2_set_mode` | `usb_dc_dwc2.c:124` | 读+写 | `USB_OTG_GLB->GUSBCFG` | 6 | ld `0x8002cc2` `0x8002cc8` / st `0x8002cd0` |
| 19295 | `dwc2_set_mode` | `usb_dc_dwc2.c:129` | 读+写 | `USB_OTG_GLB->GUSBCFG` | 7 | ld `0x8002cd8` `0x8002cde` / st `0x8002ce6` |
| 19305 | `dwc2_set_mode` | `usb_dc_dwc2.c:133` | 读 | `USB_OTG_GLB->GINTSTS` | 7 | ld `0x8002d00` `0x8002d06` |
| 19316 | `usb_dc_init` | `usb_dc_dwc2.c:533` | 读 | `USB_OTG_GLB->GRXFSIZ` | 8 | ld `0x8003124` `0x800312a` |
| 19336 | `usb_dc_init` | `usb_dc_dwc2.c:538` | 读 | `USB_OTG_GLB->DIEPTXF0_HNPTXFSIZ` | 3 | ld `0x80031ac` |
| 19359 | `usb_dc_init` | `usb_dc_dwc2.c:540` | 读 | `USB_OTG_GLB->DIEPTXF` | 32 | ld `0x8003174` `0x8003174` |
| 19553 | `usb_dc_init` | `usb_dc_dwc2.c:548` | 读+写 | `USB_OTG_GLB->GOTGCTL` | 3 | ld `0x80031e8` / st `0x80031ee` |
| 19556 | `usb_dc_init` | `usb_dc_dwc2.c:549` | 读+写 | `USB_OTG_GLB->GOTGCTL` | 6 | ld `0x80031f0` `0x80031f6` / st `0x80031fe` |
| 19562 | `usb_dc_init` | `usb_dc_dwc2.c:552` | 读+写 | `USB_OTG_GLB->GUSBCFG` | 6 | ld `0x8003200` `0x8003206` / st `0x800320e` |
| 19572 | `usb_dc_init` | `usb_dc_dwc2.c:555` | 写 | `USB_OTG_GLB->DIEPTXF` | 105 | ld `0x8003214` `0x800321a` / st `0x8003226` `0x8003226` |
| 19737 | `usb_dc_init` | `usb_dc_dwc2.c:559` | 写 | `USB_OTG_PCGCCTL` | 5 | ld `0x8003230` `0x8003236` / st `0x800323a` |
| 19742 | `usb_dc_init` | `usb_dc_dwc2.c:562` | 读+写 | `USB_OTG_DEV->DCFG` | 4 | ld `0x800323e` `0x8003240` / st `0x8003248` |
| 19758 | `usb_dc_init` | `usb_dc_dwc2.c:579` | 读+写 | `USB_OTG_DEV->DCFG` | 6 | ld `0x800331c` `0x8003322` / st `0x800332c` |
| 19764 | `usb_dc_init` | `usb_dc_dwc2.c:586` | 写 | `USB_OTG_DEV->DIEPMSK` | 6 | ld `0x8003330` `0x8003336` / st `0x800333e` |
| 19770 | `usb_dc_init` | `usb_dc_dwc2.c:587` | 写 | `USB_OTG_DEV->DOEPMSK` | 2 | ld `0x8003340` / st `0x8003342` |
| 19772 | `usb_dc_init` | `usb_dc_dwc2.c:588` | 写 | `USB_OTG_DEV->DAINTMSK` | 2 | ld `0x8003346` / st `0x8003348` |
| 19774 | `usb_dc_init` | `usb_dc_dwc2.c:591` | 写 | `USB_OTG_GLB->GINTMSK` | 2 | ld `0x800334c` / st `0x800334e` |
| 19776 | `usb_dc_init` | `usb_dc_dwc2.c:593` | 写 | `USB_OTG_GLB->GINTSTS` | 3 | ld `0x8003350` / st `0x8003356` |
| 19779 | `usb_dc_init` | `usb_dc_dwc2.c:596` | 写 | `USB_OTG_GLB->GINTMSK` | 3 | ld `0x8003358` `0x800335a` / st `0x800335c` |
| 19788 | `usb_dc_init` | `usb_dc_dwc2.c:607` | 读+写 | `USB_OTG_GLB->GINTMSK` | 6 | ld `0x80033ea` `0x80033f0` / st `0x80033f8` |
| 19794 | `usb_dc_init` | `usb_dc_dwc2.c:614` | 写 | `USB_OTG_GLB->GRXFSIZ` | 8 | ld `0x80033fa` `0x8003400` / st `0x8003410` |
| 19823 | `dwc2_set_txfifo` | `usb_dc_dwc2.c:258` | 读 | `USB_OTG_GLB->GRXFSIZ` | 36 | ld `0x8002c54` `0x8002c5a` |
| 19829 | `dwc2_set_txfifo` | `usb_dc_dwc2.c:261` | 写 | `USB_OTG_GLB->DIEPTXF0_HNPTXFSIZ` | 2 | st `0x8002c66` |
| 22371 | `dwc2_set_txfifo` | `usb_dc_dwc2.c:263` | 读 | `USB_OTG_GLB->DIEPTXF0_HNPTXFSIZ` | 16 | ld `0x8002c7c` `0x8002c7c` |
| 22378 | `dwc2_set_txfifo` | `usb_dc_dwc2.c:269` | 写 | `USB_OTG_GLB->DIEPTXF` | 40 | st `0x8002caa` `0x8002caa` |
| 24880 | `dwc2_set_txfifo` | `usb_dc_dwc2.c:265` | 读 | `USB_OTG_GLB->DIEPTXF` | 112 | ld `0x8002c8e` `0x8002c8e` |
| 42767 | `dwc2_flush_txfifo` | `usb_dc_dwc2.c:174` | 读 | `USB_OTG_GLB->GRSTCTL` | 6 | ld `0x80028b6` `0x80028bc` |
| 42775 | `dwc2_flush_txfifo` | `usb_dc_dwc2.c:177` | 写 | `USB_OTG_GLB->GRSTCTL` | 3 | st `0x80028ce` |
| 42784 | `dwc2_flush_txfifo` | `usb_dc_dwc2.c:183` | 读 | `USB_OTG_GLB->GRSTCTL` | 6 | ld `0x80028dc` `0x80028e2` |
| 42805 | `dwc2_flush_rxfifo` | `usb_dc_dwc2.c:151` | 读 | `USB_OTG_GLB->GRSTCTL` | 6 | ld `0x8002856` `0x800285c` |
| 42813 | `dwc2_flush_rxfifo` | `usb_dc_dwc2.c:154` | 写 | `USB_OTG_GLB->GRSTCTL` | 2 | st `0x800286a` |
| 42821 | `dwc2_flush_rxfifo` | `usb_dc_dwc2.c:160` | 读 | `USB_OTG_GLB->GRSTCTL` | 6 | ld `0x8002878` `0x800287e` |
| 42831 | `usb_dc_init` | `usb_dc_dwc2.c:633` | 读+写 | `USB_OTG_GLB->GAHBCFG` | 6 | ld `0x80034b0` `0x80034b6` / st `0x80034be` |
| 42837 | `usb_dc_init` | `usb_dc_dwc2.c:634` | 读+写 | `USB_OTG_DEV->DCTL` | 4 | ld `0x80034c0` `0x80034c2` / st `0x80034ca` |

## 静态有、但这条 10 ms 记录没执行的访问点

共 24 个；主要是 `usb_dc_deinit`（去初始化不在启动窗口内），以及 `usb_dc_init`/`dwc2_reset`/`dwc2_core_init`/`dwc2_set_mode`/`dwc2_set_turnaroundtime` 里未走到的分支。完整行见 CSV。

| 函数 | 未执行访问点数 |
|---|---:|
| `usb_dc_deinit` | 9 |
| `usb_dc_init` | 7 |
| `dwc2_reset` | 3 |
| `dwc2_core_init` | 2 |
| `dwc2_set_turnaroundtime` | 2 |
| `dwc2_set_mode` | 1 |

## 关键观察

- **先读后写**：`CID/GSNPSID/GHWCFG*` 和能力参数读取发生在任何配置写之前；之后才动 `GAHBCFG/DCTL/GCCFG/GUSBCFG`。
- **读-改-写是常态**：`GAHBCFG/DCTL/GUSBCFG/GRSTCTL/GOTGCTL/DCFG/GINTMSK` 多数是 `|=`/`&=`，所以一条源码行同时有 `ldr` 和 `str`；表里标成“读+写”。
- **FIFO 分配是循环**：`dwc2_set_txfifo` 的 `DIEPTXF` 读/写行指令数很高（112/40），因为按端点循环；`usb_dc_init` 的 `DIEPTXF` 写也有 105 条指令/15 次 store。
- **flush 也是轮询**：`dwc2_flush_txfifo`/`rxfifo` 都是“写 flush 位 → 轮询 `GRSTCTL` 等完成”，所以同一寄存器出现 write 后多次 read。
- **最后才开中断**：`GAHBCFG`/`DCTL` 的最终写在 flush 之后（seq 42831/42837）。

## 限制

- 这是源码级访问点 + ETM 执行位置，不是总线事务级追踪；ETM 没有数据值。
- 记录只覆盖 10 ms 启动窗口；`usb_dc_deinit` 和未走到的分支不会出现在“已执行”表，但保留在 CSV 静态表中。
- 源文件行号必须与记录里的固件一致；本表使用 `fw.elf` 对应的 `usb_dc_dwc2.c`/`usb_dwc2_param.h`。
- 值要另外拿：固件访问器日志、DWT watchpoint 或带数据 trace 的调试器；当前 ETM 硬件不支持 data trace。

## 复现

```bash
python -X utf8 etm/tools/usb_init_reg_access.py \
  --source etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dc_dwc2.c \
  --header etm/firmware/stm32h743_cdc_trace/cherryusb/port/dwc2/usb_dwc2_param.h \
  --record etm/records/capture-4395612c313f47ce97af1e1c17e0a0d5 \
  --out docs/usb-init-register-access.csv
```
