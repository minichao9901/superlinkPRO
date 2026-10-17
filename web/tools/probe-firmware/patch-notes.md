# 探针固件补丁说明（HID 0x32 + scope_sampler）

面向 `akaLinkPro/firmware/application_5301/`（本机路径 `E:\Share\github\akaLinkPro\...`）。
**6 处改动**，全部是"加东西"，不改动既有逻辑。改完的效果：网页「J-Scope 波形」页能真的采样。

> 网页侧的协议实现是 `app/scope/protocol.js`（包格式/命令码/状态字）与 `app/scope/view.js`（流程），
> 与本文的字节布局**必须逐字节一致**；改一边就要改另一边。设计依据见 `docs/scope-page.md`。

---

## §1 `src/rtt/rtt_bridge.h` / `rtt_bridge.c`：加 5 个 adapter

采样器要复用桥的 SWD 初始化（4 MHz 起手 → 20 MHz 斜坡 → 目标档 + 清 sticky + 失败 -4 让上层降档）
和它的块读；**不要在采样器里另写一份**，那条路径全是实测换来的细节。

`rtt_bridge.h` 末尾（`#endif` 之前）加：

```c
/* ---- 给 scope 采样器复用（见 src/scope/scope_sampler.c）----
 * 和桥走同一条 SWD 初始化路径；语义与桥一致：0 = ok、-1 SWJ_Clock、-2 SWD init、-4 该档不可用。 */
int      rtt_bridge_swd_ensure_ready(void);
int      rtt_bridge_read(uint32_t addr, uint8_t *dst, uint32_t len);   /* 0 = 成功 */
uint32_t rtt_bridge_last_dap_ticks(void);        /* 最近一次 DAP 命令的 MCHTMR 时刻（0 = 从未） */
void     rtt_bridge_set_swd_clock(uint32_t hz);
uint32_t rtt_bridge_swd_clock_hz(void);
```

`rtt_bridge.c` 里放在 `rtt_bridge_is_running()`（约 608 行）**之后**——
这样 `rtt_swd_init()` / `rtt_read_bytes()` / `rtt_swd_set_clock()` 都已经定义过，不用动它们的 static：

```c
int rtt_bridge_swd_ensure_ready(void)
{
    if (s_swd_ready) { return 0; }
    return (int)rtt_swd_init();       /* 成功时它自己会把 s_swd_ready 置 1 */
}

int rtt_bridge_read(uint32_t addr, uint8_t *dst, uint32_t len)
{
    return rtt_read_bytes(addr, dst, len);
}

uint32_t rtt_bridge_last_dap_ticks(void) { return s_last_dap; }

void rtt_bridge_set_swd_clock(uint32_t hz)
{
    s_swd_clock_req = hz;
    if (s_swd_ready) { (void)rtt_swd_set_clock(hz); s_swd_clock_hz = s_swd_clock_req; }
}

uint32_t rtt_bridge_swd_clock_hz(void) { return s_swd_clock_hz; }
```

> `rtt_bridge_note_dap_activity()` 里那句 `s_swd_ready = 0U` 正好是我们要的语义：
> 主机一旦用过 DAP（OpenOCD / 网页 RTT Viewer），采样器下次启动会重新初始化链路。

---

## §2 `src/api/api_param.c`：加 CMD 0x32

顶部（`#define RTT_ACT_*` 附近）加：

```c
#include "scope_sampler.h"

/* ---- CMD 0x32 SCOPE：探针侧 HSS 采样（见 tools/probe-firmware/）---- */
#define SCOPE_CMD               0x32U
#define SCOPE_ACT_STOP          0U
#define SCOPE_ACT_START         1U
#define SCOPE_ACT_STATUS        2U
#define SCOPE_ACT_CLOCK         3U
#define SCOPE_ACT_TRIGGER       4U     /* v2：探针侧触发，当前主机侧触发已够用 */
#define SCOPE_ACT_CONFIG        7U
#define SCOPE_ACT_BENCH         8U
#define SCOPE_ACT_BENCH_RESULT  9U
```

命令分发里（跟 `case RTT_CMD:` 平级）加：

