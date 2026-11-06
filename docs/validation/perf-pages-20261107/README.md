# RTT/HSS性能复核、Pages与启动策略定位

日期：2026-11-07（Asia/Shanghai）。基线`5d7f23b`。设备akaLinkPro序号`8F538633E6AEC4CA8ABB6A36FEBB1AC2`、F103ZE。接入时目标运行HCLK48MHz，完整512KiB Flash SHA与上轮原始备份一致。探针固件未重刷；没有抢占其他硬件进程。其他工程的FPGA构建/仿真未停止。

## RTT：复现历史3.05MB/s

用户指出的历史3.05MB/s来自F103ZE 96MHz固定文本负载、60MHz SWD、2048B块。额定72MHz对照在[2026-10-09历史报告](../../../web/docs/validation/2026-10-09-transfer-performance.md)为2.535MB/s。前轮2.33MB/s使用72MHz带序号负载，多了逐条十进制编码。三者需要分别复现。

同一块板、探针、USB线和本机接收器，本轮每段预热0.8s，正式窗口8s，按正式接收增量/墙钟计算十进制MB/s：

| 目标负载 | 三轮MB/s | 完整性范围 |
|---|---|---|
| 96MHz固定hello文本、60MHz请求档 | **3.055484 / 3.057519 / 3.055554** | 图案错字节0，探针读/写错误0；总收到字节=停止后的moved |
| 72MHz相同hello文本、60MHz请求档 | 2.535351 / 2.535368 / 2.535319 | 同上，与历史额定档一致 |
| 72MHz seq递增记录、60MHz请求档 | 2.357158 / 2.340758 / 2.332517 | 三轮序号缺口0，探针读/写错误0；总收到字节=moved |

96MHz为**历史超频配置的短窗口复现**，没有设为产品默认值；72MHz与96MHz均以RCC回读及HSE8MHz计算确认，没有外部测频。固定图案可以检查字节相位/模式，无法检测整条相同记录的丢失与补偿重复，唯一序号检查另在72MHz负载完成。匹配条件后RTT转发达到了历史3.05MB/s。

RTT Viewer另测72MHz固定文本20MHz请求档约0.482MB/s。60MHz普通CMSIS-DAP访存仍出现ACK=0，增加8个idle也未消除，96MHz目标同样复现；此路径不能用RTT转发的通过结果代替。旧Viewer历史数字来自另一序号探针和浏览器，当前没有证据将此限制归因于本轮重构。

## HSS：修正验收配置和接收器

前轮工具flags固定`0x01`，关闭了生产页的FAST_BATCH与CDC_OFF；周期参数仅接受整数；PyUSB串行读也没有使用生产的预读配置。关闭优化时本轮再次量到约376kSa/s，符合前轮376k结果。采用`0xa1`、支持2.5us tick配置、按生产公式配置有界有序异步预读后：

| 条件（额定72MHz目标） | 三轮正式窗口速率 | 正式窗口质量 |
|---|---|---|
| 单变量2us，名义500kSa/s | **496374 / 495225 / 496250 Sa/s** | USB=0、读错=0；仍有28235～30019次调度跳拍/约8s，与历史约0.8%损失同量级 |
| 单变量2.5us，名义400kSa/s | 399977 / 399976 / 399974 Sa/s | USB=0、读错=0；192/194/215次调度跳拍，约60～67ppm |
| 八变量连续32B、20us | 49999.90 / 50000.03 / 50000.03组/s | USB=0、读错=0；每窗1次调度跳拍 |

每档三轮8s，均预热0.8s，使用action11原子32位计数和24MHz探针时间计算稳定窗口。最终接收采用与生产公式相同的16×8192B预读、单消费者按提交顺序交付；HID轮询不阻塞数据读循环。初期单同步读/6条请求仍出现USB耗尽，失败数据保留在摘要中。

