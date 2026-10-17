# 探针 `rc=-4` 与 RTT「丢字节」定因报告（2026-10-04 实测）

两个"疑难杂症"都在这份里结案：**① J-Scope START 偶尔拿到 `rc=-4`「该档位链路不可用」是真问题，
触发条件是 60 MHz 档的换挡瞬态、根因是 scope 这条路径少了一层重试；② `P2.rtt@45M 丢 17 字节`
不是丢数据 —— 是相位口径的假象 + 采集边界上 ≤2 KB 的重复前缀。**

| 问题 | 结论 | 证据强度 |
| --- | --- | --- |
| ① `rc=-4` | 真 bug（缺重试），只在 **60 MHz** 档触发：**87/670 ≈ 13%**；≤45 MHz **0/301** | PEEK 探针内部变量 + 分档扫描 + 对照实验，可复现 |
| ② `丢 17 字节` | **不是丢数据**：带序号素材 262 MB **真丢 0 B**；异常是会话边界 ~2 KB **重复** | 目标侧换素材逐条对账 + 探针计数器 + 你们脚本复跑 3/3 LOSSLESS |

现场：本机 STM32F103ZE（OpenOCD 读回 `device id = 0x10036414`，512 KiB） + akaLinkPro 探针。
全部测量**绕开页面**，用 `tools/selftest/probe-hid-diag.py` 直接说 HID 协议的 `0x31`（RTT 桥）/
`0x32`（scope 采样器）—— 量到的就是探针本身的行为，排除页面因素。

---

## 一、`rc=-4`：scope START 的 AP 验收读没有重试

### 1.1 一次 scope START 在固件里是两级串联

```c
/* scope_sampler.c: scope_link_try() */
int rc = rtt_bridge_swd_ensure_ready();     /* ① 复用桥的初始化：swd_init → swd_init_debug
                                                 → 20M 斜坡 → 目标档 → 读一次 DP IDCODE 吃瞬态
                                                 失败回 -1 / -2 / -4（rtt_bridge.c:270-292）*/
if (rc != 0) { return rc; }
scope_req_init();
if (rtt_bridge_read(s_span[0].start, s_stage, 4U) != 0) { s_swd_ready = 0U; return -4; }  /* ② 验收读 */
```

两级都会给 -4，但修法完全不同，所以先把"到底哪一级"钉死。

### 1.2 分档量级：只有 60 MHz 档会挂

| SWD 档 | START 次数 | `rc=-4` |
| --- | --- | --- |
| 8 MHz | 60 | **0** |
| 20 MHz | 60 | **0** |
| 45 MHz | 181 | **0** |
| **60 MHz**（页面与 `full_flow_*` 现在用的就是这档） | 670 | **87（≈13%，单轮 3%~17% 波动）** |

与"采不采样""采哪个地址"无关：period 20 µs 真采样与 period 1 s 几乎不采样同样 ~10%；
把变量地址从 `0x20000000`（SRAM）换成 `0xE000EDF0`（PPB）失败率不变。

### 1.3 失败在哪一级：PEEK 探针自己的 `s_swd_ready`

探针的 HID `CMD_RTT` 有 action 5 = **PEEK**（读探针自己的内存，`api_param.c:311`）。
从 `build_dfu_evklite/output/akaLinkPro_App.map` 取到符号地址：

```
0x00081ead  1 B  .sbss.s_swd_ready   ← rtt_bridge.c（init 成功才置 1）
0x00081f54  1 B  .sbss.s_swd_ready   ← scope_sampler.c（另一份，别认错）
```
（`s_rescans_since_step`/`s_discard`/`s_rd_pending_valid` 与它同在一个对齐字里，
所以 PEEK 对齐地址 `0x81EAC` 一个 32 位字就能一次看完 4 个字节。）

`--mode=disc` 的结果：**失败 3 次，`桥 s_swd_ready` 全是 1**；成功时也是 1。

> ⇒ `rtt_swd_init()`（JTAG2SWD + DP 上电 + 20M→60M 换挡 + 吃掉瞬态的那次 DP 读）**是成功的**，
> 挂掉的是它后面那次 **AP 验收读**（`scope_sampler.c:757`）。

旁证：桥自己的 AUTOSTART 走**同一条** `rtt_swd_init()`，`--mode=bridge` **100/100 rc=0**
（它的首个 AP 访问是控制块扫描，被两层重试盖住了，见 1.4）。

### 1.4 为什么只有 scope 这条漏出来：三处兄弟路径都重试，就它没有

