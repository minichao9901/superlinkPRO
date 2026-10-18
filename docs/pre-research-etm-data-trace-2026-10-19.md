# ETM data trace 硬件能力预研（STM32H743，2026-10-19）

## 结论

**这块 STM32H743 的 ETM 不实现 data trace。** 因此无法通过“打开 ETM data trace”来重建外设寄存器的读写地址与值；不是配置问题，也不是解码器问题，是硬件能力位就没有。

## 怎么测的

- 目标板：STM32H743（DEV_ID `0x00000450`），板子在线。
- 探针：akaLinkPro（`8F538633…`）。
- 通过 ETM 页面连接探针后，直接读 ETM 寄存器（ETM base `0xE0041000`）：
  - `TRCIDR0`（`0x1E0`）
  - `TRCIDR1`（`0x1E4`）
  - `TRCIDR2`（`0x1E8`）
  - `TRCCONFIGR`（`0x010`）

读法：

```js
// 在 ETM 页面控制台（连接探针后）
const t = window.__etm.target;
const idr0 = await t.read(0xE00411E0);   // TRCIDR0
const idr1 = await t.read(0xE00411E4);   // TRCIDR1
const config = await t.read(0xE0041010); // TRCCONFIGR
```

## 读数

| 寄存器 | 值 |
|---|---|
| TRCIDR0 | `0x080006E1` |
| TRCIDR1 | `0x4100F401` |
| TRCIDR2 | `0x00000004` |
| TRCCONFIGR | `0x00000001` |

## 判定依据

OpenCSD 的 ETMv4 配置类对 data trace 能力的判定是（`decoder/include/opencsd/etmv4/trc_cmp_cfg_etmv4.h`）：

```cpp
inline const bool EtmV4Config::hasDataTrace() const
{
    return (bool)((m_cfg.reg_idr0 & 0x18) == 0x18);
}
```

即 `TRCIDR0` 的 **bits[4:3] 必须同时为 1** 才表示实现 data trace。

实测 `TRCIDR0 = 0x080006E1`，`0x08.. & 0x18 = 0`（bits[4:3] = `00`）→ **不支持 data trace**。

当前 `TRCCONFIGR = 0x1`，data trace 相关位也是关闭的（与 `etm/firmware/stm32h7b0_etm/src/trace.c` 的 `/* No ... data */` 一致）。

补充：OpenCSD 1.8.3 的 README 也把 “ETMv4 data trace - packet processing and decode” 列在 **Support to be added**，所以即使某颗芯片实现了 data trace，本仓当前解码链也没有现成支持。

## 这意味着什么

- 靠 ETM 记录 **无法**恢复寄存器的写入值；只能得到指令流、调用路径、源码行和周期。
- 想精确看到 USB 等外设的寄存器读写：
  1. **固件寄存器写日志**（推荐）：在测试固件里对目标外设地址范围记录 `{cycle, pc, addr, value, rw}` 到 RAM 环形缓冲，事后 SWD dump；ETM 负责时序对齐。
  2. 调试器 watchpoint/单步：能拿到值，但会停 CPU、破坏 USB 时序，只适合初始化阶段。
  3. 纯指令流 + ELF 静态分析：能给出访问序列和静态可算的地址/常量值，运行时值仍是 `?`。

## 原始证据

- 测试日期：2026-10-19 00:2x
- 工具：`etm/server.py` + 浏览器 ETM 页面（WebUSB 连接探针）
- 目标状态：运行中（halted=false）；读 ETM 寄存器不要求停机。
