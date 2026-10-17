==============================================================================
更新（2026-10-03）：处置结果 —— 已修 6 处、按决定不做 6 处、未采用 3 项
==============================================================================

核实结论：本报告的核心结论经**代码 / 汇编 / ELF 反汇编 / map** 四方核对，**全部属实**
（不是幻觉产物，行号、常量、地址都对得上）。下面只写"改没改、为什么"。
相关提交：`9b24cf8`（固件 6 文件 +240/-7）、`0553ca0`（回归编排层的编码修复）。

------------------------------------------------------------------------------
✅ 已修（commit 9b24cf8）
------------------------------------------------------------------------------

  P1-1 ✅ SW_DP.c 的 SWD_Read() 收 data == NULL 时改用本地 dummy，与 swd_host_port.c
       早就有的保护对齐。核实补充：DAP.c 里的 NULL 调用实为 **5 处**
       （965/1006/1171/1968/2027 —— 报告列了 4 处，漏了 2027）；raw 通道
       （rtt_bridge.c 经 DAP_ExecuteCommand）走同一条路，一并覆盖。
       实测证据链：objdump `.vectors` 首字 = `f4060000` = `0x000006f4` = map 里的
       `irq_handler_trap`；6 个档位 blob 均为无条件 `sw a5, 0(a1)`（60M.S:569 与报告
       完全一致）。

  P1-3 ✅ usb_composite.c 新增 `USB_ResetGen`：复位中断里 +1；排空循环记账前在
       关中断状态下比一次代数，变了就整轮作废（计数停在 ISR 清零后的相等状态）。
       核实补充：计数确为 `volatile uint16_t`（:502-505），`while (CountI != CountO)`
       在 :872，主循环 `CountO++` 在 :904。

  P2-5 ✅ 写串超过 FIFO 深度时改走自写的两段式：先 ISSUE，再按 FIFOFULL 逐字节泵，
       随后发读帧（START+STOP+ADDR+DATA / DIR=READ —— 因为写帧不发 STOP，这里的
       START 天然是 repeated START）。写串 ≤4B 仍走 SDK 老路（实测一直是对的）。
       SDK 侧证据：`i2c_master_address_read` 在 ISSUE 之前把 addr_size 个字节一口气
       塞进 FIFO 且不查 FIFOFULL，而同文件其他路径（L360/L543）都查 —— 反差即证据。
       （FIFO 深度取文档最小值 4 作保守判据；`I2C_CFG.FIFOSIZE` 是硬件只读字段，
       2/4/8/16 四档，本 SoC 的具体档位未上板钉死，但无论 4 还是 16，原来的写法都错。）

  P2-6 ✅ SET_CFG / ENABLE 在事务进行中回 E_BUSY。原先 RESET(:778)/XFER(:824) 查了、
       这两个没查 —— 报告的描述精确到"哪些查了、哪些没查"，属实。

  P2-7 ✅ 新增 `spi_bridge_owns_pad()`，I2C 使能时检查 SPI 桥是否已把 PA28/PA29 配成
       辅助脚。原先只有 `sb_pad_usable → i2c_bridge_owns_pad` 这一个方向。

  P3  ✅ I2C 杂项三项一并修掉：`I2C_ST_SHIFT_CMD` 的重复定义（.c:123 与 proto:62，
       删掉 .c 那份，以 proto 头为唯一真源）、死变量 `buf[4]` 及其 `(void)buf`、
       `i2c_bridge_proto.h` 里 BUS_OK 的注释按实现订正（它只表示控制器不忙
       BUSBUSY=0；"有没有挂起请求"看 I2C_ST_PENDING）。

