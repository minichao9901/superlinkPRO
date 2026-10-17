# WebUSB 控制写与 FAULT 修复

2026-10-11。用户手动点击“联动采集”后出现“调试口已有 FAULT／正在恢复；诊断未重新初始化目标”。用户确认没有预先点击“配置并暂停”或“复位至起点”。原日志仅提供末尾片段，所以不能用该片段证明第一次 FAULT 的具体访问地址。

## 已确认的问题

页面 target.write 调用通用 writeMem；该方法默认回读写入内容，不一致时再写一次。对于 AIRCR 写入键和 SYSRESETREQ、自清零控制位，以及 DHCSR 单步请求，这种普通内存校验不成立：有可能重复复位/单步。回读失败被底层捕获，但 _faulted 标记仍保留，后续 readMemDiagnostic 因此抛出截图中的提示。

target.write 的 verify=false 以前只关闭上层验证，没有关闭底层回读/重写。此前 pyOCD 验收适配器没有此自动验证逻辑，因此没有覆盖这个差异。

另一个已确认的软件问题是 finally 中的恢复/回读错误会替换原始操作错误，界面只能看到后续诊断提示。

## 修复

- 新增 writeMemDiagnostic：在原有传输锁内，只发送一次控制写，不自动回读、重写或重初始化。普通可回读寄存器仍由 target 层显式验证，自清零/键控寄存器通过状态位检查结果。
- 新的业务入口复位顺序：先停止并排空旧 trace，安装业务入口 FPB，暂时关闭复位向量捕获，然后只写一次 AIRCR.SYSRESETREQ；等待业务入口暂停，验证 PC，清理临时比较器。不会先停在 Reset_Handler 再重新初始化 SWD。
- withCleanup 保留最初的操作错误，后续清理失败单独记录。日志显示 ELF 校验、断点暂停、复位/运行至起点、时钟核对、ETM/TPIU 配置、LA 采集的阶段；读写失败带实际内存地址。
- 故障状态下不继续自动清理或启动采集，保留现场；新增“恢复调试口（保持暂停）”。恢复动作只初始化调试传输，随后保持 CPU 暂停并尝试恢复本页配置，不自动复位、重试采集或继续执行。
- 断开路径即使清理失败也会尝试释放 USB；错误不会假装配置已经恢复。

寄存器操作参考：[Arm CMSIS Debug Description 的 ResetSystem 流程](https://arm-software.github.io/CMSIS_5/5.8.0/Pack/html/debug_description.html)。

## 验收

22 项 JavaScript 测试通过，包含控制写不重试、不偷偷恢复调试口、保留首次错误、先安装断点再单次复位、显式恢复不自动运行 CPU；原断点/时钟/寄存器恢复测试也通过。Python 侧未修改。

两条真机路径均使用原有 H7B0 70/35 MHz 固件，2 ms / 500 MSa/s：

- pyOCD 适配器：`capture-9e27db237292403c80cf28181183a79f`。
- **生产 WebUsbDapProbe**：`capture-6ceed9e889634f22ba25dbc33424488c`。`usb_acceptance_bridge.py` 只转发 IF0 / OUT2 / IN1 的原始 USB bulk 字节，不实现 SWD、寄存器读写或复位；所有协议、读写、断点与复位逻辑由生产 JS 完成。

两条结果一致：首指令 `HAL_SPI_Transmit / 0x08003c1c / 2de9f047`；96,407 条路径，85,642 条已确认执行，5,806 包，2 次中断，79 条条件未知；解码错误、overflow、未覆盖字节均为 0。生产 USB 路径最后 `_faulted=false`；配置恢复后 CPU 运行，8767/8768 验收接口与 USB 资源已释放。

生产 USB 测试覆盖了用户遇到的底层路径，但没有自动完成用户外部浏览器的设备选择和整套页面点击流程。用户仍需重开页面进行人工复验。

报告：`validation/2026-10-11-control-write-reset-live.json`、`validation/2026-10-11-webusb-control-reset-live.json`。最初读取到启动阶段 64 MHz 的失败快照保留在 `validation/2026-10-11-control-write-reset-initial.json`，后续运行恢复到 70 MHz 并通过上述验收。

## 用户复验步骤

重新打开页面 → 浏览载入匹配 ELF → 连接探针 → 添加 `HAL_SPI_Transmit` 断点并选择为起点 → 时钟页核对 70/35 MHz → 采集页检查 LA、设置 2 ms → 点击“联动采集”。保持默认“先复位，再运行至起点”，不用提前点“配置并暂停”或“复位至起点”。

如仍失败，页面现在应显示首次失败的阶段和地址。优先保留操作记录；若出现“恢复调试口”，点击后目标保持暂停，再重新开始采集。
