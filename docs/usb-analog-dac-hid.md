# ADC 可用 / DAC 预留：HID 接入契约 v1

当前 HPM5301 实现 ADC，DAC 仅实现能力查询和 UNSUPPORTED 响应。
本协议为未来支持 DAC 的 HPM 固件预留，不代表已有物理 DAC 驱动或完成硬件验证。

## 公共封装

复用 HID CMD **0x38**，不新增端点。ADC action 0 / ANA1 完全保持兼容。
DAC action 1–8。请求 `req[1]` 为 CMD+data 长度，data=`action,args`；完整 report 包含 ID 的 64 B。
响应 `res[1]` 为含 ID 的总长，res[2]=0x38、res[3]=action、res[4..7]=u32 LE 返回码、res[8..]=data。
网页 xfer 已移除 ID，对应状态位于 r[3..6]、data 从 r[7] 开始。所有多字节整数均小端。
返回码：0 OK、1 RANGE、2 BUSY、3 STATE、4 UNSUPPORTED。
未知 action 返回 RANGE；不支持 DAC 的当前固件对合法 DAC 动作返回 UNSUPPORTED。

## 动作表

下列参数不含 action。channel 从 0 编号；generation 是非零 u32 任务代数。

| action | 请求参数 | OK 响应 data |
| --- | --- | --- |
| 1 CAPS | 无 | DAC1 ASCII、version:u8=1、channels:u8、bits:u8、features:u8、max_rate:u32、max_points:u16、full_scale_mv:u16（共 16 B） |
| 2 CONFIG | channel:u8、bits:u8、rate:u32、idle_code:u16 | channel:u8、bits:u8、actual_rate:u32、idle_code:u16（8 B） |
| 3 BEGIN | channel:u8、points:u16 | generation:u32（4 B） |
| 4 WRITE | channel:u8、generation:u32、offset:u16、n:u8、code[n]:u16 | next_offset:u16（2 B） |
| 5 START | channel:u8、generation:u32、cycles:u32 | actual_rate:u32（4 B） |
| 6 STOP | channel:u8、generation:u32 | 同 STATUS（20 B） |
| 7 STATUS | channel:u8 | channel:u8、running:u8、bits:u8、flags:u8、generation:u32、loaded_points:u16、position:u16、completed_cycles:u32、actual_rate:u32（20 B） |
| 8 GET_CONFIG | channel:u8 | 同 CONFIG（8 B） |

能力 features bit0=循环表播放，其余位预留且必须 0。最多 8 通道，原生位宽为 8/10/12/16 中一个。
0 通道表示当前不支持；HPM5301 的原生位宽、features 和全部上限字段均 0，不分配 DAC 状态或表内存。
网页遇到旧 ADC 固件对 action 1 回 RANGE 时，保留 ADC、禁用 DAC；通信失败或畸形能力不被吞掉。

支持 DAC 时，max_rate 为可配置更新率上限（Sa/s，1–10000000），max_points 为表容量（8–65535），
full_scale_mv 为所有广告通道共用的码值满量程。不同通道位宽/参考不同的设备需扩展协议版本。
CONFIG 的实际 rate 可因时钟分频向下量化，必须为正数且不高于请求值；网页以实际 rate 计算波形频率。
当前网页 STOP 后输出码固定为 idle_code=0。

## 驱动状态约束

- CONFIG 在该通道运行或清理时回 BUSY，不得重配全芯片时钟或影响其他功能。
- BEGIN 只准备暂存表、不启动输出；生成非零 generation。重复 BEGIN 只能替换未运行的暂存任务。
- WRITE 每包 1–25 点，按 offset 顺序提交；generation、通道、每个码值和总长度必须核验。
  错误包不得部分写入。网页核对 next_offset 后才继续。
- START 必须在全部 points 到齐、generation 匹配后才启动；cycles=0 连续循环，其他值为完整表周期数。
  全部上传完成前不得边收 USB 边播放。实际 rate 必须与 CONFIG 应答一致。
- STATUS flags bit0=cleanup 待完成，其他位保留 0；停止态仍保留该任务 generation 供网页确认。
  running 为 0/1。position 为正在播放的表下标；loaded_points 为完整已接收点数。
- STOP 仅操作匹配 generation 的通道，旧任务命令回 STATE，不能停止新任务。
  停止新触发、结束/取消 DMA 后写 idle_code；cleanup 完成后才表示物理输出已停。
- 网页 STOP 先等正在上传/启动的调用结束，避免晚到 START；STOP/STATUS 最多等待 3 秒确认
  running=0 且 cleanup=0。失败保留会话占用，允许用户重试。
- USB RESET/DISCONNECTED 必须作废 generation，停止播放并恢复约定闲置状态。
- 未来采用外设时钟/定时触发和 DMA 在 probe 端播放。未启用 DAC 时没有常驻 tick、DMA 中断或逐圈任务扫描。
  不得改变 RTT/J-Scope 使用的时钟、中断优先级和 SWD/JTAG 时序；接入新型号后仍需性能比较板测。

## 波形与页面

DAC 参数界面保留。当前设备 0 通道时禁用“启动输出 / 停止输出 / 输出状态”，预览/CSV 可用。
未来能力具备 TABLE 且通道非零时，自动启用，不按 HPM 型号识别。ADC 与 DAC 暂时互斥，共用会话占用与关闭逻辑。

幅度为峰值，offset±amplitude 必须在 0–full_scale 内，越界拒绝。
正弦/方波/三角/锯齿/脉冲生成完整一个周期，points=round(rate/frequency)，至少 8 点，不能超过表容量。
实际频率=actual_rate/points，避免把任意预览窗口直接循环产生接缝。
直流使用 8 点常数表；伪随机噪声使用指定点数的循环表，重复播放，不是无限随机序列。
预览点数仅定义显示窗口；输出周期表按频率单独计算。

## 回归

```sh
node tools/selftest/analog.test.mjs
node tools/selftest/dac-protocol.test.mjs
# Python/GCC 在 PATH，配套 firmware 位于相邻目录或设置 PROBE_FIRMWARE_REPO：
node tools/selftest/analog-wire.test.mjs
node tools/selftest/bus-periodic.test.mjs
```

生产 C/JS HID 测试验证 HPM5301 零通道和每个动作 UNSUPPORTED、坏 WRITE 拒绝与 ADC CAPS 兼容。
虚拟未来固件验证分片上传、应答身份/长度、完整周期表、上传和 START 期间取消、STOP 失败重试和 cleanup 确认。
这些测试不代替未来实际 DAC 驱动、模拟输出性能或页面浏览器验收。
