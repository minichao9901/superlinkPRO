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

253项Web离线、9项CLI回归通过。另有4项Release发布器原型回归曾通过，但实际CI创建草稿403；该原型已撤回，这些单测不代表线上发布成功。ETM页面/分析源码及历史桥包保持原样。

## 启动策略与Codex设置

按[OpenAI Docs：托管配置](https://learn.chatgpt.com/docs/enterprise/managed-configuration)、[Windows部署](https://learn.chatgpt.com/docs/enterprise/windows-deployment)检查用户/项目/系统配置、注册表RequirementsPath和命令规则。当前聊天的桌面持久配置为Full Access、`approvalPolicy=never`、`approvalsReviewer=user`；未发现项目配置、规则目录、系统requirements或旧managed配置。相关时段CodeIntegrity/AppLocker无事件，Defender两条事件均不涉及本项目。未修改Codex或系统安全设置。

历史调用也没有请求require_escalated，原始工具判决仍只给blocked by policy。不能进一步断言它命中了Codex Auto-review的某条规则。当前不开放镜像目录、不连接目标的同一CLI启动到ready并正常stop；这是受限启动恢复证据，不代表重放了原来包含镜像写入能力的完整动作。具体设置和范围见[startup-settings.json](startup-settings.json)。随后带指定探针的受限启动再次在创建进程前被拒，去掉镜像目录仍拒绝；只读execpolicy check包含待检命令文本时也被同一工具门禁拒绝，没有得到本地分类器输出。没有更换脚本入口或编码来执行已拒动作。ISSUE-044保留具体执行门禁判决待平台诊断。

## Pages与最新包

未改内容的完整重试[42305818553](https://github.com/minichao9901/superlinkPRO/actions/runs/42305818553)再次build通过、deploy约10分钟超时。此前三次失败证据继续保留。约229MB压缩产物低于[GitHub官方1GB站点限制](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)，不能把失败直接写成超过站点上限。

Release原型在CI创建草稿时403（run42308702640，build token日志显示contents:write）；尚未发布任何该原型资产。按用户要求回查历史后，已撤回该替代链路，恢复原来的`web/`静态发布和dist ZIP校验后暂存。当前已备份并重新写入既有workflow配置（PUT204，无删除、无域名变更），见[pages-config.json](pages-config.json)。下一步按历史方式部署web.3并完整核验下载。

### 历史与用户文章复核

`7ac1048`的[2026-10-24源码报告](../20261024-0803-h743-cli-release/source-report.md)已记录同样的CreateProcess之前拒绝，且明确当时为never/user reviewer。更早的[原验收](../20261024-000104-trace-web-cli-9f57c7/report.md)区分了早期worker实测与最终一键启动被拒。本轮不能把已有平台限制写成重构新引入的Web故障。

已实际读完用户提供的[掘金文章](https://juejin.cn/post/7687908992442777651)。其26.915版本案例提示检查PowerShell复合脚本和前置命令策略；本轮单独启动及只读诊断也被拒，故不能仅以拆分脚本认定已找出或修复原因。没有扩大权限、修改ACL或改写已拒动作来绕过策略。

## 包体重复与历史分发复测（ISSUE-048）

历史成功run42142498349的Pages产物128056098B，加入新ZIP后失败产物229303623B。当前workbench ZIP为102544136B；其中CLI复制了完整toolbox，导致同一组samples在两个目录重复，额外5025348压缩字节。此处存在确定的冗余，但GitHub未返回具体后端原因，不能直接断言存在官方未公布的100MB单文件限制。

修复保留toolbox里的全部示例，让冻结CLI服务复用该公共目录；Node所需的cli/web/app与index仍保留。只调整CLI内部资源路径和打包，不修改ETM页面或解码/采集算法。路径、生命周期、样例只保留一份共11项回归通过；新候选还须通过实际冻结CLI无探针启动、样例HTTP回读与退出，再部署按历史同源下载方式验证。

运行时同步的首次GCC调用返回1且无诊断，子进程TEMP/PATH调整未消除。按历史私有`compiler-runtime`前缀重新实际构建通过；cc1plus与系统安装版本SHA一致，没有静默复用旧EXE。新旧解码器.text节逐字节指纹一致，完整运行时来源见[runtime-sync.json](runtime-sync.json)。ISSUE-038继续保留其余C模型待补范围。

## 中文样例漏包（ISSUE-049）

候选归档的数量假设先把总净文件差175误当作样例数；进一步核对已入包180份与Git源184份，发现4份中文路径缺失：花园.mp4和三张引脚图。根因是普通git ls-files的C转义输出被按文本路径使用，is_file静默跳过；改为-z的NUL分隔UTF-8原路径。回归覆盖中文/空格，冻结验收新增独立全清单比对和中文视频HTTP回读。此前不完整候选保留问题证据，不作为本轮最终验收包。

同源ZIP下载链接同时增加SHA-256查询键，使同一版本的包重建后也使用新的缓存键。没有更换历史Pages下载位置。

## 重建站点后的同内容对照

用户提供曾切换private/public的线索，随后澄清日期不确定。当前仓库public、Pages公开workflow、Actions启用，环境仅允许main且匹配发布分支，未发现表面配置拦截。GitHub官方说明Free仓库转private会自动取消Pages发布；因套餐/精确变更时刻未获证据，不把这条规则直接认定为全部故障根因。

重新登记workflow站点后，同一bae6042提交及同一98670156B ZIP的run42314593240成功；重建前同内容run42312746387持续排队后由本会话取消。此前未改内容重试多次失败，故本次成功没有依赖换Web代码或Release分发。见[配置重建](pages-reset.json)、[设置审计](github-settings.json)。后续发布web.4，补齐中文样例并去掉CLI重复GUI bundle，以版本标记和SHA缓存键区分已上线的旧web.3候选。

## 最终web.4本机包

版本1.1.0-web.4，源码`89c5acfa337e60ef8cf82bb2b4c0df0ec557428c`、运行时`10b612b`，sourceDirty=false。ZIP99685437B，SHA-256 `3c0851dbec17f74a51e776bb0c513f9485cd74f13733ee5e45442df43dd3254b`；EXE SHA-256 `73eefbf094e61beca794a19cda3a3dee96f1fe25217043c5b98904d6828906cf`。3407个清单文件匹配，184份公开样例逐项与Git UTF-8原路径/内容一致，CLI样例及GUI bundle重复副本为0。

真实冻结CLI受限启动、中文视频/图片/GUI bundle共享HTTP回读、正常退出通过；没有连接目标或开放镜像目录。冻结原生重解码、查询/导出、完整HTTP下载通过，见[release.json](release.json)。这不代替被拒的绑定探针CLI启动。web.3是过程候选，最终交付以web.4及其指纹为准。
