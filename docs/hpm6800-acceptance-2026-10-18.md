# HPM6800EVK 全流程验收（2026-10-18，在裸克隆 `E:\sp-verify` 里跑）

## 结论

| 步骤 | 结果 |
|---|---|
| `board-check-6800evk` | ✅ PASS（TAP IDCODE `0x1000563d` · 8 触发器 · PC→`engine.c:123 engine_rec_ack`） |
| `flash-dbgstress-6800evk` | ✅ PASS（4.4~4.8 s，spec ≤12 s） |
| `test-dbg-riscv` | ✅ **53/0**（修复 `resumeack` guard 之后；修前卡在单步） |
| `hw-campaign-hpm` | ⚠️ 烧录 PASS / 错位读 0 PASS；**RTT Viewer 45.6~45.9 KB/s < spec 56.5** → FAIL |
| `hw-random-flow-6800evk` | ⚠️ 同一 Viewer 判 FAIL；另有一个 harness 陈旧 id 崩溃（已修） |

**老库依赖：无。** 以上全部在只含 git 跟踪内容的裸克隆里完成；
HPM 资产全在仓库内（`hpm6800evk_*` 靶子含 `fw.elf`、`hpm_flash_algo`、`app/flash/hpm`、`app/dbg/riscv.js`）。
唯一外部依赖是 HPM SDK（`E:\sdk_env_v1.11.0`），属环境不属三仓。

## 问题 ①：RISC-V 单步卡死 —— **已修复（`7e99739`）**

现象：`test-dbg-riscv` 在 `b model.c:123` 之后报
`页面异常：Error: 上次运行应答尚未清除，未发起新的单步`（`app/dbg/riscv.js:611`）。

根因：`00b34d0`（**2026-10-16**，"第二轮审查修复 F01–F13"）引入的 guard —— 写 `dmcontrol`（不带 resumereq）
后最多等 **400 ms** 让 `dmstatus.resumeack[17:16]` 清零，清不掉就抛错。
但这条 **legacy(0.11) DM 的 resumeack 不会因 hart 停下自动清零**（实测恒为 1），
而 `_ensureHalted()` 已现场核过 hart 真的停着 → 残留 bit 是良性的。

修法：给 120 ms 尽量自然清；清不掉只记一条 warn、**继续单步**。
**回归来源**：该 guard 晚于 2026-10-04 的 HPM 基线（当时 `test-dbg-riscv 57/0`），
属工具箱自身引入，**与三仓合并无关**（合并是逐字节搬运，D-6 可查）。

## 问题 ②：RTT Viewer（RISC-V）慢 —— **已量化，未定因**

18 秒逐秒采样（狂发固件 + 控制块 `0x1240000`）：

| 指标 | 实测 | 基线（2026-10-04） |
|---|---|---|
| 每次轮询搬运 | **恒定 32,767 B（≈32 KB）** | ≈15.5 KB |
| 轮询率 | 1 Hz / 2 Hz 交替 → **平均 1.35 Hz** | 3.6 Hz |
| **真实吞吐** | **43.3 KB/s** | 55.9 KB/s |
| 页面显示 | 64.0 KB/s（**瞬时值**，非均值） | — |
| **每条 SBA 批量命令成本** | **1.08 ms** | 0.82 ms（+32%） |

⇒ harness 报的 45.6/45.9 KB/s 是诚实的墙钟均值；页面 `#r-rate` 的 64.0 是瞬时值，不能当判据。
真正的退化是**每字节成本 +32%**。

两个候选（待 A/B）：
- **(a) 页面侧**：`MAX_READ_PER_POLL` 变大（32 KB vs 15.5 KB）后，批量读偶发退化成逐字读；
- **(b) 探针固件**：当前固件 `3214872` 比基线版（`bbb86a8`，2026-10-04）新，含 evklite 双 nRESET / drive-mode /
  SWO 调度优化等提交，JTAG 时序可能变化。老仓 `E:\akaLinkPro-bbb86a8`（worktree）已备好，可编一版做 A/B。

顺带记一条**手工调 API 的坑**：`hid.start({addr})` 在 RISC-V 上直接调会 `startRc=-2` ——
必须先做页面那套 `SWD+JTAG` 预处理（`HID 0x02` 切 output_mode + `HID 0x33 action 0` 放开 TAP），
所以"转发走探针引擎"的 A/B 必须经页面走，不能裸调 HID。

## 问题 ③：harness 陈旧 id（**已修复 `c334a72`**）

`hw-campaign-hpm.mjs:585` 点 `#h-reconnect`，而页面早改名 `#h-refresh` →
`null.click()` 抛 `TypeError` 让整轮 `hw-random-flow-6800evk` 中断。
与 `hw-campaign.mjs` 那次（`2b016c2`）同类，照同样写法修（两个都试 + 带 catch）。

## 复现命令（裸克隆里）

```
git clone E:\superlinkPRO E:\sp-verify
cd E:\sp-verify\web && make page-prep
make full_flow_6800evk            # board-check → hw-campaign → flash-dbgstress → test-dbg-riscv → random-flow
make test-dbg-riscv FLOW_LOCAL=--local
node tools/selftest/flash-elf.mjs --chip=hpm6800evk --elf=/tools/target-firmware/hpm6800evk_rtt_flood/fw.elf
node tmp/riscv-viewer-rate.mjs 18  # 逐秒采样 Viewer（脚本在工作仓 tmp/，未入库）
```