| 代码 | 首个"换挡后 AP 访问"失败时的处理 | 实测是否漏错 |
| --- | --- | --- |
| `rtt_find_cb()`（桥的 CB 扫描，`rtt_bridge.c:351`） | 清 sticky + **就地重读一次**；仍失败才交给外层 | 漏不出（`--mode=bridge` 100/100 rc=0） |
| `rtt_bridge_start()`（`rtt_bridge.c:574`） | 外层再来 **3 次扫描、间隔 20 ms** | 同上 |
| `rtt_bridge_run_bench()`（`rtt_bridge.c:941`） | 清 sticky + **重试一次**（注释原文："换挡后的瞬态读失败"） | — |
| **`scope_link_try()` 的验收读** | **一次都不重试** | **≈13% 直接冒成 -4** |

固件作者其实早就知道这个现象 —— `rtt_swd_init` 里写着"换挡后的第一次访问最容易踩到瞬态"，
`rtt_bridge_run_bench` 的注释直接点名"换挡后的瞬态读失败"。**scope 这条路径是漏网的那条。**

### 1.5 而且"补一次重试"救不了：必须重新初始化

`--mode=bench`：scope start 之后紧跟一次 BENCH（`rtt_bridge_run_bench`，读同一地址 4 B，**自带一次
零延迟重试 + 清 sticky**）。因为 scope start 成功后桥的 `s_swd_ready` 已是 1，BENCH 会**跳过初始化**
（`rtt_bridge.c:925`），所以量的正是"读 + 一次重试"这一级：

```
60 MHz × 60 次：scope rc 失败 6 次；BENCH err=-4 也是 6 次 —— 且**完全重合**
（"scope 失败但 BENCH 成功" = 0 次）
```

`--mode=recover`：失败后按 0 / 2 / 5 / 10 / 20 / 50 / 100 ms 连探（每次都带 BENCH 的自带重试）：
**18/18 全部失败** —— 瞬态不是"几毫秒的毛刺"，光等/光重试都读不动。

真正能恢复的是**重新初始化**：循环里失败的下一拍（+56 ms，会重走 `rtt_swd_init`）成功率 ~90%，
200 次里 31 次失败**基本孤立**（相邻两次都失败罕见）。这也解释了现场现象：

- 页面 `scopeRun` 外面包的"停采样 → 重连 HID/USB → 再试一次"**对症**（它触发一次完整 init）；
- akaLinkPro 的 `rtt_clock_step_down()` + `rtt_swd_init()`（桥的自愈路径）也对症。

### 1.6 修法

**探针固件（本轮已实施，见 §三）**：验收读失败 → 走桥的自愈路径
`rtt_bridge_link_recover()`（能降就降一档 + 清 sticky + 重新初始化）→ **再读一次**，仍失败才回 -4。

**零改动缓解**：把这条流程的 scope 档位设成 **45 MHz**（0/301 失败），代价是采样率上限约 -25%
（单字快路径 1.55 µs @60M → ~2.07 µs @45M）。

---

## 二、`P2.rtt@45M 丢 17 字节`：不是丢数据

### 2.1 那个数字是"相位"，不是字节数

`akaLinkPro/script_test/rtt_probe_bridge.py` 的判据是**固定图案** `b"hello world!\n"`（13 B）：
从头对齐后逐条 `find()`，`i != pos` 就记一个 gap，`lost = Σ 正 gap`。

这段流是**周期 13 的重复**，所以一次断裂能报出来的值只在 **1~24** 之间 ——
**真丢 5 字节和真丢 5 MB，报出来是同一量级**。`lost=17` 只能说明"有一处断裂"，
不能说明丢了多少。实测例：

```
断裂@2036 gap=+20 前后 100 B：
 hex: 0a 68 65 6c 6c 6f 20 77 6f 72 6c 64 21 0a | 68 65 6c 6c 6f 20 77 6f 72 6c 64 21 0a | ...
 asc: .hello world!.hello world!.hello world!.hello world! world!.hello world!...
                                                        ↑ 这里少了 "hello"（5 B）
```
（另一轮少的是 `"!\n hello w"` 9 B；"字节差" 还有 20/33 的组合 —— 都是同一次断裂的相位表现。）

### 2.2 断裂位置暴露了它是"采集边界"现象

固定图案靶子上连跑 4 个窗口，**3 次出现断裂，位置全部是 offset ≈2036**（我自己的采集起点后
~2 KB），而不是随机分布在中段；同时探针计数器显示 **`drained == 主机收到`**（3/4 窗口差值恰好 0）、
`rd_err=0`。→ 探针把读到的东西一个字节不少地交给了主机。

