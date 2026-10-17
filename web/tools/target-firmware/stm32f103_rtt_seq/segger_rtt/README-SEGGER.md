# bsp/segger_rtt —— SEGGER RTT 官方源码（原样冻结）

> **这几个文件来自 [SEGGERMicro/RTT](https://github.com/SEGGERMicro/RTT)，一个字都没改**
> （唯一的例外是 `SEGGER_RTT_Conf.h` —— 那是 SEGGER 留给用户改的配置头）。
> 取自 commit `4d8feab`（2026-09-26 克隆）。

| 文件 | 出处 | 说明 |
|---|---|---|
| `SEGGER_RTT.c` / `SEGGER_RTT.h` / `SEGGER_RTT_ConfDefaults.h` | 上游 `RTT/` | RTT 引擎本体（不动） |
| `SEGGER_RTT_printf.c` | 上游 `RTT/` | `SEGGER_RTT_printf()` 系列 |
| `SEGGER_RTT_Conf.h` | 上游 `Config/` 的模板 + **本工程的配置** | 缓冲大小 4096/256、非阻塞模式、用 RT-Thread 关中断做锁 —— 每项都在文件里写了理由 |

**上游建议"不要改动源码"**（原文：*SEGGER strongly recommends to not make any changes to or modify
the source code of this software in order to stay compatible with the RTT protocol and J-Link*），
这里遵守：所有适配都放在 `SEGGER_RTT_Conf.h`（官方配置点）和 `bsp/drv_rtt.c`（我们自己的胶水）里。

`bsp/drv_rtt.c` 做三件事：
1. 把控制台输出镜像进 RTT（挂在 `drv_usj.c` 的 `s31_usj_put_bytes()` 上 —— 那是所有输出的必经之路）；
2. 一个低优先级线程把 RTT 下行缓冲的输入注入 shell 的 RX 环（`s31_usj_rx_inject()`）；
3. 一条 `rtt` msh 命令看状态。

主机端见 `tools/rtt.py`（自己实现了 RTT 协议，因为这份 OpenOCD 只编了 `rtt server`）。

## 许可

```
Copyright (c) 2026 SEGGER Microcontroller GmbH All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following condition is met:

1. Redistributions of source code must retain the above copyright notice, this
   condition and the following disclaimer.
```

（完整免责声明见上游 `LICENSE.md`；每个源文件头部都保留着 SEGGER 的版权头。）
