# 探针资源仲裁架构（2026-10-04）

页面现在使用一个 `ProbeManager` 管理探针资源。连接、启动、交接、模式切换进入控制队列；RTT/J-Scope/SPI 的连续数据收发不进入该队列。

## 职责分层

| 层 | 代码 | 负责的事情 |
|---|---|---|
| 资源所有权 | `app/core/probe-manager.js` | 串行完成申请和初始化，释放冲突使用者，取消排队请求，保护不可抢占操作，保留停止失败的占用 |
| 功能声明与清理 | `app/core/probe-users.js` | `PROBE_FEATURES` 集中声明资源、关闭适配、注入对象、USB 复位身份和显示名称；视图不再承担应用内的交接顺序 |
| HID 命令通道 | `app/hid/probe.js` | 同一 HIDDevice 的请求/响应共享队列，多个客户端共享句柄生命周期；跨标签页用 Web Locks 保护命令往返 |
| USB 生命周期 | `app/core/usb-device.js` | 共享设备、接口、端点的占用，保护整设备复位，保留未退出的 native 操作 |

`app/main.js` 在各功能 `init()` 之前调用 `installProbeManager()`，依据功能表完成注册、管理器/总线注入与复位保护安装，自动连接入口同样受管理。`window.__tools.probeManager.summary()` 可查看持有者、排队请求和释放失败原因。

调试器内部的 `exclusive/tryExclusive`、RTT 内存事务锁、各会话的启动/停止状态及代次检查继续保留。它们负责功能内部操作和迟到结果，资源仲裁器负责功能之间的所有权。

## 当前资源策略

| 功能 | 持有的主要资源 | 生命周期 |
|---|---|---|
| 调试器 | 目标访问引擎、调试引脚、DAP bulk 接口 | 连接完成至断开 |
| RTT Viewer | 目标访问引擎、调试引脚、RTT 环、DAP bulk 接口 | 连接/扫描至断开 |
| J-Scope | 目标访问引擎、调试引脚、Scope bulk 接口、采样流 | 连接至释放探针；停止采样后保留连接 |
| RTT→CDC | 目标访问引擎、调试引脚、RTT 环、CDC 模式 | START 发出后至确认 STOP；固件尚在排队也保留所有权 |
| SPI/QSPI 与点屏 | SPI bulk 接口、SPI 引脚、可能重叠的 I2C 辅助引脚 | 两个页面共用一个 SpiSession；长时间忙操作拒绝抢占 |
| I2C | 固定 I2C 引脚 | 连接至断开；自动 ENABLE 包含在连接初始化里 |
| 烧录/读身份 | 目标访问引擎、调试引脚、RTT 环、DAP bulk、CDC 模式 | 准备至最终清理；操作期间不可抢占；独立 SPI/I2C 可以保留 |

资源表针对当前 HPM5301EVKLite 的引脚布局：I2C PA28/PA29 与默认 SWD 引脚独立，SPI2 PB10..15 与默认调试 PA04..08 独立。SPI 辅助引脚可选择 PA28/PA29，因此目前 SPI/I2C 保守互斥。更换板级引脚布局时必须同步更新声明，不能沿用这个共存结论；尚未实现按当前配置动态认领具体引脚。

**不同 bulk EP 现在可以在同一个 USBDevice 上共存。** 共享层按设备、接口和端点分别登记引用：关闭 DAP、Scope 或 SPI 只释放自己的接口，最后一个使用者退出时才关闭设备。整设备 `reset` 仍是全局操作；有其它使用者、未收尾的 native 请求或旧固件不支持收尾命令时，复位会被拒绝并保留故障占用。

SPI EP11 停止收流时先发送 `DRAIN`，让固件为每个挂起的 IN 请求发一个短应答，再确认所有 native 读已结束；随后确认 `ENABLE 0`，关闭桥的执行后才交还主机资源占用。该操作不代表固件将所有 GPIO 自动恢复为高阻，下一功能仍需配置自己的引脚。旧固件没有 `DRAIN` 能力标志时，网页不会假装完成，仍按故障路径保留占用并提示更新固件。I2C 断开也会先确认 `ENABLE 0`，避免只关闭 HID 句柄却让桥继续执行。

串口助手、终端和 RTT→CDC 接收视图继续共用原有 `SerialSession`，不对串口数据逐包加锁。J-Scope 的 `CDC_OFF` 仍是显式采样选项，会暂停固件 CDC 服务，不能把这种配置影响解释为仲裁器开销。

## 控制入口

```js
await runProbeOperation(this, 'scope', () => this._startOnce(generation), {
  reason: 'J-Scope 要开始采样',
});
```

默认 `handoff` 策略先等待冲突会话完整退出，再执行初始化。改目标类型、标定和运行时配置使用 `policy: 'reject'`，不会抢占其他功能。停止入口先 `cancel(owner)`，使还在排队的 START 立即结束；已执行的初始化由原会话状态机取消并排空。成功断开后 `forget(owner)`。