### 2.3 换成带序号素材：262 MB，真丢 0 字节

固定图案无法给出字节数，所以造了**等长（13 B）带序号**靶子 `tools/target-firmware/stm32f103_rtt_seq`
（`"seq %07u\r\n"`，其余不变：BLOCK_IF_FIFO_FULL + 96 MHz + 32 KB 环），逐条对账：

| 条件 | 窗口 | 数据量 | **真丢** | 坏记录 | 异常 |
| --- | --- | --- | --- | --- | --- |
| 45 MHz | 3 × 15 s | 120 MB | **0 B** | 0 | 1 个窗口开头记录号**回退** 156 条（=2028 B 重复） |
| 60 MHz | 3 × 15 s | 142 MB | **0 B** | 1（接缝处截断一条） | 1 个窗口同样回退 155 条（2015 B） |
| 45 MHz | 2 × 6 s | 34 MB | **0 B** | 0 | 无 |

（其中 45 MHz 那个"回退"的原始打印：`跳变@2034: 序号 2013246→2013090（差 -156，字节差 20）`，
`回退=2028` —— **回退 = 重复**，不是丢失。）

同时把你们自己的脚本按原样复跑 3 次（COM5、5 s、45 MHz，speed 版靶子）：
**3/3 LOSSLESS、2515~2520 KB/s**，与 README 基线一致。

### 2.4 机制：会话收尾那次 RdOff 回写失败 → 目标侧落后 ≤2048 B → 下一会话重读

```c
/* rtt_poll_once()：先交付到 CDC 环，再推进目标侧 RdOff；写失败就记 pending，下轮幂等补写 */
chry_ringbuffer_write(&g_uartrx, s_stage, target);
s_drained += target;
uint32_t new_rd = (up.rd + target) % up.size;
if (rtt_write_word(s_up_addr + RTT_UP_RDOFF_OFF, new_rd) != 0) {
    s_write_err++;  s_rd_pending = new_rd;  s_rd_pending_valid = 1U;  return -1;
}
```

- 每个会话都会出现 **一次 `wr_err=1`**（你们脚本自己也打出来了：`after-start wr_err=0` →
  `after-drain wr_err=1`），这是链路上的瞬态，设计上靠幂等补写兜底；
- 但如果这次失败正好落在**会话收尾**（`RTT ACT_STOP` 之后没人再 poll），pending 就没机会补；
  而 `rtt_bridge_start()` 里有一行 `s_rd_pending_valid = 0U;` —— **把它丢了**；
- 于是目标侧 RdOff 停在"少 ≤2048 B"的位置，**下一次会话开头把这段重读一遍** = 重复前缀
  （≤ 一个 `RTT_MAX_DRAIN`=2048 B 的块）。

这个方向是**安全的**（宁可重读不可丢），但会让"字节级对账"的消费者看到重复，
并且经 checker 的相位口径一翻译，就成了 `lost=17`。

### 2.5 建议

**探针固件（本轮已实施，见 §三）**：把 pending 的 RdOff 带到下一次会话**开头补写**（RdOff 是绝对值、
重写幂等），补完再开始搬数据 → 重读消失。

**测量侧（akaLinkPro 的 P2，建议）**：
1. 判据换成**带序号素材**逐条对账（或加目标侧 `g_bytes` 交叉对账），现在的 `LOSSLESS` 是相位级的；
2. 每个窗口**丢弃开头 4 KB**，避开会话边界的在飞/残留数据。

> 附带一条自省：自己写的吞吐脚本如果把预热/收尾的字节算进分子、却只除窗口时长，
> 数字会虚高 5~8% —— 别拿它跟 README 的 2516 KB/s 基线比。

---

## 三、修复与验收（已落地）

### 3.1 探针固件改动（akaLinkPro `bbb86a8`，4 文件 +116/-13）

| 问题 | 改动 |
| --- | --- |
| ① `rc=-4` | `rtt_bridge.c` 新增导出 `rtt_bridge_link_recover()`：SWD 侧"能降就降一档 + 清 sticky + **重新初始化**（含 20 MHz 斜坡）"，RISC-V 侧重开 TAP/DM；桥自己的自愈 `rtt_link_recover()` 改为它的包装（最低档也照样重 init）。`scope_sampler.c` 的 AP 验收读失败时调它再读一次，仍失败才回 -4；恢复成功后把 `s_clock_hz` 同步成**实际生效**档位（降过档要如实上报）。 |
| ② 会话边界 ≤2 KB 重读 | HID 的 `RTT_ACT_STOP` 与 scope 的 `START` 改成 `rtt_bridge_request_stop()` —— **停桥排队到主循环**（USB 中断里不能碰 SWD）。主循环服务时先 `rtt_bridge_flush_pending_rd()`：把没落地的 RdOff 补写回去（幂等，最多 4 次 + 清 sticky，失败就放弃）。**重启（START）前若会话还活着也补一笔**。另外 `rtt_bridge_note_dap_activity()` 里把 pending 作废 —— 主机碰过 DAP 后目标可能刚被复位/重烧，把旧位置写进去会让采样位置凭空前进（真丢数据），宁可放弃补写。 |

