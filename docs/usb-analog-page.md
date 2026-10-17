# USB→ADC / DAC 页面（2026-10-05，待板测）

入口：`index.html#analog`，复用工具箱的探针资源管理和 HID 会话。

## 已实现：ADC

- 单次、有限次数和连续采集；周期由 probe GPTMR1 通道 1 驱动，网页只取结果。
- 输出位宽 8/10/12/16 bit。硬件保持 16 bit，较低位宽截取高位；这是输出量化，**没有实现硬件转换分辨率切换**。避免改变 akaLinkPro 共用的 VREF 检测配置。
- 目标速率 1/60–1000 Sa/s，周期四舍五入为整数毫秒。1000 Sa/s 是可请求的调度上限，不是 HID 持续吞吐保证。缓冲满后明确停止；高采样率必须板测。
- EVKLite：PB11 / ADC0.3，复用 SPI2 引脚。采样期间固件禁止 SPI 重配；ADC 开始时要求 SPI 已关闭，网页协调器释放 SPI 会话。
- akaLinkPro：PB10 / ADC0.2，**现有 VREF 的 10k/10k 分压输入**，换算乘二。不是新开放的通用 ADC 引脚，不能把 LED、电源控制或调试脚当 ADC 输入。
- ADC 内部满量程参考默认 3.3 V，可输入实测参考值作电压换算；这不改变硬件参考，也不扩大允许输入范围。
- 实时曲线、probe 时间戳、实测速率、跳过数、CSV。保留最近 10000 条供导出，曲线显示最近 512 条。
- Stop 失败沿用周期引擎的占用保留和重试规则，不假报停止成功。
- 没有新增主循环轮询或常驻定时中断。RTT/J-Scope 活跃时辅助采集让路。
- EVK ADC 初始化只开 ADC 时钟组/配置 ADC 时钟源，**没有调用会修改 CPU 时钟的 board init_adc0_bus_clock()**；单次采集临时切 PB11 到模拟模式，完成后恢复 FUNC_CTL。

## 已实现：DAC 波形数据；尚未实现：物理输出

官方 HPM5300 数据手册的型号资源表明确 HPM5301 为 1×16 bit ADC、无通用 DAC：
https://www.hpmicro.com/Public/Uploads/uploadfile/files/20250205/HPM5300DSV011.pdf

页面提供正弦、方波、三角、上/下锯齿、脉冲、直流和伪随机噪声的预览与 CSV。
可调更新率、波形频率、峰值幅度、offset、满量程、量化位宽、脉冲占空比和预览点数。
幅度是峰值，Vpp=2×幅度；越过 0–满量程时拒绝生成，不静默削顶。周期波形每周期至少 8 点。
CSV 只是数据文件，不代表硬件已输出，也不是已验证的循环 LUT；非整数周期的表直接循环会有接缝。

当前按 ADC 可用、DAC 预留推进。DAC 网页协议已完成：能力查询、配置、表上传、启动、停止和状态。
0x38 action 1 返回版本化 DAC1 能力；当前 HPM5301 宣告 0 通道，其他 DAC 动作回 UNSUPPORTED。
未来支持 DAC 的 HPM 型号接入驱动并广告能力后，网页按通道数、位宽、满量程、速率和表长启用输出，
不在网页硬编码芯片型号。详细 ABI 见 [usb-analog-dac-hid.md](usb-analog-dac-hid.md)。

当前不会启动任何 DAC 时钟、DMA、定时器或额外主循环轮询。
未来驱动负责 probe 端播放；网页不逐点发 USB。周期输出使用完整一周期 LUT，更新率若被硬件量化，
界面显示实际频率。停止等待在飞 START 和硬件 cleanup，失败保留占用供重试。

## 固件契约

- HID 0x38，action 0（CAPS），请求标准 req[1]=2。
- 响应长度 res[1]=20，res[4..7] u32 状态（0 成功，1 参数错误）。
- res[8..19]：ANA1 ASCII，channel:u8、native_bits:u8=16、input_gain:u8、physical_dac:u8=0、reference_mv:u16、max_requested_rate:u16=1000。
- HID 0x37 周期程序扩展 bus/kind=4（ADC）；payload 为 channel:u8、output_bits:u8。结果为右对齐 u16 LE 码值。
- 0x37 原 SPI/I²C wire 格式、BPT1 CAPS 和既有行为保持兼容；ADC 能力通过 0x38 action 0 单独识别；DAC 通过 action 1 独立协商。

## 验证与发布门槛

```sh
node tools/selftest/analog.test.mjs
node tools/selftest/bus-periodic.test.mjs
make test
```

已有虚拟时间生产 C/JS 跨仓库测试增加 ADC 路径（单次布局、三拍自主采集、时间戳和码值换算）。
新增数学测试覆盖预置波形、上下限、过采样点数要求、种子可重复噪声、采样周期量化和 CSV。
固件两板型 ADC 模块对官方 SDK 头文件做主机语法检查。

发布前仍需完整固件编译/链接和板测：DC 零点/半量程/满量程，ADC 参考与输入衰减，采样节拍，SPI/ADC 模式切换、停止/USB 复位，RTT/J-Scope 旧→新→旧对比。
