# PXLogic / F103ZE 采集与解码验收

日期：2026-10-13。使用另一位 AI 提交的 `tools/la/pxlogic_la.py`，通过 PXView CLI 采集真实硬件；未使用桌面点击自动化。

## 已通过的流程

目标 Flash 与当前 worktree 的 `firmware/stm32f103_cdc_trace/fw-ze.elf` 校验一致，20168 字节。CPU/HCLK 48 MHz，Trace 输入 HCLK 48 MHz，PE2 上的 TRACECLK 24 MHz。ETMv3.4，Trace ID 2，ETMCR 回读 `0x810`，分支广播关闭。

同一持续 SWD 会话运行实际 Web `EtmTarget` 控制器，复位到 `cdc_acm_init`、配置 ETM/TPIU 并回读，然后继续 CPU。PXLogic 对连续运行目标自由采集：

```text
Python 采集脚本 → PXView CLI → CH0～4 原始位流
  → 同构跳变 CSV → 四位 DDR 提取 → TPIU 帧与源 ID 校验
  → OpenCSD ETMv3 → 匹配 ELF 机器码 / DWARF / 源码
  → Web 导入与重新解码 → 执行流 / Timeline / 火焰图 / 源码浏览
```

LA 参数：真实设备 ID 2，CH0～4=PE2～PE6，共地，500 MSa/s，10 ms，2.0 V。RLE、软件毛刺滤波、硬件滤波和信号反相均关闭。每个通道导出625000字节，合计5000000个采样点；另通过 PXView 配置回读确认500000000 Sa/s、5000000点、Buffer模式。

| 连续采集 | 路径指令记录 | 已证实执行 | 直接分支/调用落点核对 | 条件未成立分支核对 | overflow / 包错误 / 未处理字节 |
|---|---:|---:|---:|---:|---|
| A，10 ms | 185030 | 166537 | 36994 | 18493 | 0 / 0 / 0 |
| B，10 ms | 187254 | 168538 | 37440 | 18716 | 0 / 0 / 0 |
| C，10 ms | 188104 | 169304 | 37609 | 18800 | 0 / 0 / 0 |

三轮均全部对应有效源码行，TPIU 帧错误及未知源字节为0，解码器最后错误为0。还核对相邻线性指令地址连续性。函数包含 `main`、`cdc_poll`、`SysTick_Handler` 和 `HAL_IncTick`。这验证当前空闲 CDC 负载；不能代替 USB 大量收发及任意业务负载的验收。

记录保留采集开始处等待同步和末尾不完整帧边界；这些边界不补猜。0 overflow 和源码映射率100%不等于任意负载无损保证，也不代表程序覆盖率100%。火焰图宽度仍按执行序号/路径记录统计，不是函数耗时。

## 换线后的结论

旧接线的原始数据曾出现额外时钟边沿、非法 TPIU 帧及解码错误，多档门限/滤波/降频未形成可靠结果。用户更换 LA 线并重新共地后，同样的48/24 MHz、500 MSa/s、2.0 V、不加滤波条件立即成功，且连续三轮10 ms通过。它支持“连接/回流路径影响高频采集”的判断，不能凭数字 LA 数据进一步断言是某根线接触、线长、电容或 MCU 驱动能力中的哪一项。

另修复了冷启动配置顺序：先打开 `DBGMCU_CR.TRACE_IOEN` 及四位并行模式，再等待 ETM 编程状态。该门控未开启时，烧录/复位后会出现编程状态等待超时；此问题与 LA 解码质量是两个独立问题。有对应目标控制器回归测试。

排查时新增显式 `-CpuMHz 24` 诊断档，PLL48/AHB÷2保持USB48，物理TRACECLK12；旧线下未解决问题。普通ZE默认仍为48/24 MHz，当前板上已恢复该默认档。

## 复现命令

先用网页连接探针、加载匹配ELF、选择初始化后的业务断点、配置并暂停，再继续CPU。保持目标连续运行，串行调用以下命令；不要让网页和另一程序同时占用探针或LA。

```powershell
python tools\la\pxlogic_la.py capture --device 2 --channels 0-4 `
  --rate 500M --time 10ms --threshold 2 `
  --out captures\pxlogic-f103ze.csv

python tools\decode_trace.py captures\pxlogic-f103ze.csv `
  --elf firmware\stm32f103_cdc_trace\fw-ze.elf `
  --config captures\f103-etm-config.json --out decoded\pxlogic-f103ze
```

`f103-etm-config.json` 使用实际寄存器回读，本次为：

```json
{"protocol":"ETMv3","idr":1091891777,"ctrl":2064,"ccer":100352,"traceid":2}
```

在文件页导入CSV与ELF、选ETMv3配置，或直接导入下述便携 `.etmrecord`。原始位流为 LSB-first、每字节8个采样点；采集脚本产生唯一目录、核对导出容量/文件时间并保存 SHA256，不能复用历史位流冒充新采集。

CLI 清单如实保留 `actual.verified=false`：它本身不回读采样率；本次500 MSa/s通过独立PXView配置回读交叉核对。不要用高频24 MHz信号的中位21样本完整周期推导精确采样率：500/24不是整数，会量化成504 MSa/s；首轮2 ms的这一推导结果不作为精确时长依据，正式三轮均按500 MSa/s换算。

## 证据与使用入口

证据目录：`E:\web-etm-trace-coordination\artifacts\20261013-pxlogic-new-cable`。

- `new-cable-10ms-a/b/c.etmrecord`：CSV、匹配ELF、源码、SWD配置快照、原始五通道位流及采集清单。
- `new-cable-10ms-a/b/c-validation.json`、`validation.json`：包/字节核算、分支及源码核对。
- `web-record.json`、`web-acceptance.json`：导入的Web记录编号与重新解码/浏览接口结果。
- 包中的 `pxlogic/` 保存完整原始位流；现有服务器只导入标准成员，不能指望再次从网页导出时自动带回这些额外成员。应保留原始证据包。

页面地址 `http://127.0.0.1:8774/`，记录名称 `PXLogic · F103ZE · 48/24 MHz · 10 ms · C`。

## 尚未通过的部分

**网页“联动采集”目前仍为Kingst后端。** 本次通过的是PXLogic Python自由采集→Web导入分析，不能说已完成PXLogic断点联动或网页直接采集适配。

CLI的`--trigger`参数不生效；实测预先设置触发后，用带device ID的CLI采集还会清除触发配置。直接MCP设置D0下降沿并启动的测试也曾在CPU暂停时结束，尚未证明可靠武装。集成前必须先证明CPU暂停时LA持续等待、继续CPU后触发、停止/取消能确认空闲，不能仅以`started=true`或`state=capturing`认定已武装。

本轮结束已恢复取得的ETM/GPIO寄存器资源并让CPU继续运行，释放SWD连接；F103 TPIU仍保持并行协议，避免运行中动态切SWO停钟。