```c
        case SCOPE_CMD:
        {
            /* 响应布局与 0x31 一致：payload[2] = 返回码（启动码）、payload[3..] = 12 个状态字。
             * ⚠️ 与 0x31 的差别：0x31 把启动码同时放在状态字 w10，而**网页对 0x32 直接读 payload[2]**
             * （app/scope/view.js 的 start() 就是这么等 -100 变 0 的）。 */
            uint8_t *out = (uint8_t *)resp;
            switch (req_hid[3])
            {
                case SCOPE_ACT_STOP:
                    scope_sampler_stop();
                    break;
                case SCOPE_ACT_START:
                    scope_sampler_request_start();          /* 只登记；SWD 在主循环里做 */
                    break;
                case SCOPE_ACT_CLOCK:
                    scope_sampler_set_clock(((uint32_t)req_hid[4]) | ((uint32_t)req_hid[5] << 8) |
                                            ((uint32_t)req_hid[6] << 16) | ((uint32_t)req_hid[7] << 24));
                    break;
                case SCOPE_ACT_CONFIG:
                {
                    uint32_t period = ((uint32_t)req_hid[4]) | ((uint32_t)req_hid[5] << 8) |
                                      ((uint32_t)req_hid[6] << 16) | ((uint32_t)req_hid[7] << 24);
                    uint8_t flags = req_hid[8];
                    uint8_t n = req_hid[9];
                    scope_var_t vars[SCOPE_MAX_VARS];
                    if (n > SCOPE_MAX_VARS) { n = SCOPE_MAX_VARS; }
                    for (uint8_t i = 0; i < n; i++)
                    {
                        const uint8_t *p = &req_hid[10 + (uint16_t)i * 6U];
                        vars[i].addr = ((uint32_t)p[0]) | ((uint32_t)p[1] << 8) |
                                       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
                        vars[i].size = p[4];
                        vars[i].type = p[5];
                        vars[i].rsv = 0U;
                    }
                    scope_sampler_configure(period, flags, n, vars);
                    break;
                }
                case SCOPE_ACT_BENCH:
                    scope_sampler_request_bench(((uint32_t)req_hid[4]) | ((uint32_t)req_hid[5] << 8) |
                                                ((uint32_t)req_hid[6] << 16) | ((uint32_t)req_hid[7] << 24));
                    break;
                case SCOPE_ACT_BENCH_RESULT:
                {
                    uint32_t iters = 0U, ticks = 0U;
                    int32_t err = 0;
                    if (!scope_sampler_bench_result(&iters, &ticks, &err)) { out[2] = 0xFFU; break; }
                    memcpy(&out[3], &ticks, 4);              /* 网页按 ticks/24 → µs 换算 */
                    memcpy(&out[7], &iters, 4);
                    memcpy(&out[11], &err, 4);
                    break;
                }
                case SCOPE_ACT_TRIGGER:                      /* v2：先只回 OK */
                case SCOPE_ACT_STATUS:
                default:
                    break;
            }
            if (req_hid[3] != SCOPE_ACT_BENCH_RESULT)
            {
                uint32_t w[12];
                (void)scope_sampler_status(w, 12U);
                out[2] = (uint8_t)(int8_t)scope_sampler_start_result();   /* -100 = 排队中 */
                for (uint8_t i = 0; i < 12U; i++) { memcpy(&out[3U + (uint16_t)i * 4U], &w[i], 4); }
            }
        }
        break;
```

**报文长度**：一条 CONFIG = `1 + 4 + 1 + 1 + 6×8 = 55` 字节 ≤ 61（`buildRequest` 的 data 段上限）✓
所以 8 个变量**一条报文就够**，这也是网页把上限定在 8 的原因。

---

## §3 `src/main.c`：主循环加一句

```c
#include "scope_sampler.h"
...
        rtt_bridge_poll();
        scope_sampler_poll();        /* J-Scope 采样器（与 RTT 桥互斥） */
```

---

## §4 `src/usb/usb_composite.c`：把闲置的 EP 0x83 用起来

这个端点在描述符里早就声明了（`SWO_IN_EP = 0x83`，512 B bulk IN），固件里也注册了，
只是因为 `SWO_STREAM=0` **从来没人写过它** —— 现在把它当采样数据面。

```c
#include "scope_sampler.h"
...
void swo_in_callback(uint8_t busid, uint8_t ep, uint32_t nbytes)
{
    (void)busid;
    (void)ep;
    (void)nbytes;
    /* 一次 512 B 写完成 → 还回一个包缓冲。
     * 回调不带缓冲下标，采样器内部按 FIFO 还（提交顺序 = 完成顺序）。 */
    scope_sampler_tx_complete();
}
```

**不需要改描述符、不需要装驱动、不需要新增浏览器授权** —— 浏览器侧只是换一个端点号读
（`app/scope/transport.js` 里找的就是 `endpointNumber === 0x83` 的 bulk IN）。

---

## §5 `CMakeLists.txt`：把新源文件加进来

```cmake
sdk_app_src(src/scope/scope_sampler.c)
```
（放在 `sdk_app_src(src/rtt/rtt_bridge.c)` 后面即可；新文件放 `src/scope/` 目录。）

---

## §6 互斥：和 RTT 桥只能开一个

两者都要独占 SWD、都在主循环里跑。**互相停一下最省事**：

```c
/* 0x31 的启动分支里 */
case RTT_ACT_START:
    scope_sampler_stop();          /* ← 加这一行 */
    ...
/* 0x32 的启动分支里 */
case SCOPE_ACT_START:
    rtt_bridge_stop();             /* ← 加这一行 */
    scope_sampler_request_start();
```

不这么做的话典型症状是：桥每 20 ms 抢一次 SWD，采样周期被撕得七零八落（而且两边都不报错）。

---

## §7 编译期自检清单

| 检查 | 期望 |
|---|---|
| `SWO_IN_EP` 可见 | `usb_composite.h` 里 `#define SWO_IN_EP 0x83` |
| `SCOPE_PACKET == DAP_PACKET_SIZE` | 都是 512（HS bulk 的 wMaxPacketSize） |
| `ATTR_PLACE_AT_NONCACHEABLE_BSS_WITH_ALIGNMENT` 可见 | 与 `cdc_interface.c` 用 `uart_rx_buf` 时同一个来源（`board.h`）—— 包缓冲必须在非 cacheable 段，否则 DMA 读到旧数据 |
| `hpm_common.h` / `HPM_MCHTMR_BASE` | 与 `rtt_bridge.c` 同一个 `mchtmr_now()` 写法 |
| 编译告警 | `-Wall` 下 `scope_sampler.c` 应为 0 warning |

---

## §8 已知没做（写在这里免得当成 bug）

- **探针侧触发**（`SCOPE_ACT_TRIGGER` 只回 OK）：触发目前做在主机侧 —— 20 s 的数据都在内存里，
  改个阈值就能立刻重新定位，反而比探针侧更灵活。
- **数组整段采样**：网页侧 v1 只支持标量与结构体成员（数组会带原因列出来）。
- **指针跟踪 / 局部变量**：不做（DWARF 里没有固定地址的会自动剔除并说明原因）。
- **不做压缩/差分**：会毁掉波形可信度，宁可如实丢样本也不做"看起来更顺"的加工。
