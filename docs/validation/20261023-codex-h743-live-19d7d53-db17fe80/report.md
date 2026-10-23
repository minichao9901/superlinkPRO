# H743 200 / 500 / 1000 ms 真机验收（2026-10-23）

结论：限定 CPU60 / TRACECLK20 MHz 的持续负载采集、原生解码、查询、正式 EXE 导入重解码可用；**未通过正式开放 1 s 上限的全部判据**。1 s 完整处理约 14.1～14.4 s，首次导航存在约 0.48 s 峰值；产品与已发布 EXE 的默认采集上限继续保持 200 ms。30 MHz 在本次接线下未通过，不与 20 MHz 的结果混用。

- Validation-Run-ID: `20261023-codex-h743-live-19d7d53-db17fe80`
- Tested-Revision: `pre-commit-worktree`
- Tested-Base: `19d7d535271643b61ae399eac75cd1e35d1bdb5d`
- Tested-Dirty: `true`（仅固件持续负载、非阻塞 RTT 与构建脚本/文档；解码实现未改）
- Validation-Result: `partial`；Hardware-Test: `partial`
- 工作目录：`C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO`
- 原始证据：`E:/superlinkPRO/docs/validation/20261023-codex-h743-live-19d7d53-db17fe80`
- 原始记录/ELF/记录包：`C:/Users/Administrator/.codex/validation/20261023-codex-h743-live-19d7d53-db17fe80`
- 未 push / 发布；本轮不重写历史、不更新线上 ZIP。

## 条件与计时边界

H743（CPUID 0x411fc271、DEV_ID 0x450、REV_ID 0x2003），HSE25 MHz，CPU/HCLK60 MHz；PLL1R40 MHz、物理 TRACECLK20 MHz。探针 akaLinkPro `8F538633E6AEC4CA8ABB6A36FEBB1AC2`；目标 CDC COM33（VID_F055:7431），不是探针 COM34。KingstVIS 3.6.6、Kingst USB VID_77A1:01A2、500 MSa/s、门限1.65 V、CH0=PE2、CH1～4=PE3～6；GPIO speed2、fraction0.25、软件滤波4 ns，LA 硬件滤波快照全部关闭。本次未取得 LA 制造商序列号。

ETM CONFIGR=0x1011，Trace ID2；分支广播关、返回栈/周期计数开。目标先运行、持续负载预热后，连续采集；没有 reset/断点后的闲置预触发区。实际 KVDAT 深度分别100M/250M/500M，时窗覆盖200/500/1000 ms（最后一个采样点比名义窗口少2 ns）。不拼接、不分片冒充整段，不经 CSV 中转。

banner 每20 ms，echo 脚本每4 ms发送带序号/CRC的文本并持续回收；采样完成后停止发包，继续读取。最终会话7776发送/7776回收、CRC错误0、重复0、遗漏0。下表 echo 数来自 trace 恢复的 `main → handle_command`，证明每份窗口内实际有业务活动；banner 次数分别10/25/50。最终九份采集在已置满的 RTT 缓冲区下运行，调试日志可以跳过，CDC/ETM 不跳过。

下表“完整处理”从原始波形交给解码到 store/完整索引/默认图已发布，不含 LA 设置、物理采集、USB回传、KVDAT保存、上传及浏览器渲染。计时包含波形取样、TPIU、OpenCSD、写出/索引/完整性绑定；三次样本全部列出。1 s 从采集请求到任务完成为24.946 / 24.666 / 24.916 s（亦不含请求前 ELF 校验、目标配置、负载预热与浏览器渲染）。不能把14 s说成采集总等待时间。

| 窗口 | 三次恢复指令数 | 三次完整处理 / s | 三次 echo 调用数 |
|---|---|---|---|
| 200 ms | 3,670,868 / 3,669,657 / 3,662,813 | 3.317 / 3.381 / 3.351 | 46 / 45 / 45 |
| 500 ms | 9,178,053 / 9,171,476 / 9,177,532 | 6.903 / 7.129 / 7.303 | 110 / 112 / 112 |
| 1000 ms | 18,356,923 / 18,357,489 / 18,356,006 | 14.247 / 14.109 / 14.350 | 226 / 226 / 224 |

