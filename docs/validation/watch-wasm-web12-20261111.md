# superlinkPRO 变量监视与 OpenOCD Wasm 更新

2026-11-11（北京时间）。从 zynqlinkPRO 提交 02bf554b 移植变量监视格式、按类型编辑及 p/x。保留 superlinkPRO 的调试后端、CFG 导入、断点和观察点实现。变量表默认十进制，可切换十六进制。暂停目标后双击数值，Enter 写入并逐字节回读，Esc 取消。只读项和越界数值拒绝写入。位域按当前存储字节读改写，保留相邻位。切换 ELF 时丢弃旧值缓存，重连后的旧编辑请求会被拒绝。

正式 Wasm 使用 zynqlinkPRO 已验证的候选。Wasm SHA-256 为 1977164ce62a8595753be9dfa741751c0b3f2459a95aa6bd03f25afe72d554fe，配套 JS SHA-256 为 5a55691c948f503d13bc233902d4a74782bee82e7276d9ad7d52d8394053700a。构建入口固定为候选实际使用的 OpenOCD 9f2d876，并在编译前应用完整流水线补丁。原有 RISC-V SBA 合批继续生效。

65 项定向 JS 回归和 10 项 CLI 回归通过。隔离 Edge 使用模拟目标和 F103 ELF，实际验证十进制/Hex 切换、双击编辑、输入溢出、Esc 取消和 p/x。写入 0x12345678 后回读 78 56 34 12。首次扩大测试集时混入了要求浏览器参数或独立目录的测试入口，随后使用正确工作目录和离线范围复验。本轮的变量写入验证使用模拟目标。

同步 ETM 快照时默认 GCC 重建解码器返回 1。复用历史记录中的私有编译器路径后重建通过，cc1plus 与系统安装的 SHA-256 一致。参见 ISSUE-038。变量功能跟踪为 ISSUE-136。

Windows 正式包为 1.1.0-web.12，源码提交为 7b1cc2ab2365f04e209427e08879291a527e1545，构建清单的 sourceDirty 为 false。工作区中已有的 ISSUE-103 修改及未跟踪的 Hazard3 报告继续保留。候选完成冻结启动预检、HTTP、CLI 鉴权往返、示例重新解码、查询、SVG 导出和独立 CLI 服务的示例资源核对。3410 个程序文件均与清单一致，ZIP 中的全部文件也逐项核对大小和 SHA-256。

用户退出旧版后，将已验收候选安装到 dist/superlinkPRO，并更新根 dist 中的正式 ZIP 和 manifest。安装前后的 66 个原有数据文件逐文件 SHA-256 一致，配置文件一致。旧程序归档到 tmp/workbench-package-9c0ba1c0/previous，可用于回退。正式产物如下。

- ZIP 大小为 103194223 字节，SHA-256 为 985dac5da605d7500c2ed20fba4266387a786392fa261a64835fa878d4c75aa9。
- EXE SHA-256 为 e1a2c42fb03289e0f65c399ca26c0ecad8e039927572b38fbee0d94ff1ca45d0。
- packageId 为 3a7c41cab3d267a8b0aabcb6fad8f273a24b8ea8a51db74d64182fa702c9d405。

安装后再次使用独立临时数据目录验证默认安装的 EXE，全部冻结包检查通过。真实 Edge 从该 EXE 的 HTTP 页面验证十进制、Hex、修改后回读、溢出拒绝、Esc 取消及 p/x，再次得到写入字节 78 56 34 12，HTTP 提供的 Wasm 哈希与上述正式候选一致。Edge 首次因系统兼容层重启而先退出启动父进程，验收驱动显式跳过该重启后通过。桌面程序动态生成的重下载 ZIP 采用独立压缩流程，其 HTTP 清单和下载内容自洽；根 dist 的正式分发 ZIP 按上列哈希单独核验。

本轮变量写入使用模拟目标，实板验证仍待开展。软件验收通过后关闭本任务测试服务和浏览器。清理操作两次被自动审批拒绝，返回原因仅为 blocked by policy，因此本任务构建中间目录、浏览器配置及测试数据仍保留。具体目录为 web/tmp/etm-package-a3d3ad6b、tmp/workbench-package-9c0ba1c0，以及系统 TEMP 下的 slpro-watch-sync-bb7576a1。workbench-package 目录中的 previous 是旧版回退副本。正式安装、正式 ZIP 和报告已就位。交付范围为本机与 Git，线上发布需另行执行。
