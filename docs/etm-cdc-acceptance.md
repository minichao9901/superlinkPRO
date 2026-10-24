# ETM 固定验收：CDC 四档窗口

日常与发布主线统一使用 `etm/firmware/stm32h743_cdc_trace` 的 CDC echo 例程。
这是今后的验收规范，不代表本轮重新采集过这些窗口，也不把软件允许 1 s 当作硬件录满证明。

| 窗口 | 500 MSa/s 的期望样本数 | 主要检查 |
|---|---:|---|
| 10 ms | 5,000,000 | 连通、短窗解码、首中末分页与源码定位 |
| 200 ms | 100,000,000 | 默认回归、CDC持续收发、三态与导出/记录往返 |
| 500 ms | 250,000,000 | 中长窗正确性、峰值内存、分页/筛选延迟 |
| 1 s | 500,000,000 | 长窗完整度、端到端耗时、资源上限和失败清理 |

## 固定条件

- 绑定实际 ELF SHA256、构建命令/编译器与源码快照；使用同一个 CPU/Trace 档位比较四个窗口。
- 首选已验证长窗较稳定的 CPU60 MHz / 物理 TRACECLK20 MHz；30 MHz 另列性能/信号裕量组。
  改时钟、GPIO、门限或滤波后是另一个配置组，不能混算速度或质量。固件和网页的实际回读为准。
- 500 MSa/s、CH0 时钟/CH1～4 数据、门限 1.65 V、取样 0.25、解码前 4 ns 滤波作为本板起始配置，
  全部随记录保存；设备侧滤波单独记录。默认关闭 branch broadcast、return stack、cycles，比较时一致。
- 使用已初始化并枚举成功的 CDC 持续工作窗，不在这组例程中反复烧录/复位。目标 CDC 与探针串口必须区分。
- 单独的初始化覆盖测试可从 `trace_hook_entry` 开始；初始化阶段不要要求尚未出现的 CDC 端口完成预热。
  初始化窗与已枚举后的持续负载窗分别报告。

例：先由操作者确认目标已运行匹配固件、CDC已枚举；下列命令会执行采集并控制目标，
不是本轮已运行的记录。`<目标CDC端口>` 与 ELF 要替换成实际值，输出目录每次新建。

```powershell
python -X utf8 etm/trace.py --json-out E:/trace-results/cdc-200ms-result.json capture `
  --elf etm/firmware/stm32h743_cdc_trace/fw.elf --out E:/trace-results/cdc-200ms-001 `
  --seconds 0.2 --rate 500000000 --threshold 1.65 --fraction 0.25 --glitch-ns 4 `
  --expect-core-hz 60000000 --expect-trace-hz 20000000 `
  --no-return-stack --no-cycles --echo-port <目标CDC端口> --echo-interval 0.004

python -X utf8 etm/trace.py verify --record E:/trace-results/cdc-200ms-001 --require-decoded
```

`fw.elf` 必须确为上述20MHz配置；不能仅修改 expect 参数冒充已调整固件。
另外三档分别使用 `--seconds 0.01 / 0.5 / 1`，无需改通用 CLI 协议。

## 必须分别通过的门槛

1. **长度**：KVDAT真实深度、LA实际深度回读、请求样本数相符，使用现有verify容差。
   例如请求1s实际927ms就是不完整，不把多段或重复短窗拼成真实连续1s。
2. **质量**：沿用严格 verify；overflow、中途失同步、未核算字节等不能放宽。
   仅现有规则允许的输入尾部 INCOMPLETE_EOT 单列保留；unknown数量不因展示折叠而下降。
3. **业务**：CDC echo 的发送数、完整回收数、序号/CRC、重复包和超时均记录。
   `echo-result.json` 包含预热等窗口外活动，它本身不证明所有收发均在Trace窗口内；
   还要在Trace中核对DWC2/CDC相关处理路径和覆盖范围。10ms不强行要求USB完整初始化。
4. **一致性**：固定输入上比较优化链与保留的Python/旧格式参考；小记录逐事件完整比较，
   大记录用紧凑PC/三态序列完整比较并核对边界、计数、源映射，不能只对输出大小。
5. **使用链**：首/中/尾分页、深页搜索、unknown导航、简化C/HTML、完整C、CSV、记录包往返，
   至少在200ms完整执行；1s还要测试深页/筛选/导出取消或失败后的完整性。
6. **性能**：采集、LA传输/保存、前端、解码、索引、首屏和导出分别计时；另报端到端与进程树峰值内存。
   首次冷启动单列，固定ELF连续至少3次报中位数与范围。不把采集1s等同于处理1s。
7. **清理**：成功/失败后核对自身临时断点、设备租约、LA空闲、可恢复目标配置/CPU状态；
   不宣称恢复了复位前业务现场。没有新硬件工作时只报告离线验证。

## 保留与轮换

每个固件/时钟配置保留一组完整、通过验收的10/200/500/1000ms原始记录和一个故障边界样本，
加上匹配ELF、源码/采集配置、指纹清单、compact Store与摘要报告。正式包只留当前交付的一份。
记录放 `etm/records/baselines/h743-cdc/<配置ID>/<窗口>/`（默认不入Git），不要再放进 docs 或 tmp。
没有合格500ms/1s样本时明确标为待采集，不复制10ms数据伪造。

CDC是主线基准，但仍保留小型ETMv3/F103、未知IT、异常/尾部包、损坏拒绝和合成边界测试。
单一普通CDC工作负载不能代替这些语义覆盖。

清理规则与脚本见 [存储维护](storage-maintenance.md)。