固件 ELF SHA256：`2a35e24ca61f20fbe2d12694c5dd08fa460b3e20a84fcd2edef8a1665464a2fd`。FLASH PT_LOAD 31856字节烧录后全部回读一致，最后清理前再次由生产 JS 校验一致。`-BannerEveryMs 0` 和 `20` 均构建通过；新增 `-SdkRoot` 的重建 FLASH 字节与实烧镜像逐段一致（ELF debug 路径可不同）。SDK 来源/1453文件哈希见 `sdk-inputs.json`，本地依赖移至数据目录 `h743-sdk/`，不提交第三方 SDK。供应商 SDK 的 `at` 属性有 GCC 忽略警告；实测 RTT 控制块位于0x24000cf4，不能声称固定地址 RTT 已验收。

## 发现和修复

1. 周期 banner 占用 CDC IN 时，原主循环仍交付 echo 给“忙则返回”的发送函数，2 s压力测试462发/458收。周期模式等待端点可用后再处理待收数据，复测462/462。
2. 本机 SDK 默认 RTT 模式为阻塞。约1.37万次 echo 后，256 KiB RTT缓冲区写满，主循环卡在 `_WriteBlocking`；后续诊断捕获约3425万条指令，主要在该函数，不能计为有效 echo 负载验收。改为应用显式 `SEGGER_RTT_MODE_NO_BLOCK_SKIP`。置满 RTT 后695/695回显；九次采集后的最终健康检查691/691，banner仍持续。可跳过的是调试日志，不是业务 echo 或 trace。
3. 30 MHz 下同步启动和预运行采集均失败；相位/滤波/引脚速度对照未获得完整通过记录。原 Python 前端也在同一原始记录、同一物理偏移920502处拒绝，不能归咎于 C++ 优化。20 MHz 的通过仅是新的限定条件；尚不能证明30 MHz问题的具体模拟电气根因。
4. 最终第三份500 ms有1个 `I_INCOMPLETE_EOT`，位于 offset1087019，长度1，恰好在1087020字节输入末端。保留原始 `packetIssues=1`，逐项判为明确的末尾截断边界，见 `capture-boundary-adjudication.json`。其余八份包问题0；全部溢出/重叠/遗漏/未处理字节/decoder error=0，TPIU结构错误、帧内FSYNC、丢弃partial frame、idle resync均0。没有放宽帧内损坏检查。

开头未同步数据、未知源前缀、TPIU尾帧与 IT 条件未知都明确保留；仅有1个初始化 NO_SYNC gap。字节对账不等同采集无损证明，未推断采集前调用栈或把条件未知当作已执行。

## 正确性与正式 EXE

- 修复前一份有效200 ms：旧文本/legacy完整链路与新 store **3,722,397条事件所有字段0差异**，CallTracker分析及summary0差异。原对照overview有5项表示/HTTP元数据差异：eventsFile、nativeJsonl，以及仅HTTP采集携带的targetSnapshot/captureScope/laFilter。三项元数据逐项与原session核对、两项按现有原生表示契约归一化后，overview236项检查0差异；原失败报告保留，见 `legacy-200ms-adjudicated.json`。
- 修复后200/500/1000 ms各取一份：独立Python参考前端与C++的完整TPIU、ETM字节流逐字节一致，framing逐字段一致。200 ms走原 `trace_port.extract + tpiu_demux`，较大记录走有界Python `Frontend`。报告 `frontend-equivalence-final.json`。
- 不是九份记录的全部旧链路重放，也未完成1 s的1836万事件逐事件legacy对照；不要将上述分层证据说成全链路数学证明。
- 正式线上同版 ZIP：`1.0.14-native.12`，SHA256 `30a849972579e63d4e0b92fc78f351ade5dc67bb914013ee0fff5ac51b2e45db`；验证包内1929文件哈希后实际启动EXE。
- 实际从1 s记录导出 `.etmrecord`：133,051,224字节，打包39.70s，导入2.67s。导入后summary/quality/analysis及首中末150行全部一致、输入指纹验证为true；实际重解码15.24s，再次全部一致，确认默认引擎native。
- 实际导出中段100001条记录的简化C、完整C、HTML、CSV，均HTTP200且内容非空、HTML有结束标签；逐个大小/SHA256见 `portable-1s-acceptance.json`。133 MB记录包未触发1 GiB上传/成员预算，不取消保护。
- 真浏览器打开源码服务的首中尾位置、源码/调用栈、全段/局部火焰图；正式EXE网页也打开重解码后的1836万指令记录及火焰图。截图 `browser-portable-execution-1s.png`。浏览器下载动作服务器已返回200，但下载完成事件接口超时；不声称浏览器下载事件已验证。四项导出另由真实EXE HTTP收取并检查。
- 设备操作通过生产 `WebUsbDapProbe + EtmTarget` JS、真实CMSIS-DAP USB字节转发完成；没有伪造探针，但本次未覆盖浏览器WebUSB选择器/USB授权交互。未在EXE中直接采集1 s；EXE保留200 ms上限，1 s通过原始/记录包离线导入重解码验证。