九段完整原始输入共检查25,197,070个`g_pack.u_hi`值（八变量取其对应槽），固定高16位异常与低16位计数倒退均为0；包缺口、乱序、重复、重同步均为0。停止边界仍有小于一包的未发布尾部，见ISSUE-043。固件`scope_sampler_stop()`目前停止调度但不提交半满的DATA包，不能把seq连续当成所有produced都已送达。

早期Node原生多读试验出现顺序/重启问题，未采用其异常窗口作为最终质量证明。改用现有PyUSB加载的libusb异步接口，无新增系统依赖；请求有界、保序，关闭时等待取消回调退役后再释放内存/接口。这里没有执行浏览器授权弹窗或绘图验收。

`dap-webusb.js`、RTT协议、Scope生产接收器和探针Scope采样源码与重构前`fb46f9e`逐字节（统一换行）相同，指纹保存在[summary.json](summary.json)。本轮修复的是验收条件、原生接收工具和统计口径，没有通过修改产品采样算法来制造性能提升。匹配历史条件后未观察到这两项所疑的吞吐回退。

## 恢复、证据与回归

原CDC48固件已恢复，完整512KiB Flash SHA与接入时一致：`48f226217497607c696a156addbb363925577499b00ee64f187bd5eea7a94499`。RCC/FlashACR恢复原值、目标48MHz运行、CFSR/HFSR=0；RTT/Scope均已停。切换无USB夹具后Windows COM34复现错误31，仅重启目标`USB\VID_F055&PID_1031\F103CDC0001`后PING/PONG通过，列ISSUE-047环境规避。

数值、失败、计数、恢复与源码指纹见[summary.json](summary.json)；96/72MHz固定文本ELF的构建参数/指纹见[fixtures.json](fixtures.json)。精确ELF、原始输入代表和清单保存在`E:/superlinkPRO-backups/20261107-perf-pages/`，不向Git提交大波形或用户原固件。

253项Web离线、9项CLI和4项发布器回归通过。发布器测试覆盖远端损坏不公开草稿、既有错误资产不覆盖、哈希通过后才公开、跳转不携带GitHub凭据。ETM页面/分析源码及历史桥包保持原样。

## 启动策略与Codex设置

按[OpenAI Docs：托管配置](https://learn.chatgpt.com/docs/enterprise/managed-configuration)、[Windows部署](https://learn.chatgpt.com/docs/enterprise/windows-deployment)检查用户/项目/系统配置、注册表RequirementsPath和命令规则。当前聊天的桌面持久配置为Full Access、`approvalPolicy=never`、`approvalsReviewer=user`；未发现项目配置、规则目录、系统requirements或旧managed配置。相关时段CodeIntegrity/AppLocker无事件，Defender两条事件均不涉及本项目。未修改Codex或系统安全设置。

历史调用也没有请求require_escalated，原始工具判决仍只给blocked by policy。不能进一步断言它命中了Codex Auto-review的某条规则。当前不开放镜像目录、不连接目标的同一CLI启动到ready并正常stop；这是受限启动恢复证据，不代表重放了原来包含镜像写入能力的完整动作。具体设置和范围见[startup-settings.json](startup-settings.json)。ISSUE-044保留具体执行门禁判决待平台诊断。

## Pages与最新包

未改内容的完整重试[42305818553](https://github.com/minichao9901/superlinkPRO/actions/runs/42305818553)再次build通过、deploy约10分钟超时。此前三次失败证据继续保留。约229MB压缩产物低于[GitHub官方1GB站点限制](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)，不能把失败直接写成超过站点上限。

新方案将已校验工作台ZIP交给GitHub Release，Pages清单指向对应版本。上传先用草稿，完整回读核验后公开；匹配已有同名资产才复用，禁止覆盖不一致资产。本机EXE同源下载与历史ETM下载路径保持。web.3候选、Pages新产物及线上完整下载还需分别验证，最终结果另附发布证据。
