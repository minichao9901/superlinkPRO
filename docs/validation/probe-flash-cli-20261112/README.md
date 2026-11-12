# 探针固件 CLI 验收

`probe-flash` 使用 dfu-util 访问 HPM5301 探针的应用区。入口要求探针已进入 DFU，按设备 ID、应用区布局、序列号和 USB 端口选定。先备份完整可擦写应用区，再写入并逐字节读回。仅在读回一致后发出退出 DFU 的请求，并确认同一设备重新枚举为运行态。

本次对仓库 APP BIN 执行离线校验，192,848 字节，SHA-256 为 `e0e0f3e74c1bdb19bb7b30db3b9b7cc97f03a185abfe690d8dbd6d694018f0ac`。模拟测试覆盖备份、写入、回读、重启确认、CRC 错误及读回不一致时保留 DFU 模式。真实 DFU 烧录按本轮范围留待复验。历史实板使用的应用区地址和备份长度见 [探针应用升级记录](../probe-app-upgrade-20261108.md)。

对应 ISSUE-139。

17 项 Python 测试、CLI 参考和 Web bundle 一致性检查通过。使用真实 Chromium 在源码服务及冻结候选 `1.1.0-web.16` 点验第 14 个按钮。取消确认未调用 DFU，确认后模拟工具按顺序返回 901,120 字节备份、APP 写入、完整读回和运行态枚举。模拟退出 DFU 返回 74，结果仍要求同一序列号和 USB 路径出现在运行态。冻结 CLI 直接运行 `--dry-run` 通过。

首轮安装遇到 web.15 后台进程占用目录。用户确认继续安装后，核对旧版没有活跃执行页和排队命令，通过独立控制台正常退出。日常入口 `dist/superlinkPRO/superlinkPRO.exe` 已安装 `1.1.0-web.16`，原 `data` 已保留。EXE SHA-256 为 `931cd7e8a6dbf9d83a3530ea7c68d6e5393a01d570974d0af41a100270eb30a7`，ZIP SHA-256 为 `e9c779deb3c73332ee6525d20eeb292ecbb74f01ca7fa219516885738c38de2c`。固定入口的冻结 CLI 离线检查和 Chromium 第 14 个按钮的 DFU 模拟流程复验通过。

## HID 自动切换 DFU

运行态探针通过 USB 描述符及私有 HID 型号查询识别，随后发送一次进入 DFU 的请求。按序列号确认重新枚举，并在可读到运行态 USB 路径时核对路径。已处于 DFU 的设备沿用原有入口。源码 CLI 的 HID 依赖为 hidapi，冻结包随附。dfu-util 仍负责备份、写入和读回。

20 项测试通过。模拟覆盖运行态切换、发送时断开、相同 VID/PID 的 ZynqLinkPRO 排除、HID 型号不匹配、USB 端口不匹配、已处于 DFU 的兼容流程和写入校验。真实探针切换与烧录待验。

`1.1.0-web.17` 冻结候选完整包验收通过。源码 Chromium 测试从运行态点击手册按钮，模拟 HID 型号查询、DFU 指令、重枚举和后续读写，返回 `enteredDfuByHid: true`。候选 EXE 复验已在 DFU 的兼容路径，并在本机用指定的不存在序列号确认 HIDAPI 可加载且只调用设备查询。原始结果保存在本机 `tmp/probe-flash-hid-20261112/`。

2026-11-13，用户确认 VexRiscv 验收结束后，通过专用控制台正常退出 8897 服务，并在固定入口安装 `1.1.0-web.17`。原数据目录的 NTFS 文件身份一致。EXE SHA-256 为 `e4f43c65fe252213818b4059ac1024eb8977f1f606601e80093ad7d5942c8e57`，ZIP SHA-256 为 `29bb25c5817b08cbd3a043fb1a3438fa46f0b712199aebbd03462514b9ad52d8`。安装后复验 `probe-flash --dry-run`、冻结 HIDAPI 目标缺失保护，以及 Chromium 手册按钮的模拟备份、写入、读回和重枚举。真实探针切换与烧录仍待实板验收。
