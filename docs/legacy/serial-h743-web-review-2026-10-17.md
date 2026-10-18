# H743 网页接入与 DeepSeek 实验复核（2026-10-17）

## 当前结论

基于 DeepSeek 分支 `h743-dsh-etm` 的 `77123fb14c2850637e2a4b3f39ff0f6326646968` 继续。保留它已打通的 CDC/ETM 配置、原始记录和连续 KVDAT；修正其不能成立的解码与验收结论。

H743 的网页目标识别、60/60/30 MHz 时钟回读、ELF 校验、硬件断点、复位至起点、ETM 选项回读、联动采集、源码与图表已接入。当前仍标为**实验适配**，没有把同 DEV_ID 的 H742/H750/H753 一并宣称验收。

**本轮有效采集条件**：阿波罗 H743、HSE25 MHz、CPU/HCLK60 MHz、物理TRACECLK30 MHz；Kingst500 MSa/s、CH0～4=PE2～PE6、1.65 V、半周期取样位置0.25、显式解码前软件滤波4 ns。默认滤波为0；原始CSV/KVDAT不被改写。该结果不证明未滤波信号没有窄脉冲，也不证明模拟信号质量已改善。

## 审查确认与修复

1. **P0：不能跨帧内完整 FSYNC 拼帧，也不能遇到非法 FF FF 就丢半帧继续。** Arm IHI0029D D4.2.2 规定完整帧同步在帧间；D4.2.3 允许帧内出现的是半字同步。OpenCSD 1.8.3 的 `trc_frame_deformatter.cpp` 也拒绝帧内FSYNC。恢复严格拒绝规则，保留正常HSYNC去除；增加所有偶数字节位置的反例，失败保留 physical.json/demux-errors.json。否则会掩盖采样错误，不能用“解出了指令”验收。
2. **P0：流式质量字段不能直接写零。** 原实现将 overlapBytes 固定为0，并用 processed 推出 unaccountedBytes；同时未做网页的IT条件未知/调用分析。现在按真实包区间核算，流式与常规使用相同条件执行及调用栈语义，错误计数覆盖 BAD_/OVERFLOW/RESERVED/INCOMPLETE_EOT。
3. **P0：原“流式 O(1)”不成立。** 原实现在末尾把整份decoded.json读入字符串、写回附加字段，又保留完整events数组及JSONL副本。新格式 `streamed.schema=2` 仅在decoded.json保存摘要，事件/包/元素只存JSONL；SQLite按迭代器建索引，导出简化C也逐行读写。CSV提取、ELF和派生调用树仍占内存，不能称整个链路O(1)或无限长度。常规内存解码恢复200万条保护；BIN自动流式，较长窗口/大CSV自动流式。旧版超大JSON明确要求重新解码，避免把服务拖入OOM。
4. **P1：不要仅按mtime/size缓存原始输入指纹。** 保留每次概览的真实SHA256核对，改为分块读取，避免整文件额外驻留。已有“同大小、保留mtime但内容被改”的回归测试仍有效；没有以性能为由弱化证据校验。
5. **P1：H743 网页原来仍不允许配置。** 使用实验能力标志启用H7的ETMv4路由及真实IDR0选项；纠正时钟页硬编码H7B0 VCO560 MHz，改用当前RCC推导。H743示例起点优先trace_hook_entry，文档说明固件与网页职责。
6. **P1：固件与脚本细节。** 修复240 MHz档位误走VCO=8f导致超限、显式AHB分频1被忽略；补APB3/4 ClockType，使用真实PCD结构体；USB模式切换有超时、检查初始化返回值，失败不置ready；普通hello文本不再附ERR UNKNOWN。烧录脚本检查OpenOCD退出状态，去掉AIRCR已运行后再次resume导致的非零退出。辅助脚本改用自身checkout，修正DEV_ID/REV及PLL输出使能位的诊断文字。span不再默默把当前LA缓冲当成指定记录，需显式--live-buffer确认。
7. **P1：集成页面窄屏裁切。** 主工具箱面板在窄屏改成static布局后，iframe退回150 px默认高度；补容器height:100%，实际复核从150变为578 px。没有用缩放截图掩盖此问题。