`run()` 返回初始化函数的结果；传给函数的 lease 提供所有者、取消信号和 `assert()`。所有权在初始化后继续保留，直到实际会话释放。不要从初始化函数里再次调用同一个管理器排队：内部自动连接走不重复申请的私有方法。

释放失败会登记故障并继续阻止冲突申请。原功能可恢复连接，再执行并确认 STOP；成功后清除故障。重复释放共享同一清理 Promise，避免另一个功能在 USB 关闭尚未完成时开始认领。

烧录与读身份共用 `_withProbeOwnership()` / `_closeProbe()`。关闭失败时保留原探针句柄和故障占用，禁止其它目标访问功能接手；再次点击烧录或读身份会先重试旧句柄关闭，成功才开始新操作。原操作和清理同时失败时保留两份错误。故障恢复不会要求独立 I2C 停止。

## 冗余清理与新增功能

视图的兼容交接通过 `prepareProbeHandoff()` 共用一条路径：无管理器的独立演示先释放本页使用者，再请求跨页交接；完整应用已经在 `run()` 内仲裁，该适配不会再次清场。RTT Viewer 和烧录视图中重复访问其它功能并停桥的代码已移除。直接使用底层 DebugSession 的兼容入口仍保留；它的硬件初始化也不能用主机资源登记替代。

新增业务会话和视图之后，将实例加入 `tools`，在 `PROBE_FEATURES` 增加一份描述：

```js
{
  id: 'logic', label: '逻辑分析仪',
  client: t => t.logicSession, view: t => t.logic,
  usbKind: 'logic', resources: ['logic-pins', 'logic-stream'],
  active: t => !!t.logicSession?.connected,
  release: t => t.logicSession.close(),
  guarded: t => !!t.logicSession?.busy,
}
```

`usbKind` 应与该传输创建 `UsbLease` 时的 owner 相同；复用 DAP 的功能填 `dap`。注册、注入、复位保护都从这份描述生成，核心仲裁算法不需要增加功能 ID 分支。入口用 `runProbeOperation()`，关闭成功后 `forget()`，失败用 `fail()` 保留占用；STOP 前取消排队启动并等待本会话正在执行的初始化完成。共享硬件应声明相同资源名，独立硬件声明不同资源名。专用冲突提示由调用方的 `conflictMessage` 提供，通用仲裁层不再包含 CDC 或烧录器的业务分支。

调试器内部事务锁、I2C 事务队列和代次检查分别保护多命令事务与迟到结果，不属于重复的跨功能仲裁。高速数据面继续由各传输实现，不添加统一逐包锁或通用会话基类。当前仍不支持完整多探针资源池。

## 跨页面与 HID

同源标签页通过 Web Locks 串行控制交接，结合原 `ProbeBus` 的释放请求、确认和失败回应。收到交接请求时，会取消等待控制锁的启动任务，避免双方各等对方释放。

HID 的被动型号/状态查询也必须经过命令锁，否则另一个标签页的相同命令响应仍可能错配。WebHID 缺少标准的稳定设备 ID：没有序列号时，跨页面 HID 锁保守按 VID/PID 分组。一个页面关闭 HID 客户端时，仅在最后一个本地客户端退出后关闭共享句柄。

失效 HID 请求不会在释放后自动重新认领设备；连接恢复通过重连/功能启动的连接准备完成。超时和关闭能结束调用方的等待，但不能取消浏览器底层的 `sendReport`。同页面共享通道在未收到旧响应时禁止继续发送，只有旧响应被消费且底层写 Promise 已结束，或物理拔插结束旧会话，才恢复通道；单纯关闭再打开不会清除该故障。最后一个 HID 客户端关闭失败时保留句柄和注册，上层保留功能占用并允许重试。

当前应用仍按页面默认的一台探针管理资源，没有完整的多探针资源池。不同来源和外部 OpenOCD 等程序不在同源协调范围内。缺少 Web Locks 时保留同页面串行化及 BroadcastChannel 交接，但不能提供跨标签页同时申请的原子性保证。HID 协议没有事务编号；故障隔离登记仅在同页面共享，超时释放跨页面锁后，其他标签页仍可能受迟到的同命令响应影响。因此不能宣称跨标签页超时后响应匹配完全可靠，遇到未同步提示应等待旧响应或拔插探针。

## r5 发布前异常路径修复

本轮逐项修复并补充回归：损坏 ELF 的表和段边界检查；ARM/RISC-V 调试器关闭失败保留后端；记录文件选择、排空和关闭的并发互斥；异步写失败及断开时的错误提示；文件提交前的离页保护；共享 HID 迟到响应隔离和单次派发；I2C 真机切模拟先确认失能；SPI 采集代次隔离；OpenOCD RPC 超时销毁失去同步的连接；内存短读拒绝补零。收尾检查同时修复 SPI/J-Scope 吞掉 HID 关闭失败的路径。

