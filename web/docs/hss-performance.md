# HSS 性能改动与上板验收

本次改动尚未上板测速。完成了固件短批次/小数周期，以及网页收流和 u32 入库优化；
速率目标不是实测结果，推荐周期仍保留保守规则。

## 周期与兼容性

支持 2.25 / 2.5 / 2.75 µs，按24MHz timer tick量化，下限仍为2µs。
整数周期继续用旧 action7 / v1包；小数周期用 action10 / v2包。
发送小数周期前检查 STATUS w0 bit2，旧固件要求升级。
v2包头时间戳和 DEF/STAT 周期均为tick；先去u32回绕再转换到µs，约179秒回绕。
原始包回放同时支持v1/v2。完整协议在探针仓库 docs/hss-tick-protocol.md。

## 短批次开关

“单字短批次（实验）”默认开启，只有固件 STATUS bit3 支持时才发送 flags bit7，
旧固件自动清除此位。单字 SWD、周期<=3µs 时一次最多16拍，逐拍等deadline、
包边界返回；其他计划保持逐拍路径。它改变主循环服务间隔，可能增加其他桥的响应延迟。
取消开关可在相同周期下做 A/B 对比。尚未按此改动下调推荐周期。

## 网页数据路径

保持3条4096B bulk IN；一条返回后先续挂，再同步解析，减少主机请求队列的空窗。
重挂请求被停止/错误收尾跟踪，旧轮次回包不进入新采集，STALL仍需clearHalt。
单通道u32且触发关闭时，直接按包写入类型化缓冲，避免逐样本解码数组/slice。
整数精度、LOD包络、时间戳锚点、满缓冲计数保持一致；其他类型或触发使用通用路径，
复用一个帧数组。原始包录制仍保留，性能测试时应分别比较录制关闭/开启。

本轮没有引入Worker。先用性能录制确认解码/LOD或绘图是否仍占主线程的大头，
再决定是否迁移；USB先续挂已经独立提交，可单独比较收益。

## 上板顺序

使用本分支配套固件，单个已知u32地址、相同SWD时钟和线材，暂停CDC。
每档分别比较短批次关闭/开启，从3、2.75、2.5、2.25到2µs。
先5秒验证，再对候选档跑30秒×3；记录实收速率、scheduler skip、USB buffer
exhaustion、SWD errors、DAP yields、seq缺口和页面流畅程度。
对比探针DISCARD时用相同配置；3µs名义上限333.33kHz，不应由M0/333比值推算CPU开销。

完整命令和功能回归清单在5301evk_akaLinkPro/docs/hss-benchmark-checklist.md。
Python run 仍是同步USB读，不能据此判断WebUSB上限。新action11探针快照使用完整u32
计数和timer窗口，主机用到达窗口；启动和drain均不混入速率，窗口差值不是精确丢失量。

## 离线验证

```sh
node tools/selftest/scope-proto.test.mjs
node tools/selftest/scope-transport.test.mjs
node tools/selftest/scope-store-batch.test.mjs
node tools/dev/check-syntax.mjs
node tools/dev/check-liquid.mjs
```

make test / make test-scope 已包含三个 scope 测试。它们验证协议、数据一致性和模拟
USB状态机，不验证浏览器原生USB实现、板上时序或实际吞吐。固件完整构建需HPM SDK
和RISC-V工具链，本次环境没有提供，未生成新的可烧录固件。