参考规范：[Arm CoreSight Architecture Specification v2.0，D4.2.2/D4.2.3](https://documentation-service.arm.com/static/5f9009d5f86e16515cdc0417)。本机参考源码在vendor/OpenCSD-1.8.3/decoder/source/trc_frame_deformatter.cpp。

8. **P1：缩放与新定位并发时旧光标覆盖新光标。** 保留所选记录代次校验，另加选择代次校验；缩放完成后不再覆盖期间新发出的定位。已用延迟渲染/新选择的可重复用例覆盖。
9. **P1：流式重解码期间不能暴露混合代次。** 每条记录有独立事务锁；侧文件写入和索引更新期间，读取该记录明确返回409，任务轮询及其他记录不受阻塞。终态done在事务释放后发布。

10. **P1：新页面不能混用仍在运行的旧桥后端。** 捕获前校验captureProtocol=2；旧服务需重启/更新，不会默默忽略软件滤波参数或继续使用宽松解复用。

## DeepSeek 原始波形：确实存在窄脉冲

下表直接读取其CSV，未做软件滤波。2 ns是500 MSa/s的一个采样周期；是数字采样观测，不能仅据此断言是振铃、串扰或某一种硬件故障。

| 原记录 | 实际CSV窗口ms | CH0最小宽度ns | CH0 2 ns脉冲 | CH0 4 ns脉冲 | 保存TPIU严格校验 |
|---|---:|---:|---:|---:|---|
| t2 | 2.000 | 14.0 | 0 | 0 | 通过 |
| cpu60-trace30 | 5.000 | 2.0 | 15 | 0 | 失败 |
| cpu120-trace30 | 5.000 | 2.0 | 28 | 1 | 失败 |
| sf30b | 5.000 | 2.0 | 10 | 0 | 失败 |
| sf30-50ms | 50.000 | 2.0 | 108 | 5 | 失败 |
| c60t30-50ms | 50.000 | 2.0 | 165 | 4 | 失败 |
| full60 | 20.000 | 2.0 | 66 | 4 | 失败 |
| full60b | 20.000 | 2.0 | 18 | 9 | 失败 |
| full120 | 12.000 | 2.0 | 79 | 0 | 失败 |
| dense40 | 2.000 | 2.0 | 11744 | 15887 | 失败 |

数据线上同样存在窄脉冲：例如cpu60-trace30的CH1有114个2 ns脉冲，CH2/3/4分别8/2/15个；t2的CH4甚至有3601个2 ns脉冲，但其时钟没有这些额外短脉冲，因此仍能在选择的取样点恢复有效流。不能把“数据线出现窄脉冲”直接等同于“每个脉冲都会造成解码错误”；时钟额外边沿尤其危险。

为什么旧纪要没有明确告警：扫频工具记录irregularHalfPeriods却未将其作为验收门槛；后来又通过宽松解复用绕过帧错误。原有“字节对账为零”等数据不足以证明无损。10份记录中，本次只有t2保存的TPIU字节流通过严格校验；原始CSV和缓存TPIU的关系仍以其记录元数据为准。

cpu60-trace30的KVDAT及capture.csv.la.json已保存CH0～4两采样点/4 ns滤波配置，但CSV仍含2 ns脉冲。**配置快照不能证明Socket导出已经滤波**。没有将用户重新点击滤波当作唯一修法，也没有断言厂商导出必然忽略滤波；具体应用/导出行为尚需独立A/B。

本轮先以离线软件4 ns对照验证假设，失败记录可严格解出64516条路径，包错误/overflow均0；该离线对照不是新硬件验收。之后才进行了下列真实重新采集。

## 新实板采集与控制流核验

网页按钮操作使用真实浏览器；由于内置浏览器无法完成原生USB设备选择，验收服务8911使用**仅在证据目录存在的实体USB字节转发适配器**，生产网页控制器及DAP驱动照常运行。第一行时间最早的capture-d9...来自网页完整按钮流程；其余为同一生产JS控制器/网页后端的批量实板复测。不是模拟硬件数据；也不能据此声称原生Chrome WebUSB授权、物理拔插/重新枚举已经验收。

| 记录 | 时长ms | 路径记录 | 已核验后继转移 | 控制流矛盾 |
|---|---:|---:|---:|---:|
| capture-2782e804bbec435797047e2a389a657f | 10 | 210602 | 208529 | 0 |
| capture-519f92f12fe14bd4820b48a961098ded | 2 | 63490 | 61447 | 0 |
| capture-988224627659415e9e02d22b8ebfca8c | 2 | 63530 | 61487 | 0 |
| capture-d9d041f33fb548aab98523204152928c | 2 | 63490 | 61447 | 0 |

以上4次均：Flash与ELF机器码一致、首条恢复指令匹配trace_hook_entry、严格帧校验成功、包错误/overflow/解码器错误/未处理/未核算/重叠/未知源字节均0，原始CSV实际SHA256与解码结果指纹一致。软件滤波处理量逐次记录。尾帧、未知条件和捕获边界仍原样保留，未补猜源码或耗时。

控制流验证只检查可证明的线性后继、条件未成立的顺序后继、立即数分支/调用，不猜间接跳转。首版诊断脚本曾把ldr pc,[sp],#4的地址偏移误当跳转目标；失败报告保留，修正后的权威结果为fresh-capture-validation-v2.json，新增回归测试覆盖此区别。

附加实测：CPU60档新固件烧录后5次精确13字节hello world!\n回显；网页三个ETM选项全开回读成功，再全部关闭回读成功；复位至起点成功；重复采集后恢复本页硬件断点；不复位再次等待一次性入口会明确超时，临时比较器清除、调试口仍可用，后续需复位而不是无限等待。

## 长记录结果及边界

- 正常与流式路径逐条对比238317个事件，相同的调用分析/执行状态；decoded元数据4470字节，SQLite分页结果一致。
- 对旧stream200的ETM源数据进行容量复测：2598778条指令，元数据18976字节，能读取第2400000条起的150行，总处理约336.7秒。它是容量测试，**不是硬件无损证据**：实际质量统计有2318个包问题、16个OVERFLOW、解码器错误26、860个未同步字节，不能只引用processed=input就说没问题。
- 首次建索引/分析仍是线性工作；超过45秒的首建概览等待有单独预算，解码阶段等待预算扩到15分钟；不是每个查询都O(150)。范围导出限制内存，整段可流式导出简化C。
- 本轮未重做“初始化→Windows枚举→单次hello”的完整连续长窗硬件验收；DeepSeek的分窗导出不能冒充本轮完整通过。原始连续KVDAT保留，若导出全段还须处理大CSV内存和耗时。
- 未重做完整频率上限扫频。dense40即使离线4 ns处理仍不能严格解码；它证明该配置失败，不足以把40 MHz定成LA通用物理上限。平均带宽利用率也不能线性外推到1.76 GHz或保证所有业务突发不overflow。

## 重现与证据

Run-ID：20261017-h743-web-review-c7a4e9。
证据根目录：E:/web-etm-trace-coordination/artifacts/h743-acceptance/20261017-h743-web-review-c7a4e9。
源码工作目录：E:/web-etm-trace-worktrees/codex-f103-target，分支codex/h743-cdc-etm。

```powershell
$env:SERIAL_ROOT='E:/web-serial-rtt-tools'
python -m unittest discover -s tools/tests -p 'test_*.py' -q
node --test tools/tests/*.test.mjs
python tools/h743/review-captures.py E:/web-etm-trace/captures/h743/t2 E:/web-etm-trace/captures/h743/cpu60-trace30 --out <新的报告路径.json>
```

主要文件：deepseek-pulse-review.json、reproducible-pulse-review.json、strict-demux-review.json、strict-phase-review.json、fresh-capture-validation-v2.json、repeat-summary-*.json、cdc-reviewed60.json、stream-parity.log、long-stream-review/validation.json、dense40-filter-review/result.json、flash-reviewed60-repeat.log、build-reviewed60-final.log、build-reviewed240.log。

initial-web-target.json是早期临时盘点快照，被后续盘点更新，不作为不可变验收证据；各次采集的session/decoded/原始文件及独立验证报告才是权威来源。全部重要文件的SHA256见本run证据清单。原始数据较大未提交Git，交接到其他机器须另行复制。


## 本轮验收覆盖说明

离线回归：Python115项、Node44项（最终提交前运行日志为准）。115项包含真实H7B0封存窗口、F103/H7能力及生命周期回归，未进行H7B0/F103实板重新采集。H743 CPU60固件实际烧录/验证/回显；240 MHz档仅构建通过，未烧录。

网页图表：使用独立浏览页打开实板记录，火焰图收起源码、以光标#2为焦点放大至#0～#31748后光标仍为#2；框架高度裁切问题已按实际DOM尺寸复核。截图保存在run目录h743-flame-web.jpg。测试用实体USB转发适配层及其空闲超时恢复缺口只存在于证据目录，未加入生产代码，也不作为原生USB拔插恢复通过的证据。
