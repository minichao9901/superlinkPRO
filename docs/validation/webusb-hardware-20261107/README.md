# WebUSB 工作台：F103ZE 真机验收与速度测试

日期：2026-11-07（Asia/Shanghai）；源码提交`484e3eea4f039b8a3a41a84b9e66d8f3d3d7edde`。设备：akaLinkPro，CMSIS-DAP固件报告2.1.2，自定义HID报告0.1；目标F103ZE，DEV_ID0x414、512KiB Flash。只使用当前已确认空闲的这套探针/目标，未争用另一工程的RISC-V仿真任务。

## 本轮结论

烧录、寄存器/内存、原生单步、Flash断点、中断恢复、DWT配置、RTT查看/转发和波形数据通路完成真机验证。验收使用生产WebUSB、FlashView、DebugSession及RTT/Scope协议，通过Node WebUSB适配器或HID/USB原生接收执行；不是模拟目标。浏览器设备选择弹窗未由工具操作，实际浏览器布局和离线交互沿用[上一轮验收](../web-refactor-20261106/README.md)，不把原生适配器验证冒充浏览器授权流程。

发现并修复三个迁移问题：

1. ISSUE-036：通用PPB双读会清掉DHCSR的S_RETIRE_ST，导致已执行的自跳转单步误报失败。单步专用读取改为锁内单次物理读，保留故障隔离和清理。
2. ISSUE-041：Trace CLI烧录仍访问已移除的f-backend/f-path。删除这两处依赖，新增缺少旧控件时的回归。
3. ISSUE-042：旧akaLinkPro的CLOCK/STOP回包可能保留-100启动状态。仅在完整状态回读确认指定频率、或确实停止时接受；错误频率、仍在运行及短响应仍拒绝。

## 功能与恢复

| 项目 | 验证与结果 |
|---|---|
| 目标身份 | DPIDR0x1ba01477、Cortex-M3、F103ZE/512KiB，6个Flash断点比较器、4个DWT观察点 |
| 原生单步 | SRAM中MOV/ADD两条正常指令及b .自跳转均以dhcsr模式完成；后者PC不变但退休状态证明执行 |
| 调试现场 | 寄存器写回、RAM字节、FPB、核心寄存器和时钟恢复逐项通过；随后SysTick断点命中，确认中断未被单步屏蔽残留阻断 |
| 烧录 | 不以ELF文件长度计写入量；实际写入约19.7KiB，三轮校验与复位运行通过，墙钟2.951/2.970/2.789s。另烧入72MHz RTT/Scope夹具，结束恢复Golden |
| RTT Viewer | 72MHz夹具、20MHz请求档三轮序号连续，约0.55MB/s；25MHz未更快；30/45/60MHz有ACK失败，不能推荐为该运行负载的稳定档 |
| RTT转发 | 72MHz夹具、请求60MHz、2048B块，三轮约2.337/2.327/2.324MB/s，序号缺口0；收到字节与停止后的探针moved计数一致 |
| 单变量波形 | 72MHz夹具、60MHz SWD、4us/250kSa/s，三轮数据包连续、重同步0、USB/SWD错误0；跳采23/39/25，对应约31/52/33ppm |
| 波形边界 | 3us约333kSa/s但约1068ppm跳采；2us请求500kSa/s实际约376k且错误计数非零。八变量10kSa/s无跳采/USB/SWD错误，50kSa/s有非零USB计数。高档不能当作零损失稳定档 |
| 停止尾部 | 多组停止后的produced与已发布样本有小于一包的尾差（单变量56～72，八变量6）；原始状态与计数保留，另列ISSUE-043待复核，不以包序号连续掩盖尾差 |
| 最终目标 | CDC48 Golden已恢复，512KiB完整Flash SHA与验收前一致；RCC回读相同、HCLK按HSE8MHz计算48MHz、CFSR/HFSR0、运行中；重启目标USB接口后COM34的PING/PONG通过 |

最终完整Flash SHA-256：`48f226217497607c696a156addbb363925577499b00ee64f187bd5eea7a94499`。RTT/Scope夹具RCC分别回读计算72MHz；这些是寄存器计算，不冒充外部频率计实测。目标USB在切换无USB夹具后出现Windows端口错误31，仅重启该目标USB设备后恢复；没有重装驱动。

## 最佳速度的计时边界

SRAM基准保持原CDC48/HCLK48MHz，目标暂停，20KiB线性区域，读写后逐字节比对并恢复原区域。频率均为请求SWCLK，未做逻辑分析仪外部测频。

| 请求档 | 修改版OpenOCD读/写 MB/s（三轮中位） | 生产WebUSB读/写并校验 MB/s（三轮中位） |
|---|---|---|
| 10MHz | 0.857 / 0.852 | 0.408 / 0.175 |
| 20MHz | 1.489 / 1.511 | 0.539 / 0.209 |
| 30MHz | 2.069 / 2.133 | 0.546 / 0.231 |
| 40MHz | 2.368 / 2.467 | 0.551 / 0.231 |
| 45MHz | 2.749 / 2.885 | 0.555 / 0.237 |
| 50MHz | 2.749 / 2.885 | 0.582 / 0.226 |
| 60MHz | **3.277 / 3.501** | **0.623 / 0.241** |