------------------------------------------------------------------------------
❌ 经评估不做（每条都附实测理由）
------------------------------------------------------------------------------

  P1-2 ❌ **实测有害**：把 DAP_XFER_SIZE 缩成 512 会砸掉 60 MHz 档 ——
         | 固件                              | 60M HSS 标定（scope bench --set one） |
         | 原样（1024）                      | 605.6 ~ 610.1 kHz  ✓                  |
         | 只把该值改成 512                  | **err=-4，链路在该档读不动**  ✗       |
         | 其余修复不动、只把它改回 1024     | 610.1 kHz  ✓（swd_ops 仍在移动后的地址）|
       真因不是包长语义，而是**内存布局**：缓冲 4×1024→4×512 腾出 4 KB，DLM 里后续
       缓冲/栈整体前移，ILM 里的 blob 缓冲区 `swd_ops` 也挪了 368 B；而 60 MHz 档的
       bit-bang blob 是 6 指令/bit 的最紧时序，余量薄到会被这点位移吃掉。
       ⇒ ZLP 那个隐患要修就在**端点侧**解决（例如只给 DAP 的 IN 端点打开 ZLP），
       并且必须连同 60 MHz 档一起上板复测。这段结论已写进 `DAP_config.h` 的注释。

  P2-4 ❌ **试过，实现有害**：按"初始化前存档 DAP_Data、主机一活动就重放"实现了
       （含 `DAP_RestoreHostState` / `DAP_HostClockHz`），实测**污染主机与桥的时钟**：
       烧完固件第一次回归 RTT@60M 满分通过，之后只要 OpenOCD 碰过 DAP（P1.sram 阶段）
       就变成确定性失败 `lost=1401 / rd_err=7`，连跑 3 次数值一模一样。
       原因是 `rtt_swd_set_clock()` 本身就是靠**合成一条 DAP `SWJ_Clock` 命令**
       （`dap_cmd`）换挡的，与"存档/重放 DAP_Data"互相纠缠。
       ⇒ 要修得换设计（例如只还原 transfer 配置、绝不动时钟，且只在桥停下来时做）。

  P2-8 ❌ 电气问题，**按用户决定保持现状**：nRESET 直连（PA08 → J5.3 → 目标 NRST）、
       `OD_SET(0)` 推挽 39 Ω 不变。取舍已当面确认 —— 改开漏后"释放复位"只能靠上拉
       （本脚 100 k + 目标侧 NRST 内部 ≈40 k，配合板上 ~100 nF，上升时间约 ms 级），
       而这条线一错就全挂；报告自己也写着"需要上板确认"。等真遇到"目标自己复位被
       探针顶住"再改。

  P3  ❌ **汇编时序一律不碰**（用户明确要求）：blob 忽略 `swd_conf.turnaround` /
       `data_phase`、ACK 协议错误不补 33 拍退避 —— 保持现状；JTAG 恒 45 MHz
       （`JTAG_DP.c` 硬编码 45M blob）同样保持现状。

  P3  ❌ 与 P2-8 同批决定：`PIN_nRESET_IN` 读输出锁存而非引脚实际电平 —— 不动。
       附带风险提示：这个脚配成输出时输入通路可能是关的（HPM 要
       `LOOP_BACK`="force input on" 才活），改不好会读成恒 0（="一直被复位"），
       反而可能让 OpenOCD 误判，所以不适合"顺手改"。

  P3  ❌ 有意搁置（要动协议）：I2C 的 PINTEST / BITPROBE 在 HID 中断里各占
       200~300 µs。这两个是网页侧**同步取结果**的诊断命令，改主循环异步就得改协议
       + 网页，而收益只是"手动触发的诊断少占点中断"，不值这个改动量（与前两轮判定
       一致）。

------------------------------------------------------------------------------
🔶 已实现但未采用（在补丁里留档，想用时 `git apply` 即可）
------------------------------------------------------------------------------

    · P3  `DAP_JTAG_CJTAG_Sequence` 错误分支回报真实请求长度（原固定为 1，会让
          ExecuteCommands 里后续命令错位解析）
    · P3  `DAP_JTAG_Configure` 的 count 上界钳到 `DAP_JTAG_DEV_CNT`（原可越界写
          200+ 次、踩坏邻接静态变量）
    · P3  `DAP_SWD/JTAG/CJTAG_TransferBlock` 的 `request_count` 钳到
          `(DAP_XFER_SIZE-4)/4`（防手滑/恶意 count 把响应缓冲写穿）
    补丁：`%TEMP%\akalinkpro-opus5-5-fixes-20261003.patch`（27805 B）

