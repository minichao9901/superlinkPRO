# F103ZE / Kingst ETMv3 解码验收

日期：2026-10-13。CPU 48 MHz，TRACECLK 24 MHz，4 位 DDR，Kingst MIPI16 500 MSa/s、门限 2.0 V。

## 已完成

`Kingst CSV → 双沿 DDR 字节流 → TPIU 解复用 → OpenCSD ETMv3 → Thumb 指令 → ELF/DWARF 源码` 已跑通。
新增记录可在本地页面记录列表中打开，复用执行流、Timeline、调用统计、火焰图、热点、包列表与可信区段。
页面的包标签跟随记录协议，显示 ETMv3 包；H7 原有 ETMv4 解码仍可用。

| 实测记录 | 时长 | ETM 包 | 指令路径记录 | 已执行指令 | SysTick 中断 | 包问题 / Overflow |
|---|---:|---:|---:|---:|---:|---:|
| 原 CDC 主循环 | 10 ms | 76,639 | 189,669 | 170,713 | 10 | 0 / 0 |
| 分支验收负载 | 2 ms | 27,011 | 52,159 | 49,310 | 2 | 0 / 0 |

两份记录所有指令地址均有 DWARF 源码映射。原始 CSV、KVDAT、ELF、源码快照、配置、JSON 解码结果均保留。
它们是限时采集窗口，包含未同步开头和不完整的 TPIU 尾帧；没有声称恢复窗口外的执行或整次程序启动。

## 路径验收

验收固件增加 `trace_workload_round()`，每轮八次交替调用 `trace_even()` / `trace_odd()`，每次再执行 `trace_mix()`，循环次数依次为 1～8。
真实 2 ms 记录核对通过：

- 129 个完整 round，八次交替叶函数调用顺序全部一致。
- 1,035 次可核对的 mix 调用；完整 round 的循环次数均为 1～8。
- 12,035 次直接分支/调用的下一条 PC 与目标或未成立时的顺序地址一致，其中 2,589 次为未成立分支。
- 37,527 次线性指令衔接一致。
- 28,033 字节 ETM 输入全部处理，包字节无重叠、无覆盖缺口，OpenCSD 最终错误码为 0。
- 明确保留 219 字节 ETM 未同步开头、167 字节 TPIU 同步前物理输入及 2 字节不完整尾帧。

验收脚本：`tools/validate_f103_capture.py`，结果：`records/f103ze-48-workload/f103-validation.json`。
这证明当前负载与采集窗口中的路径恢复符合已知控制流，不代表任何负载或任何频率都自动无损。

## 对齐问题的修正

此前 `tmp/f103ze_decode_align.py` 每次只选择上升沿或下降沿，再把相邻样本合成字节，漏掉了另一半 DDR 符号。
其“bit0=1 比例”评分也没有协议依据：正常 TPIU 数据字节与 ETM 数据都允许 bit0=0。

本次改用 `tools/trace_port.py` 在每个时钟沿之后取样，按真实 DDR 两个 nibble 合成字节，再用完整 TPIU 帧结构确认字节相位。
标准 TPIU FSYNC 是 `FF FF FF 7F`；ETMv3 A-Sync 是至少五个 `00` 后跟 `80`。二者属于不同层，不要用 `00×N + FF×N` 搜索替代。

2 ms 分支记录：2,947 个完整 TPIU 帧，170 个 FSYNC，结构错误 0，字节相位为 0（相位由每次采集起始边沿决定，不能固定所有捕获均为 0）。
采样位置 0.1、0.2、0.25、0.3 个半周期得到完全相同的 ETM 载荷 SHA256；选择 0.25，最小数据跳变距离约 3 ns。
相位扫描位于 `records/f103ze-48-workload/phase-validation.json`。中点附近会落到数据跳变区，不能用半周期中点作为固定采样点。

Kingst 的 KVDAT 快照显示 ch0～ch4 启用了 1 个采样点的毛刺滤波（500 MSa/s 下约 2 ns），报告保存了该设置。
TRACECLK 平均约 24.0014 MHz，解析器定义的异常半周期数为 0。

## 解码器配置与镜像证据

通过 OpenOCD 在采集后读取配置，并在结束时 `reset run` 恢复独立 Trace 固件：

| 寄存器 | 值 |
|---|---|
| ETMIDR | `0x4114f241`（ETMv3.4） |
| ETMCR | `0x910` |
| ETMCCER | `0x18800` |
| ETMTRACEIDR | `2` |

配置文件：`configs/f103ze-etmv3.json`。能力寄存器未启用的时间戳等能力不等同于当前数据流中有该信息。
当前记录没有逐指令时间戳，Timeline 和火焰图横轴仍为执行序号，不代表耗时。

原主循环 ELF SHA256：`d87a7037dd24711050a83e53d5e55f81fbd9d0edcef729eac012a74af89d59bd`，OpenOCD 比对 20,552 Flash 字节通过。
分支负载 ELF SHA256：`8f983634cdd5033f1b46d0e9c0ed96f0943325389ffc073f07ca8047bc4abc04`，烧录后比对 20,660 Flash 字节通过。

本机 OpenCSD 1.8.3 已包含 ETMv3 包处理和指令路径解码，无需另外手写一个基于 PC 猜测的解析器。
参考：[OpenCSD 官方仓库](https://github.com/Linaro/OpenCSD)、[OpenCSD 解码 API 说明](https://github.com/Linaro/OpenCSD/blob/master/decoder/docs/prog_guide/prog_guide_main.md)。

## 复现

当前目标板已烧录分支负载档：`firmware/stm32f103_cdc_trace/fw-ze-standalone-48-workload.elf`。
固件自行初始化 Trace，正常采集过程中无需 SWD 会话。

```powershell
# 从 E:\web-etm-trace\firmware\stm32f103_cdc_trace 构建可选负载档
pwsh -NoProfile -File .\build.ps1 -Chip ze -StandaloneEtm -CpuMHz 48 -TraceWorkload

# 从 E:\web-etm-trace 执行采集、解码与路径验收
python tools\la\kingst_la.py capture --rate 500000000 --time 0.002 --threshold 2.0 --channels 0 1 2 3 4 --out E:\web-etm-trace\captures\f103ze-new.csv --stats
python tools\decode_trace.py captures\f103ze-new.csv --elf firmware\stm32f103_cdc_trace\fw-ze-standalone-48-workload.elf --config configs\f103ze-etmv3.json --out decoded\f103ze-new
python tools\validate_f103_capture.py decoded\f103ze-new\decoded.json --elf firmware\stm32f103_cdc_trace\fw-ze-standalone-48-workload.elf
```

## 查看与交付文件

- 页面记录 `F103ZE · CPU 48 / Trace 24 MHz · 2 ms 分支负载`，ID `f103ze-48-workload`。
- 页面记录 `F103ZE · CPU 48 / Trace 24 MHz · 10 ms`，ID `f103ze-48-10ms`。
- 离线源码对照：`decoded/f103ze-workload/simplified.html`。
- 可重新解码的记录包：`decoded/f103ze-workload/f103ze-48-workload.etmrecord`，含原始 CSV、KVDAT、ELF、配置与源码快照。
- 完整结果：`decoded/f103ze-workload/decoded.json`；简化源码：`decoded/f103ze-workload/simplified.c`。

回归：57 项现有 Python 测试通过；H7 ETMv4 原始包流用更新后的 native 解码器复解，错误码为 0。

后续优先补 F103 的网页端目标配置与采集控制，目前新增能力主要是离线解码/浏览，网页 SWD 控制仍沿用原 H7 验收范围。