## 查询、资源与门禁

九份记录审计见 `capture-audit-final.json`；包括深度、实际时窗、文件全量SHA256、完整性绑定、首页/中页/末页、event、6模式双向navigate、window/timeline、issues。应用新建适配器为冷打开，OS文件缓存可能已热；审计阶段曾与ZIP校验并行，不能当成独占机器的绝对性能上限。

- 1 s首次overview+150行+默认window：0.748～0.842s；首次默认window约43～49ms，适配器相同window缓存约2ms（HTTP另见逐请求结果）。
- 安静条件下正式EXE的36个导航查询：首次模式访问P95=248.9ms、最大477.1ms；重复预热后P95=30.8ms、最大33.4ms。首次惰性加载成本不能隐去。
- 1 s原生解码进程树峰值RSS=571.7MiB，Kingst另计369.9MiB；EXE导入/重解码树峰值647.5MiB，含导航后抽样最高647.5MiB。源服务浏览多份记录缓存后最高约1.68GiB，不把同时运行两套服务的总内存混成单套应用指标；不含浏览器本身。
- 单份1 s KVDAT约309.8MiB，store约47.0MiB。记录全目录大小（含native.bin等原始/中间流）在审计逐项给出。
- 机器：AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD，8物理核，内存14.83GiB；未修改Defender、电源或全局设置。

| 门禁 | 判定 |
|---|---|
| T1 200 ms完整处理≤3s | 未达标：3.317～3.381s |
| T2 1 s完整处理≤10s | 未达标：14.109～14.350s |
| T3 首次打开≤1s | 适配器/API口径通过；浏览器未单独精确测绘制时间 |
| T4 全区间首次统计≤1s | 默认预计算区间通过，预计算已包含在完整处理计时 |
| T5 相同区间缓存≤100ms | 通过（适配器及实际HTTP） |
| T6 局部查询/导航P95≤200ms | 首次模式访问未达标；预热后通过 |
| T7 解码进程树RSS≤2GiB | 本轮采样通过；浏览器与Kingst单列 |
| T8 新派生store≤512MiB | 通过，约47MiB；不含原始/中间流 |
| 1 s全事件旧实现对照、LA序列号、浏览器WebUSB交互 | 本轮未覆盖，不冒充完成 |

## 下一轮具体方向

1 s首样的主要成本：物理前端3.17s、OpenCSD1.59s、store构建与索引9.43s。其中原生builder约3.93s、queryIndex约3.40s。优先分析/减少builder的动态段处理与索引默认视图/编码成本，再考虑前端整数采样和阶段重叠。OpenCSD不是当前最大单项。

另一个实际等待点是记录包ZIP打包约40s，宜单独做压缩级别/快速无压缩打包及后台流式输出对照；不要把它算成14s解码的一部分。30 MHz应先查接地、线长/连接与信号质量，再在同一负载和严格质量检查下重测；未修复前保留20 MHz限定条件。

浏览器另观察到报告未显示实际采样时长、隐藏源码窗首次展开未自动滚到所选行；本轮未修改网页，不把这些交互说成已修复。

## 原始记录索引

