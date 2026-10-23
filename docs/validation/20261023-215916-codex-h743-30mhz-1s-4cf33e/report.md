# H743 TRACECLK30 MHz 单独复测（2026-10-23）

结论：**先前的TPIU帧内同步错误本次没有重现，但尚未取得完整1 s合格记录。** 500 MSa/s请求1 s，设备实际只录到926.76555 ms；这份记录严格帧检查通过并成功解码。改200 MSa/s后也只录到936.40239 ms，且有265个包问题，不能通过。不能将任一条记录称作1 s验收通过。

- Run-ID: `20261023-215916-codex-h743-30mhz-1s-4cf33e`
- Tested-Revision/Base: `b14724e89d0b073bc980f6d786b63a8e078635ea`；源码未修改；仅本机独立验收服务进程允许请求1 s。
- Validation-Result / Hardware-Test: `partial`（请求时长不满足、200 MSa/s包问题）
- 原始证据：`E:/superlinkPRO/docs/validation/20261023-215916-codex-h743-30mhz-1s-4cf33e`
- 原始波形：`C:/Users/Administrator/.codex/validation/20261023-215916-codex-h743-30mhz-1s-4cf33e/records/`

## 条件与实际结果

与前轮通过的20 MHz设置保持CPU60 MHz、HSE25 MHz、banner20 ms、echo约4 ms、GPIO speed2、Trace ID2、CONFIGR0x1011（RS/CC开、BB关）、threshold1.65 V、fraction0.25、软件滤波4 ns、CH0时钟/CH1～4数据。仅构建PLL1R分频8改为物理TRACECLK30 MHz。实烧ELF与Flash全部31856字节读回一致，目标寄存器回读物理TRACECLK=30000000 Hz，实际波形均值约30.00076 MHz。

| 项目 | 500 MSa/s主复测 | 200 MSa/s对照 |
|---|---:|---:|
| 请求时长 | 1000 ms | 1000 ms |
| 实际时间窗 | 0～926.76555 ms | 0～936.40239 ms |
| 实际深度 | 463,382,775 | 187,280,478 |
| KVDAT体积 | 433,830,237字节 | 438,360,371字节 |
| 恢复指令数 | 17,021,550 | 16,640,491 |
| 完整处理（不含采集/回传/保存） | 15.223s | 15.299s |
| TPIU结构错误/帧内FSYNC | 0 / 0 | 0 / 0 |
| 包问题 | 1（仅末尾1字节INCOMPLETE_EOT） | 265（不通过） |
| 溢出 / 未处理 / 字节遗漏 | 0 / 0 / 0 | 0 / 0 / 0 |
| banner / echo trace调用 | 46 / 206 | 54 / 204 |

500 MSa/s尾包位于offset2015487、长度1，恰好抵达2015488字节输入末端，明确保留为截断边界。没有放宽帧内FSYNC、丢帧或idle-resync检查。输入开头仍有693字节未同步和8127条条件执行未知，不宣称全路径无损。其clock_gaps为空，TPIU errors/mid_frame_fsyncs/dropped_partial_frame_bytes/idle_resyncs均0。

首个采集脚本日志中PASS只表示质量检查，不验证请求时长；审计KVDAT后发现长度不足，立即否定1 s通过。随后补充深度核验脚本第一次错误地从`/records/<sid>`解码响应读取actualDepth（该接口不返回session元数据），因此日志报undefined。实际时长以上述磁盘session.json、KVDAT sample_depth及physical一致结果为准；脚本错误不影响原始采样，原日志保留。两次采集均未手动stop，设备返回ACK/saved/no error，不能把请求seconds=1当成实采1 s。

## 原因判断与对照

本机KingstVIS 3.6.6官方安装目录的 `Resource/ug-en.pdf` 第37～38页明确说明：硬件存储配合压缩扩展深度，高速连续跳变时压缩收益下降，设定100M以上深度可能提前耗尽。摘取原页文字保存在`vendor-capture-depth-explanation.txt`。本次两档均在约0.93 s结束，**与压缩存储容量限制相符，是当前主要判断，尚未通过硬件内部容量计数证明**。不能承诺500MSa/s下这组信号能完整存1 s，也不能引用旧稀疏信号长窗记录代替。

200 MSa/s把采样点间隔由2 ns改成5 ns，不降低30 MHz信号的跳变次数，对边沿压缩容量未见有效改善；该记录出现265个包问题，本轮不进一步放低采样率或通过滤波掩盖错误。

本轮500 MSa/s的clock短脉冲明显少于前轮失败记录；没有改接线/全局设置的证据，不猜测先前失败已永久修好。单次通过不能证明稳定性。CPU业务条件相同，但此次有效记录只有926.77ms，不能与前轮完整1 s的14.2s直接作等长性能A/B。

独立Python有界参考前端再次读取同一500 MSa/s KVDAT，与C++完整TPIU字节、ETM字节、framing逐字段**全一致**；耗时21.52s（仅正确性对照，非产品性能）。报告`reference-500m.json`。

## 收尾

- 固件保留CPU60 / TRACECLK30 MHz的非阻塞RTT+周期banner版本，ELF SHA256 `9816bb57e9f11ec76a89bc7de208843d0484a587e5ce17f86c849c2d621f2c21`。
- 负载原始日志：1906发送 / 1906收到，缺失0；阶段心跳CRC错误/重复均0。进程结束后按原始TX/RX日志恢复摘要，不伪造未生成的正常结束摘要。
- 初次收尾JS遇本机临时USB字节服务已退出，未完成；随后独立pyocd重新连接、核对Flash、关闭ETM/ETF、TPIU状态2并恢复CPU，g_trace循环168741710→168846686，见`final-pyocd-health.json`。
- 最终CDC重新打开健康检查没有完成，已停止属于本轮的该进程；不宣称最终串口复测通过。此前采样过程的业务回显有原始日志和trace佐证。探针连接已关闭，目标CPU运行；源验收临时服务已退出。未发布或改产品默认上限。

## 文件与可复核入口

500 MSa/s记录：`capture-8f34ee629df74b6382a1cf6bae175756`，KVDAT SHA256 `b3e9b17988b5c2c9f1aa28e2a2e7cce60361f6bc22130658c116a58dc6b78dff`。

200 MSa/s记录：`capture-3d6cc5d8be714d35a447955130a336de`，KVDAT SHA256 `8d960a8f5722126bd3482493880e4f62fcfa70c04b9ec8d109a6345b0946d599`。

`retest-audit.json`记录完整原始/派生文件长度及SHA256、两份窗口、配置、质量、业务调用与分项耗时。实际入口：`flash-firmware30.py`、`run-monitored-v4.py trace30-1s 1 1`、`run-depth-checked.py trace30-1s-200m 1 1`（第二次设置SAMPLE_RATE=200000000）、`reference-500m.py`、`cleanup-target.py`。工作目录为`C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO`；不覆盖旧原始证据。临时metadata里的USB token不进入报告。