------------------------------------------------------------------------------
验证记录（两套回归都在改后跑过）
------------------------------------------------------------------------------

  make regression-swd（STM32F103ZE 靶子）—— 连跑 3 次 PASS fails=0（64.1/64.1/64.3 s）
      P1.sram @60M      xfer 写 3382 / 读 2915 KB/s，8 档逐字节校验通过
      P2.rtt 20/45/60M  1388 / 2516 / 2972 KB/s，全部 LOSSLESS（lost=dup=0）
      P3.bench-one@60M  604.6 kHz；fit R²=0.9998；pack@60M 89.0 kHz
      P3.run            one@3µs 335.9 kHz（丢 0.2%）/ pack@12µs 82.5 kHz（丢 1.5%）

  make regression-riscv（HPM6800EVK / HPM6880）—— PASS fails=0（40.9 s）
      P1.rbench 1517.4 / P1.wbench 1525.8 KB/s；sbastat sticky=0 retries=0
      P2.delivery 1398.7 KB/s（rderr=wderr=0）；P2.loss 14.4 MB lost=0 dup=0
      P3.integrity@100µs 32364 样本 bad=0
      P3.bench-one 321.9 kHz / bench-rv 27.8 kHz
      P3.run-rv@200µs 契约 PASS（lfsr 15359/15359）；run-one@10µs 100.9 kHz（丢 0.0%）

  注 1：一次回归里出现过 `P2.rtt@45M` 丢 17 字节（rd_err=wr_err=0）—— 属已记录的
        既有抖动（孤立跑 10/10 LOSSLESS，只在完整回归上下文里偶发），重跑即绿。
  注 2：RISC-V 回归要求**刚烧过的探针** —— sbastat 的"阻塞重试"是开机以来累计、
        `stop`/`open` 都不清零；探针跑过 SBA 活且中途卡过就会让 `retries==0` 契约
        必红，重烧即可。
  注 3：`make regression-riscv` 曾因**编码**假红（子脚本用控制台代码页 cp936 写中文、
        编排器按 UTF-8 读 → 正则匹配不上 → 取默认 -1 判 FAIL）；已在 `0553ca0` 于
        编排层统一注入 `PYTHONIOENCODING=utf-8:replace` 根治。