新回归文件 `recorder-lifecycle`、`i2c-lifecycle`、`spi-runner-lifecycle`、`bridge-rpc`、`bridge-memory` 全部进入 `test-stability`；现有 ELF、调试、HID、RTT 和采样生命周期用例补充故障场景。桥源码变更已重新生成网页安装包，使用 `bridge-kit` 用例检查嵌入内容一致性。页面构建号更新为 `2026-10-04-r5`。

补丁导出时待验收的项目包括截断 ELF、记录写失败、HID 迟到响应及拔插、I2C 切模拟、SPI 脚本重启、OpenOCD 超时重连和断开重试。2026-10-04 本机已使用在线 akaLinkPro + STM32F103CB 完成主要硬件路径，后续补做 AT24C02 整片读取、W25Q64 单线读取、20 轮外设交接与调试共存、10 轮 Scope→RTT 转发→调试交接与 I2C 共存；详见 [逐项验收记录](validation/2026-10-04-f103cb.md)。网页生产模块通过原生 HID/USB 测试适配器或 OpenOCD 访问实机，浏览器本身仅验证模拟界面。浏览器原生 API 的授权、文件提交、跨标签页实机并发、后台运行、物理拔插、长期压力、外设写入/擦除仍需对应场景验收。W25Q64 的 IO2/IO3 未接到探针，未验收四线模式。

## RTT STOP 的固件配套

5301 固件 `rtt_bridge_service_requests()` 处理 STOP 时，现在清除之前排队的 START，并在主循环发布 `start_rc=0`。否则同一次 service 会先停桥再执行旧 START。

网页在 START 尚未确认时保留 `_bridgeRequested`；STOP 后持续检查 `running/startRc`，等待排队启动被处理或取消。此路径应配合固件提交 `5d0bcd6` 使用。旧固件遇到 START/STOP 排队竞态可能被网页拒绝继续交接，需要更新固件。

## 验证与测速

离线回归入口：

```sh
make test-probe
make test-stability test-dbg-features
make test
```

新增测试覆盖资源冲突与共存、并发初始化、排队取消、重复释放、停止失败、烧录保护、跨标签页同时申请、共享 HID 响应与句柄、停滞写请求、固件 START 排队期间的所有权。功能声明扩展、ProbeBus、USB 生命周期/传输、CDC 模式和 SPI teardown 均已纳入 `test-probe`，通过 `test-stability` 自动进入 `make test`。SPI/I2C/RTT/J-Scope 协议及传输回归继续通过。

本轮结果：资源交接、EP11 收尾、CDC、调试器/RTT/烧录/读身份关闭失败恢复、新功能声明扩展等离线回归通过；JavaScript 语法检查覆盖 222 个模块。固件 RTT STOP、SPI DRAIN、TARGET guard 主机测试再次通过；此前 scope 生产代码主机测试及 HSS 测试通过。

补丁导出环境的 `make test` 通过；`rtt.test.mjs` 中依赖外部 ESP-IDF 本地路径的 ELF 子用例因文件不存在而跳过，其余离线用例执行。222 个模块语法检查和 54 个 Markdown 的 Liquid 检查通过。导出环境未连接 F103CB；本机验收与吞吐数据单独记录在上述验收报告中。

原有 `dbg-core.test.mjs` 错误依赖本地 `build-noncache/fw.elf`。现已改用仓库提交的 `tools/target-firmware/stm32h743_scope/fw.elf`，验证 184 条 H743 行号记录，调试核心测试 321 通过、0 失败。该用例检查 DWARF 序列，与 D-cache/MPU 构建变体无关。补丁导出时只有 GCC 主机测试；本机另已完成 HPM5301EVKLite 探针固件编译和上板验证，不能由主机测试结果推定其它板型通过。

此次收尾未修改高速 bulk 读取、RTT DAP 内存读取、采样存储和 SPI 数据传输逻辑。仲裁在控制边界执行，没有逐样本/逐 bulk 包的全局锁。HID 控制命令排队和跨页面锁的开销可能影响启动/切换延迟；同时进行其他固件任务也可能占用 CPU/USB 带宽，速率仍须实测。

回家后固定同一目标、固件、时钟、变量、周期和采集时长，对比：

1. J-Scope 单变量 u32：有效采样率、丢样率、USB 缺包和连续运行稳定性。
2. RTT Viewer 与 RTT→CDC 分别运行：有效吞吐量、数据完整性和长时间停启。
3. 调试→RTT→Scope→烧录→SPI/I2C 切换，包含快速重复点击与启动期间取消。
4. 两个标签页同时启动，以及运行期间由另一页请求交接。
5. I2C 与采样共存时单独比较吞吐量；这项测的是额外工作负载的影响。
6. RTT START 尚在排队时立即 STOP，确认之后不会自行再次启动。
