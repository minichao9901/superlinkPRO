# HSS 亚微秒周期协议（已实测 STM32F103CB）

旧 HID 0x32 action 7 保持不变：u32 周期单位 µs，包 version=1，时间戳单位 µs。
新增 action 10 CONFIG_TICKS，字段布局与 action 7 相同，周期单位改为 24 MHz tick。
最小周期 48 ticks（2 µs），最大 24000000 ticks（1 s）。

| 周期 | ticks | 名义速率 |
| --- | --- | --- |
| 3 µs | 72 | 333.33 kHz |
| 2.75 µs | 66 | 363.64 kHz |
| 2.5 µs | 60 | 400 kHz |
| 2.25 µs | 54 | 444.44 kHz |
| 2 µs | 48 | 500 kHz |

action 10 会发 version=2 的 DEF/DATA/STAT：
- 包头 u32 时间戳是原始 tick，约 179 s 回绕；主机先按 u32 去回绕，再除以 24 得 µs。
- DEF payload[4..7] 和 STAT payload[16..19] 的 u32 周期都是 ticks。
- 帧载荷、序号、类型、长度、flags 与 v1 相同。
- 新网页同时解析 v1 和 v2；旧网页不能使用 v2。

STATUS w0 bit2=1 表示支持 CONFIG_TICKS。STATUS w11 bit17=1 表示当前周期是 tick 单位，
w11 低 16 位为周期原始单位；与旧状态一样，长周期在这个紧凑字段里会截断，完整周期以 DEF 为准。
网页在发送 action 10 前检查支持位，旧固件应明确要求升级，不能把未知动作当成成功配置。

离线验证：`python script_test/scope_host_test.py` 编译实际 sampler C，替换 SDK 外设和 timer MMIO；
覆盖旧/新单位、posted read 值连续性、满包 flush、周期限幅、非法变量宽度。
完整 HPM 固件编译需原有 SDK/工具链，吞吐、采样抖动、USB 并发需上板验证。

## Bounded single-word batches

STATUS word 0 bit 3 advertises `SCOPE_FLAG_FAST_BATCH` (configuration flags bit 7).
When requested for a direct single-word SWD plan at <=72 ticks (3 µs), one poll
handles at most 16 samples and returns at packet boundaries. The tight 48-tick
period (2 µs) use up to 64 samples and keep the budget across packet boundaries.
All samples still wait for their deadlines. Buffer starvation, errors or control
requests return promptly. Interrupts remain enabled; other main-loop services
can be delayed by at most 128 us nominal plus sample/interrupt cost.
JTAG, multi-span, and slower plans retain the scalar scheduler. Disable bit 7 for
A/B measurements. This changes service cadence, not the nominal sampling period.

The single-word sample path also bypasses the generic span loop. Posted reads still
write the previous slot, and the last slot is flushed before handing a packet to USB.
Host tests cover stop/error, packet boundaries, and the 32-bit timer wrap. Target
instruction timing, ISR latency and throughput require an on-board measurement.

## Measurement snapshot (HID 0x32 action 11)

STATUS word0 bit4 advertises this action. The first 12 little-endian u32 words
keep their layout. A STOP-tail capable HPM probe adds word 12; use the HID
length field to distinguish 48- and 52-byte snapshots from report padding.

| Word | Meaning |
| --- | --- |
| 0 | Magic `0x31535348` (HSS1) |
| 1 | Hardware timer tick, u32 wrap |
| 2 | Timer frequency, 24000000 Hz |
| 3 | Accepted sample slots, including a posted read later counted as invalid |
| 4 | Scheduler skipped samples |
| 5 | USB buffer exhaustion samples |
| 6 | SWD errors |
| 7 | DAP yield events |
| 8 | USB TX completions for protocol packets (excluding STOP ZLPs) |
| 9 | Formatted bytes (includes discarded packets in DISCARD mode) |
| 10 | Full sampling period in ticks |
| 11 | Flags bits0..7; running bit8; on STOP-tail firmware, draining bit9 and reset-aborted bit10 |
| 12 (optional) | Invalid accepted sample slots omitted after a posted-read failure |

Timer and counters are captured in a short critical section. Counters are full u32;
use modulo subtraction across rollover. At 24 MHz a timer window must be shorter
than 179 seconds. A USB completion counts a packet; it does not certify application
receipt. Host receipt uses its own monotonic arrival window. Startup and post-STOP
drain must be excluded from both rates. The two windows have small HID boundary
skew, so their difference alone cannot be interpreted as an exact loss count.

M0 now uses an independent aligned 64-byte scratch buffer while stopped. Queued
USB DATA packets are never used as benchmark destinations, and posted-read
pointers are cleared before the scratch buffer leaves scope. Running benchmarks
are rejected before link initialization.

## STOP tail on HPM EP 0x83

STATUS word0 bit5 advertises the opt-in tail protocol, bit6 is draining, and
bit7 records a USB reset that interrupted a drain. Bit0 goes low when sampling
stops, before delivery completes. A bare STOP retains the old behavior.

1. Keep the ordered WebUSB reader alive and pause normal rearming. Send HID
   action 0 with one extra byte `1`. A canceled queued START also returns an
   end marker; an already idle probe has no new stream to finish.
2. The sampler stops scheduling in the ISR, then finalizes any posted read in
   the main loop. A final DATA packet is still 512 bytes on the wire; its header
   lists only the valid samples. After all queued packets, one USB zero-length
   packet (ZLP) makes the current native read return short, including zero
   bytes when the data ended exactly on a read boundary. This is not a protocol
   packet and does not affect its sequence or sample count.
3. Deliver the bytes of that short read before acknowledging EOF. After
   STATUS confirms the first drain, send action 13 once, with the exact number
   of additional reads already submitted (1–32, omit the action when zero).
   The probe sends that many ZLPs and the host checks that all those reads end
   empty. Then read STATUS until bit6 clears. No device reset is needed for
   this successful path.
4. With DISCARD off, the final count must satisfy `received + invalid == produced`.
   Report invalid samples separately from scheduling gaps and USB buffer
   exhaustion. A timeout, malformed end read or reset is not a clean stop.

Old probes can still be used with the legacy STOP. The browser delivers any
reads that complete in the bounded legacy drain and reports count mismatches;
the old probe cannot supply a final partial packet. The Zynq EP 0x87 STOP
handshake is separate. See the [offline report](validation/hss-stop-offline-20261107/README.md)
for the pending hardware checks.