| 记录 | 窗口 | 实际深度 | 原始KVDAT SHA256 |
|---|---|---|---|
| capture-5129ec9260564aa4b638eb7d1fc16cf7 | 200 ms | 100,000,000 | `09bcd12d8ac81b3f554743f7c8c575ad99b38df722c0f86c0c1731d622b8d865` |
| capture-2e108eab6a194905a9fad3b6d191ab19 | 200 ms | 100,000,000 | `a6d5535e34be94724183be6bcbc175f38beb710b14db198c88b543eacf158725` |
| capture-0e9143e57c46418e882baf313f07d671 | 200 ms | 100,000,000 | `8ff206cbaf9e48a342fb973c2f182c83bfe4b393c0550ae15c2a3420ad8a7375` |
| capture-ac3d5229fdaa4cb784fb01c0ce7cdea9 | 500 ms | 250,000,000 | `d1a4049fcc2ce17b5b24cd1e7764e3a4c38ca74e058a15660ec60c58bc3a2dac` |
| capture-1e91e5e1bdab424bbb00866640136ed0 | 500 ms | 250,000,000 | `1dfcff672a908546e1c8b3a96cdb5c6cfabd7b7892bb3e6f17bc4204bb6e04b4` |
| capture-424e07686abb447ab027b8427896ca9b | 500 ms | 250,000,000 | `d9521b9e1a76187fa6b08b2ce1b7f84bea34ea608184f404279c595135f57d75` |
| capture-d4c1ed1b43214b49ba75e4467a6e1d8b | 1000 ms | 500,000,000 | `35093202d7ef0f8cfed8bdae41d52d5c55f3b9721a3c7fc6f894f838ceec3693` |
| capture-110080e64bcf4e47ba7d618f64a96c87 | 1000 ms | 500,000,000 | `9f08227ca03169587966283a771a93288821d6869b71af8a7e687f3d37a73497` |
| capture-e1f54bb5e2b64abf95620fd071d351df | 1000 ms | 500,000,000 | `5d0048990b783c8cdbe6c24d96c34224ccca8b235b87910d71ca14dbd3650191` |

原始记录均保留，诊断/失败样本未覆盖或删除。最早板上固件前128KiB备份仍在 `flash-before-128k.bin`，SHA256 `8316028250bff3dba1aef0345c7ebd973f827b9c2de2d7a2d34dc9c91a1353eb`。未自动恢复旧固件：板上保留本次非阻塞持续负载固件，方便后续复测。

最终状态：再次Flash核对一致；ETM/ETF已停、TPIU状态2、CPU继续运行，g_trace循环增长，RTT flags=0、满环仍不阻塞，最后691/691 echo及165条banner。探针接口和COM33已释放，源验收服务已关闭；正式EXE工作台保留于 http://127.0.0.1:58361/#etm 。

## 可复核入口

本次实际脚本（均位于上面的证据目录，不是可移植的产品命令）：`capture-continuous-v3.mjs`（200/500）、`capture-continuous-v4.mjs`（1 s及明确EOT边界），`run-monitored-v3.py / v4.py`、`audit-captures-final.py`、`reference-frontends-final.py`、`reference-legacy-200ms.py`、`adjudicate-legacy.py`、`force-rtt-full.py`、`serial-forced-full.py`、`portable-1s-acceptance.py`、`portable-navigation.py`、`final-target-health.mjs`、`serial-final-health.py`。入口及运行时使用本机独立端口，硬件操作有独占租约；旧协调目录本机不存在。

例如本轮实际执行：

```powershell
$env:PYTHONUTF8 = '1'
python "<证据目录>/run-monitored-v3.py" acceptance-final ".2,.5,1" 3
# 遇末尾EOT包停止后，保留样本、逐项确认，再执行：
python "<证据目录>/run-monitored-v4.py" acceptance-final-1s 1 3
python "<证据目录>/reference-frontends-final.py"
python "<证据目录>/portable-1s-acceptance.py"
```

以上脚本依赖当次设备/服务及独占输出目录，**不得直接重跑覆盖已生成证据**；复测须新建run-id和输出路径。`evidence-index.json` 保存重要证据、脚本、二进制和受测源码的SHA256；本报告不包含USB转发器临时令牌。
