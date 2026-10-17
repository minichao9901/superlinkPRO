# 调试、烧录、RTT、J-Scope 稳定性修复

本轮基于网页 `21b6919`、探针固件 `c151238`，按独立问题分别提交。

## 已修复

- 烧录从准备到连接关闭全程排斥重复请求；读取身份也在关闭连接后才解除忙碌状态。
- 烧录交接先取消调试器待执行命令，等待当前动作退出，再关闭 USB。
- 同页调试、RTT Viewer、RTT→CDC、J-Scope 共用交接入口；烧录忙时其他功能不能接管。
- 跨页签交接按请求身份匹配响应；正在烧录的页面拒绝释放；失败/未完成不能报告成功。
- 调试器独占状态覆盖排队动作；内存、监视以及命令收尾刷新遵守同一把锁。
- ARM/RISC-V 切换等待旧会话退出；按钮在点击时读取当前 session。
- REGRDY 超时明确失败，禁止返回陈旧 DCRDR；flashloader 初始化 R9/static_base。
- RTT 扫描与轮询绑定会话代号，旧数据/旧错误/旧定时器不能影响重连后的会话。
- RTT 复位先排空读；USB 断开期间不接受新事务，不允许迟到超时重新打开连接。
- RTT→CDC 的 STOP 等待探针完成；待启动流程可取消；后台状态查询避让用户动作。
- J-Scope 启动、停止、让出探针有独立状态与代号，STOP 可以取消正在等待 STATUS 的 START。
- 正常停止先取消读的重挂载，让生产者完成已提交读；不必每次启停都 reset USB。
- 未退出的 native USB 读在重启前通过 reset/close 清理并等待结束；更换 transport 仍保留这笔账。
- J-Scope 看门狗按每包样本数与周期计算，慢采样不会被固定 2.5 秒阈值误杀。
- 网页禁止运行中切后端和采样中做标定；固件拒绝运行中全局后端切换，RTT action 10 返回 -7。

## 离线检查

`make test-stability` 覆盖延迟 CONFIG/STATUS、重复操作、停止失败、陈旧 USB 请求、重连旧数据、
跨页签拒绝、后端交接、寄存器握手及算法 ABI。它们不需要硬件。

固件：

```sh
python script_test/target_switch_host_test.py
python script_test/scope_host_test.py
python script_test/test_scope_hss_test.py
```

`dbg-core` 原先错误依赖本地 `stm32h743_scope/build-noncache/fw.elf`。
后续已改用仓库提交的 `stm32h743_scope/fw.elf`：DWARF 序列检查不需要非缓存构建变体，当前为 321 通过、0 失败。

## 回家上板验收

1. 烧录准备阶段快速重复点击：只出现一轮擦写；关闭连接后才能再次烧录。
2. 调试器连接、运行/单步、刷新内存/监视，再切烧录：旧观察循环退出，不继续抢探针。
3. ARM/RISC-V 来回切换后，暂停/继续/复位按钮操作当前目标。
4. RTT Viewer 与转发互相切换，连续启停至少 20 次；检查输出序号、重复前缀与 RdOff 错误。
5. J-Scope 连续启停至少 30 次；启动中停止、断线重连，不应显示虚假的“采样中”。
6. 单 u32 周期 100 ms，采集 30 秒以上：正常满包间隔约 12.4 秒，不应在 3 秒左右误停。
7. 固件运行中通过原始 HID action 10 切后端应回 -7；停止后切换应成功。
8. 按原 HSS 测速清单对比 3/2.75/2.5/2.25/2 µs，记录交付速率、probe dropped、USB dropped。

高速数据循环没有新增逐样本或逐包的异步等待；新增交接检查主要位于连接与启停路径。
吞吐、USB reset 后 HID 重取、真实芯片 flashloader 行为仍需上板验证；离线测试不代替这些结果。

## 本轮验证结果

- `make test-stability`：15 组针对性回归全部通过。
- 原有 RTT/HID、J-Scope 协议/传输/存储、烧录解析/HPM、DWT/bt、探针协调共 12 组检查：
  初次验证时 11 组通过；`dbg-core` 为 318 通过 / 1 失败（路径指向未提交的构建产物）。后续修正 fixture 路径后，`dbg-core` 为 321 通过 / 0 失败。
- JavaScript 语法检查：205 个模块通过；Liquid 检查：53 个 Markdown 通过。
- 算法表离线校验通过；固件 TARGET 拒绝、采样器主机测试、8 个测量脚本测试通过。
- 当前环境没有设备和 HPM SDK，未执行真实 USB、上板吞吐或完整固件构建。
