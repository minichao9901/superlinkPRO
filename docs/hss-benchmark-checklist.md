# HSS 回家上板测速清单

本分支尚未接设备测速；离线通过不代表达到 400/500 kHz。构建、烧录需本机原有 HPM SDK
和 RISC-V 工具链。配套网页在 web-serial-rtt-tools 的 perf/hss-pipeline 分支。

先用原固件留一份基线，再烧本分支；目标 RAM 地址、SWD 时钟、线材、变量表、CDC 开关
保持一致。默认 `--set one` 地址是 STM32F103 测试靶子的 g_tick；其他靶子用
`--addr 0x实际地址` 覆盖。不要拿不可读地址测速。关闭其他调试程序/网页的设备会话。

## 依次测三个层次

1. M0 紧循环基准：`bench`，只用于判断 SWD/采样读取上限。
2. 完整采样器：`discard`，不认领 bulk 数据接口，测 probe scheduler + frame 成本。
3. 主机实收：网页 WebUSB 为主，Python `run` 为交叉核对。Python 仍是单线程同步 IN，
   自身可能先碰到吞吐上限；不能用它的结果代表 WebUSB 的上限。

在仓库根目录执行（Windows 可将 python3 换为 python）：

```sh
python3 script_test/scope_hss_test.py bench --set one --swd --clock 60000000 --period 3 --flags 0x21 --iters 2000
python3 script_test/scope_hss_test.py discard --set one --swd --clock 60000000 --period 3 --secs 5 --flags 0x21
python3 script_test/scope_hss_test.py discard --set one --swd --clock 60000000 --period 3 --secs 5 --flags 0xA1
python3 script_test/scope_hss_test.py discard --set one --swd --clock 60000000 --period 2.5 --secs 5 --flags 0xA1
python3 script_test/scope_hss_test.py run --set one --swd --clock 60000000 --period 2.5 --secs 5 --flags 0xA1 --readsize 4096
```

`0x21`=允许60MHz+暂停CDC；`0xA1` 再加短批次，discard 命令自动加 bit1。
不自行加入 DELAY0/NO_YIELD，避免同时改变多个因素。若要测它们，应单独记录。

## 周期扫描

| 周期 µs | ticks | 名义 kHz |
| --- | --- | --- |
| 3 | 72 | 333.33 |
| 2.75 | 66 | 363.64 |
| 2.5 | 60 | 400 |
| 2.25 | 54 | 444.44 |
| 2 | 48 | 500 |

每个周期分别测短批次关闭/开启，先5秒、候选稳定档再30秒，各跑3次。
记录 M0、探针快照 produced/s、scheduler skip、USB buffer exhaustion、SWD errors、
DAP yields、网页实收/s、seq缺口以及浏览器是否流畅。v2 时间戳179秒回绕，长采集额外测试
190秒以上回放/时间轴；单次脚本快照窗口应小于170秒，脚本对此有限制。

判断瓶颈时用同一周期和相同 flags：

- DISCARD 增速且 scheduler skip 减少：短批次改善了调度成本。
- DISCARD 很稳，网页 USB exhaustion 上升：优先查主机服务停顿/USB 交付。
- SWD errors 增加：先查时钟、接线和目标访问，不能当成主机吞吐问题。
- produced 正常、seq gap 少、页面卡：比较关闭原始包录制/触发后的表现，再做浏览器性能录制。
- 3 µs 的333 kHz主要是配置的节拍。不能用 M0/333 的比值推算框架固定开销。

探针速率用 action11 的 timer 和 u32 计数增量；主机速率用 USB 返回时刻的窗口。
启动、停止及 drain 不混入任何一个速率。两窗口边界有 HID/在飞包偏差，差值不能直接
当作精确丢失量。DISCARD 的 formatted bytes 不是 USB 发出的字节。

## 功能回归

检查单字值连续性（使用已知值/递增靶子；低频目标允许重复值）、124样本包边界、2变量
远地址、pack/cross、多类型、触发、原始包回放、旧v1包、新v2包、暂停后重启、拔插重连、
CDC停采自动恢复和并发DAP让路。对短批次还应检查其他桥的响应延迟。
如果短批次出现功能回归，取消网页开关/清除 flags bit7；小数周期可独立继续使用。

## 离线检查

```sh
python3 script_test/scope_host_test.py
python3 script_test/test_scope_hss_test.py
```

前者编译真实 scope_sampler C 配硬件替身，不验证 HPM 汇编或 MMIO 时序；后者检查
周期编码、能力位/拒绝、快照和计数回绕、收流窗口，以及 v2 解包。
