# 主循环空闲服务门控

目标：非运行服务没有控制/清理请求时，不进入其 poll 函数；保护 RTT/JScope
高速采样循环。主循环顺序、运行中批量预算、SWD/JTAG 与 USB 数据路径不变。

`service_gate_t` 是原有状态标志的集中存储，不是额外的镜像 pending mask。
各生产者继续写独立的 volatile 字节；主循环用内联函数读取对齐的 32 位字。
最多四个状态的服务检查一个字，RTT 与 SPI/ADC 检查两个字；标志放普通 BSS，
SPI 的大缓冲及其他桥状态仍留在原 AHB SRAM。无需在门控检查时关中断，
无需每圈进入 XIP 中的服务函数。该 union 字节写/字读取依赖 GCC 的 union
成员重解释语义；不是跨平台并发框架。

| 服务 | 必须唤醒的状态 |
| --- | --- |
| 参数保存 | 待保存 |
| RISC-V | 待执行命令 |
| RTT | 运行、START、STOP、raw DAP、benchmark |
| JScope | 运行、START、USB reset、benchmark |
| SPI/ADC | SPI enable、RESET、USB reset、ABORT、排空读取、ADC ownership |

I2C/周期任务原有内联请求判断、CDC/UART 的使能判断继续使用。
ADC 的 START/END/reset/close 在 ADC ownership 存续期间由 SPI 服务入口处理，
即使采集已停止但未释放，仍可排空及清理。SPI disabled 时的 DRAIN/reset
也继续服务；不能只使用 enabled 作为门控。

门控只读，不清位。中断若在判断后到达，工作留在原标志中，下一轮处理；
不增加容易丢唤醒的集中读改写掩码。原服务内部判断保留，以支持直接调用
和处理到达时序。原有请求队列/协议行为不在此次改造范围内。

验证：生产 inline gate 的逐状态/迟到事件/主循环接线检查，及完整 host
回归、USB 描述符检查。尚未完成目标链接与板测；不宣称具体速度增益。
板测比较相同配置下旧→新→旧的 RTT 吞吐、JScope Probe/端到端速率，
并验证空闲状态下 START、STOP、reset、SPI DRAIN 和 ADC→SPI 切换。