OpenOCD计时包括dump_image/load_image及本地文件IO；WebUSB计时为现有readMem/writeMem调用，写包含回读校验，两列不能当作相同纯传输口径。修改版来自zynqlinkPRO的Golden host包，二进制SHA `cb0d4ef35ce090b4ca59281eb5d4ef34b4f64efcbedebe484c2b6f3ce2a182bc`；补丁将请求上限4改8、分块4KiB改64KiB，本探针仅公布4个包槽。保留二进制来源/补丁指纹，不把编译差异全部归因于异步补丁。

原版nightly cb0c224二进制SHA `b52c7643b08c8e450d081e95d3565138f163f56dff066358b362e9bfbbbed631`：10MHz三轮约0.822/0.852MB/s；升45MHz及独立60MHz测试失败，均恢复SRAM。原版高频失败不能被从表中删除来制造全通过，也不能用本次结果宣称任意板型均有同等提升。

首个测试脚本误用了verify_image，OpenOCD校验辅助代码占用被测SRAM，导致比较失败。该次失败单独保留；后续禁用work area，全部按主机字节比对，不执行目标CRC辅助代码。

## 启动拦截定位

已排除工程启动异常：工具返回的是CreateProcess之前的策略拒绝；用户手工执行相同命令后原生host/worker正常ready，后续status/command均可执行。不是Python异常、USB占用或Windows启动器报错。当前证据将问题定位到Codex工具执行审批层；该层只返回blocked by policy，没有规则ID或详细判决，不能编造更具体根因。读取当前聊天终端诊断也返回未附加终端。没有修改或关闭审批、安全设置。

[官方自动审核说明](https://developers.openai.com/api/docs/guides/agents-api/sessions/approvals)说明审核可能批准或拒绝动作，浏览器来源访问另有控制；它不提供本次命中规则。ISSUE-044继续保留平台判决细节待定位。

WinUSB是另一独立问题ISSUE-040：当前探针MI_00已有WINUSB服务却缺DeviceInterfaceGUIDs。备份后仅补该实例的固件声明GUID并重启该接口，Node/libusb/OpenOCD均恢复。随后完整读回650B MS OS 2描述符，长度与声明一致、包含GUID；当前描述符有效，但旧注册缺失形成原因未证实。

## 证据与复现

精简计数/失败/恢复结果见[summary.json](summary.json)。固定测试ELF见[夹具清单](../../../web/tools/fixtures/f103ze-acceptance/manifest.json)，包含精确来源及SHA；夹具来自zynqlinkPRO `b5d3f3de`的额定72MHz版本，本次没有将目标超频到96MHz。

可复用验收入口：`tools/validate_f103_workbench.mjs`、`validate_f103_flash.mjs`、`validate_f103_rtt.mjs`、`validate_f103_streams.py`、`analyze_f103_scope.mjs`、`benchmark_f103_openocd.py`、`benchmark_f103_webusb.mjs`、`read_f103_state.mjs`。它们会实际读写/暂停/烧录目标，执行前需确认设备空闲，不属于离线自测。

本次251项离线Web、9项CLI通过。旧固件C模型编译环境ISSUE-038未因真机结果关闭。ETM页面及其分析/采集实现未改；未测Kingst采集、SPI/I2C/ADC等未接外设，也未将F103结果推广到H743或RISC-V。NRST实体接线专项ISSUE-015仍保留。

## 清理与交付方式

[清理清单](cleanup.json)中的12个本次目录、25297个文件、约3.91GB已移入Windows回收站，原位置残留0；这是可恢复移出工程，不声称已释放对应物理磁盘空间。原始Flash/SRAM/注册项及必要原始失败输入已校验保存在`E:/superlinkPRO-backups/20261107-webusb-acceptance/`。历史.formal-trace、ETM原始记录、基线和其他会话目录不动。永久递归删除被工具审批拒绝后改用回收站，没有绕过拒绝进行不可恢复删除。

源码先独立提交，再从该提交构建web.2，Git只保存完整分发ZIP和manifest；本机展开运行目录及data排除。Pages工作流核验后暂存这两个分发文件，在线下载页无需重新引入工程生成器。最终版本身份和候选/安装版冻结检查见[release.json](release.json)。

最终分发版本为`1.1.0-web.2`，3580个清单文件全部与源码输入/包清单匹配；安装版CLI查询、SVG导出、HTTP原生重解码及完整HTTP下载校验通过。分发ZIP为102544780字节，SHA-256 `efff944ef36f764d8ef0508ae14919400c869426498bfdc86708fae090d08398`；EXE SHA-256 `6e8c98cd18c86c9c9df1d3e6c33806975235c1f06590cb1c27dacaa16518d357`。冻结验收没有再次访问硬件，真机范围按前文原生生产适配器记录。静态分发包与EXE动态重建下载包分别按各自清单核验，不要求ZIP压缩/时间戳相同。