### 3.2 验收数据

| 项 | 改前 | 改后 |
| --- | --- | --- |
| scope START @60 MHz × 200 | **31 失败** | **0 失败**（其中 8 次走了恢复路径，状态字如实报 45 MHz） |
| `make regression-swd`（akaLinkPro） | PASS 3/3（P2 偶发 lost） | **PASS fails=0，64.4 s**：P1 八档通过；P2 20/45/60M = **1389 / 2512 / 2977 KB/s 全部 LOSSLESS**（lost=dup=rd_err=wr_err=0）；P3 bench/run 全过（吞吐与改前基线一致，无回归） |
| 带序号靶子 @45 MHz × 6 窗口（132 MB） | 每会话 1 次 `wr_err`；约 1/3 窗口开头出现 ≤2 KB **重读**前缀 | **真丢 0 B、跳变 0、回退 0、`wr_err` 0**；`drained − 主机 = 0±2048`（在飞边界） |
| `make full_flow_f103ze`（本仓端到端） | 曾因偶发 -4 需要页面级重试 | **绿**：campaign 两轮（转发 2.90/2.90 MB/s、J-Scope 50 kHz 零 USB 丢、**页面级重试未触发**）、调试压力 77/0 |

### 3.3 两条操作注意（都是这轮踩出来的）

1. **HID-only 测 scope 会把探针的包缓冲池耗光**：`--mode=scope/disc/bench/recover` 会让采样器往
   USB 0x83 推 DEF/DATA 包，而本工具**不读**那条端点 —— 8 个包缓冲一直"在飞"回不来，
   之后点 scope「标定真实速率」会稳定报 `err=-5`（没有空闲包缓冲）。
   恢复：**重烧探针**（RAM 重来）或让页面/脚本正常读一次 0x83。跑完这类模式建议重烧。
2. **RISC-V 侧未在本轮验证**：本机只挂了 F103ZE，改动里 RISC-V 分支保持与原实现逐语句等价
   （只是把静态函数提成导出），但要真正确认还得在 HPM6800EVK 上跑
   `make regression-riscv`（那套要求"刚烧过的探针"，sbastat 计数是开机累计的）。

### 3.4 给 akaLinkPro P2 的建议（还没做，随时可做）

判据本身仍是相位级的；仓库里现在有现成的**同长（13 B）带序号靶子**
`tools/target-firmware/stm32f103_rtt_seq`，搬过去把 `rtt_probe_bridge.py` 的
`PATTERN` 检查换成"逐条对账序号"即可（跳号 = 真丢 ×13 字节、回退 = 重复、解析不出 = 写坏），
再顺手丢弃每个窗口开头 4 KB 就彻底不受边界现象影响。

---

## 四、复现命令

```powershell
# 探针诊断（绕开页面，HID 直驱）
python tools/selftest/probe-hid-diag.py --mode=scope   --iters=200 --clk=60     # 量 -4 失败率
python tools/selftest/probe-hid-diag.py --mode=disc    --iters=40  --clk=60     # 判定失败在哪一级
python tools/selftest/probe-hid-diag.py --mode=bench   --iters=60  --clk=60     # 重试能不能救
python tools/selftest/probe-hid-diag.py --mode=recover --iters=120 --clk=60     # 瞬态持续多久
python tools/selftest/probe-hid-diag.py --mode=bridge  --iters=100 --clk=60     # 只起桥（对照）

# 字节级完整性（需要带序号靶子，见该目录 README）
python tools/selftest/probe-hid-diag.py --mode=loss --seq --clk=45 --iters=3 --window=15

# 他们自己的 P2（45 MHz，5 s）
python E:\Share\github\akaLinkPro\script_test\rtt_probe_bridge.py COM5 5 36000 45
```

探针符号地址（PEEK 用，改固件后要重新取）：

```powershell
riscv32-unknown-elf-nm -S --defined-only akaLinkPro_App.elf | Select-String 's_swd_ready'
# 归属看 .map：.sbss.s_swd_ready -> src/rtt/rtt_bridge.c.obj 是桥那份
```