==============================================================================
以下为原报告正文（未做任何改动）
==============================================================================

  P1

  1. 主机每做一次 AP 读，都会往探针自己的 0x00000000 写 4 字节，那里是异常入口指针
  - DAP.c:965/1006/1171/1968 投递 AP 读和最后的写确认时调用 SWD_Read(req, NULL)。
  - 所有档位的读 blob 在收完数据后都无条件执行 sw a5, 0(a1)（SW_DP_GPIO_ASM_60M.S:569，20M/30M/36M/45M/SLOW 都一样）。
  - ILM 的 0x0 就是 __vector_table[0]，向量模式下它存的是 irq_handler_trap 的地址（map 里 .vectors 起始 0x0，trap 入口在 0x6f4）。
  - 后果：只要 OpenOCD 读过一次内存，异常入口就被换成目标的数据。平时没有异常所以看不出来；一旦发生任何异常，CPU 会跳到一个随机地址，而不是进 SDK 的 trap 处理。
  - swd_host 那条路在 swd_host_port.c:42 已经把 NULL 换成了 dummy，主机路径漏了。
  - 修法：在 SW_DP.c 的 SWD_Read() 里加一行 uint32_t dummy; if (!data) data = &dummy;。
  - 上板确认办法：OpenOCD 跑一次 mdw 后，用 RTT_ACT_PEEK 读探针的 0x0。修复前应该不再是 0x000006f4。

  2. DAP_XFER_SIZE 设成 1024（2 倍包长），但端点关掉了零长度包（ZLP），恰好 512 B 的命令或响应会把传输"粘住"
  - SDK 给所有端点都设了 zero_length_termination = 1（hpm_usb_device.c:236，意思是不自动补 ZLP），DAP 的 IN 回调也不手动补。OUT 方向按 1024 武装。所以一笔恰好 512 B 的传输不会结束，会一直等后面的数据。
  - IN 方向：TransferBlock 读 127 个字时响应正好 512 B。从 1KB 页内偏移 0x204 开始的块读，按 TAR 自增边界切块，第一块就是 127 字。按 pyOCD 的切块方式这很常见，后果是主机读超时或两条响应粘在一起。
  - OUT 方向：DAP_Transfer 请求有 38 种读写项组合恰好是 512 B。OpenOCD 只防"等于报告包长 1024"的情况（packet_usable_size = pkt_sz - 1），挡不住 512。
  - 修法：你们自己的 docs/experiment-dap-resp-size.md 已经测过，1024 端到端没有提速。最省事的是把 DAP_XFER_SIZE 改回 DAP_PACKET_SIZE。

  3. USB 复位打在主循环执行 DAP 命令期间，会把大约 6.5 万条陈旧命令重新执行一遍
  - usb_composite.c:550-557 在复位中断里把计数清零（#11 的修复引入），但主循环随后照样执行 USB_RequestCountO++（:904）。
  - 结果 CountO 比 CountI 大 1，while (CountI != CountO) 要等到 16 位计数回绕才会停。Python 模拟出来是 65536 条，执行的全是缓冲里的旧命令，里面可能有对目标的写。
  - 窗口不大，但后果重。
  - 修法：加一个复位代数。主循环执行命令前记下代数，执行完在关中断的状态下比一次，变了就跳过索引更新。

  P2

  4. RTT 桥和 scope 采样器会悄悄改掉主机设好的 DAP 状态，而且不恢复。
     - 主机空闲后 RTT 会重新初始化：rtt_swd_init → swd_init → DAP_Setup()。
     - 这一步会重置 TransferConfigure（idle、重试次数、匹配重试）、换成 RTT 自己的 SWD 时钟和 blob，并把 debug_port 强制设成 SWD。
     - OpenOCD 之后发的命令就在 RTT 的时钟下跑。如果主机原本连的是 JTAG，它的 Transfer 会被分派到 SWD 引擎上。
     - 修法：初始化前保存 DAP_Data，主机一有活动就恢复。或者只做必要的初始化，不调 DAP_Setup。
  5. I2C 先写后读的事务，写的部分超过 4 字节就会出错。
     - 写的部分是"子地址 + 写数据"，最长 55 字节，被当作 addr 交给 SDK 的 i2c_master_address_read（i2c_bridge.c:378）。
     - SDK 在 hpm_i2c_drv.c:196-200 一口气写 DATA，不检查 FIFOFULL，而 FIFO 只有 4 字节。超出部分丢掉，从机收到半截写，接着超时、SCL 被拽住，只能 RESET 恢复。协议上这种写法是允许的，测试脚本没覆盖到。
     - 修法：改用 i2c_master_seq_transmit/receive，或者临时在 rd_len>0 时拒绝超过 4 字节的写。
  6. I2C 的控制命令在 HID 中断里执行，和主循环正在跑的事务没有互斥。
     - SET_CFG 和 ENABLE 不检查有没有事务在跑，会在事务中途重新初始化控制器。
     - 在 RECOVER 打拍期间收到 ENABLE=0：主循环随后会执行 init_i2c_bridge_pins()，把两根脚又复用回 I2C，但这时 s_enabled=0，SPI 桥会认为这两根脚可以拿去用。
  7. I2C 和 SPI 抢引脚的检查只做了一个方向。 SPI 桥会问 I2C 桥有没有占用这根脚，但 I2C 在 ENABLE 时不检查 SPI 是不是已经把 PA28/PA29（辅助脚索引 16/17）配成了辅助脚。注释里写的"谁后配谁赢"在这个方向上依然存在。
  8. EVKLite 的 nRESET 是推挽直连。 DAP_config.h:795 设了 OD=0，释放时以 39Ω 驱动拉高。STM32 这类目标的 NRST 是双向的，内部复位（SYSRESETREQ、看门狗）会和探针顶着。建议直连板改成开漏，需要上板确认。

  P3（择要）

  - blob 忽略 swd_conf 里的 turnaround 和 data_phase，ACK 协议错误时也不补 33 个时钟的退避；Slow 的 C 版本有这两个处理。
  - PIN_nRESET_IN 读的是输出锁存而不是引脚实际电平（DAP_config.h:622），所以 SWJ_Pins 看不到目标自己拉低的复位。
  - DAP_JTAG_CJTAG_Sequence 走错误分支时，回报的请求长度是 1（DAP.c:703），放在 ExecuteCommands 里会让后续命令错位解析。
  - I2C 的 PINTEST 和 BITPROBE 在中断里各占大约 200～300 µs。
  - I2C 杂项：proto 里 BUS_OK 的注释和实现不一致；I2C_ST_SHIFT_CMD 重复定义（i2c_bridge.c:123）；buf 是死变量。
  - 之前判定"留着"的遗留项都还在：JTAG_Configure 不检查设备数（DAP.c:720）、bulk 路径计数不钳位、JTAG 恒用 45M 等。

  建议顺序

  先修 1（一行）和 3（大约 10 行）。2 直接把 DAP_XFER_SIZE 回退到 512，再跑一次 sram-test 回归。之后处理 5、6、4。

  要不要我照前几轮的格式把这份写进 docs/代码审查报告.md，或者直接动手修 P1？
