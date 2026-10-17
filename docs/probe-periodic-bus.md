# Probe 端 SPI/I²C 周期任务（待上板验证）

周期由 HPM5301 的 GPTMR1 通道 1 驱动，共用 LED 的 IRQ，保留通道 0 和其时钟。
GPTMR0 通道 0 的 CDC 刷新不变。没有任务时关闭采集通道、中断与计数器。
定时 ISR 仅清状态、停当前计数器、登记到期标志；主循环执行既有收发函数。
使用按下一次到期时间重装的定时方式，没有常驻高频 tick。

主循环 `main.c` 未修改；通过已有 `i2c_bridge_req_kind=5` 唤醒 SPI 或 I²C 周期工作，
不增加每圈轮询函数或第二个标志读。LED ISR 增加通道 1 状态判断。
这属于结构上的开销控制，不能据此宣称 RTT/J-Scope 实测零退化。
RTT/J-Scope 正在运行时辅助事务延后 20 ms 再尝试；已有 SWD/JTAG 汇编和关中断区间不变。
辅助事务已经启动后不能撤销，偶尔并发仍可能占用 CPU/总线。通常分开使用。

## 执行契约

- 最多 8 个任务组，每组 512 B、16 条记录；周期 1–60000 ms。
- 单条 SPI 完整帧不超过 128 B，每帧读不超过 54 B；支持 XFER、CS、GPIO、PING、AUX_IN。
  DELAY 转为独立非阻塞记录。STEP/RESET/CFG、长读、大块刷图留在原一次性入口。
- I²C 使用原 XFER 字节布局，写最多 51 B、读最多 54 B；定时组不支持 SCAN/长读自动分片。
- 组内顺序执行，包括等待。不同组不交错执行，避免 CS 或器件状态串扰。
  每个 SPI 周期结束或取消后释放 CS，因此 CS 不跨周期保持。
- 全部组先上传和预备，再统一 RUN，首拍立即执行。SPI 次数按成功周期，I²C 按尝试周期，
  与原 runner 口径一致。持续失败的 SPI 有限次数循环仍需要用户停止。
- 周期是目标节拍，不保证硬实时。错过节拍时跳过并计数，不突发补跑、不无限排队。
  结果带采集开始的 probe 毫秒时间、周期号、任务代数和累计跳过数。
- 结果队列 32 条；满后停止任务、回报 OVERFLOW，不静默覆盖，也不继续执行无处保存结果的写。
  页面冻结/主机停止取结果仍可能使队列满。它是有界采集缓冲，不是长期离线记录器。
- STOP/USB RESET/DISCONNECTED 作废任务代数；已启动的一笔事务可能完成，但结果不再发布。
  CS 清理由主循环完成，确认 cleanup=0 后才释放会话。网页 STOP 失败保留占用，可重试。
- 运行/预备期间拒绝重配和普通总线操作。结果分片通过 seq+epoch 核对、显式 ACK 出队。
  模块固定数组，无动态分配，新增状态约 8 KiB；最终 SRAM/Flash 余量须以固件链接 map 核实。

## HID 0x37 / BPT1

请求实际 64 B：Report ID、长度、0x37、action、参数。`req[1]` 为 CMD+data 字节数，
即标准网页 `buildRequest()` 的 `1+data.length`，不含长度字节和 Report ID。
响应 `res[1]` 为含 Report ID 的总长；`res[4..7]` 为 u32 LE 返回码，数据从 `res[8]` 开始。
返回码：0 OK、1 RANGE、2 BUSY、3 STATE、4 EMPTY、5 OVERFLOW。

| action | 参数 / 响应数据 |
| --- | --- |
| 0 CAPS | BPT1 ASCII、jobs:u8、program:u16、steps:u8、data:u8、queue:u8 |
| 1 CLEAR | 无；只在无已预备/运行/清理任务时清空程序、队列和 fault |
| 2 PUT | slot:u8、bus:u8、offset:u16、n:u8、bytes[n≤55]；顺序分片写入 |
| 3 START | slot:u8、period_ms:u32、count:u32；预备该组，返回 epoch:u32 |
| 4 STOP | slot:u8（255 为全部）；取消预备、运行与在飞结果发布 |
| 5 STATUS | active_mask、queued、fault、epoch、cleanup_mask，均 u32 |
| 6 READ | offset:u8；固定头 seq/epoch/cycle/time_ms/skipped（5×u32），slot/step/err/len（4×u8），最多 32 B 数据 |
| 7 ACK | seq:u32；仅弹出这个序号对应的队首记录 |
| 8 RUN | 无；统一启动预备组，使用同一时间基准 |

程序记录：kind:u8、flags:u8（0）、length:u16 LE、payload。
kind 1=I²C XFER 参数，2=SPI 原完整帧，3=DELAY u32 微秒（0–60000000）。

## 已完成的离线验证

```sh
make SHELL=/bin/bash .SHELLFLAGS=-c PYTHON=python3 test-host
ASAN_OPTIONS=detect_leaks=0 BUS_HOST_SANITIZE=1 python3 script_test/bus_periodic_host_test.py
# 在配套网页仓库：
PROBE_FIRMWARE_REPO=/path/to/akaLinkPro node tools/selftest/bus-periodic.test.mjs
```

生产调度 C 的虚拟时间模型覆盖：自主周期、多组、非阻塞尾延时、迟拍、满队列、
SPI 成功次数、核心任务让路、执行期间复位、停止与非法请求；ASan/UBSan 通过。
跨仓库测试使用网页真实 HID 组包、生产 C 调度器和生产 SPI/I²C 参数校验函数，
覆盖 54 B 分片、组身份、网页暂不取结果、取消、溢出及 STOP 失败保留占用。
既有 JTAG、Scope、RTT STOP、SPI DRAIN 和 TARGET guard 主机回归通过。

对定时器、SPI/I²C、API、LED 和 USB 修改做了官方 SDK 头文件下的主机语法检查；
主机不能编译 RISC-V ISR 属性，检查该部分时仅移除了 ISR 声明宏。
没有 RISC-V 交叉工具链，未完成固件链接、栈/存储余量或实板验证。

## 上板验收

1. 编译固件，检查新增约 8 KiB 状态后的 RAM/栈与 Flash 余量。
2. SPI/I²C 10/50/100 ms 采集，用逻辑分析仪量相邻 CS/START；前台/后台、暂停结果读取分别检查。
3. 验证组内写→等待→读、次数、停止立即重启、切换模式、断开和 USB 复位，CS 不悬挂。
4. 同板同配置旧→新→旧，辅助采集关闭时比较 RTT 字节率/坏记录/丢失，以及 J-Scope
   probe 采样率、端到端采样率和丢包；出现可重复退化必须定位后再发布。
5. 偶尔并发验证核心优先，辅助跳过/超期计数诚实，不能以页面到包时间代替采样时间。

该功能改变的是总线读取的节拍，不保证外部 ADC 的实际转换时刻同步。
