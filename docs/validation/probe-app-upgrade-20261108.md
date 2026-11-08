# akaLinkPro A.0 第二只探针应用升级

2026-11-08（北京时间）。设备序列号末八位 `0AE800E0`，升级前后 USB 运行态均为 `0d28:0204`。升级前 HID 读到应用版本 `0.1`，构建时间 `2026/10/18 11:30:22`。确认 DAP 接口可认领，RTT 和 HSS 状态均为空闲。

使用探针应用源码 `ee00aa3` 的既有验收包，包长 194,088 B，SHA-256 为 `5d32f353f91dc8b38074179d5aed0dc0cc2c5bd7644567d9c092c5a406aac3ed`。写前检查包头和应用 CRC。HID 进入 DFU 后按相同序列号和 USB 路径限定 `0d28:0207`，从应用起址 `0x80020000` 备份 901,120 B。提取的可回退应用包为 192,712 B，包头和 CRC 通过，SHA-256 为 `3ce67484c28ae56ce0884af49c09c5ecafb456216a1b3e04e98efcbf656fd895`。原区域与回退包保存在本机 `tmp/probe-upgrade-20261108-91d417e2/`。

以已核验的 dfu-util 0.11 写入应用区，完整读回 194,088 B，与验收包 SHA-256 相同。随后提交 DFU 结束请求，探针在状态应答期间断开并重启。相同序列号重新进入应用模式，HID 确认应用仍标记 `0.1`，构建更新为 `2026/11/07 14:01:44`，硬件 A.0、Bootloader 1.0 及其原构建时间均正常读回。Windows 枚举 CMSIS-DAP、WebUSB、DFU Runtime 和 COM52。本轮验证范围为探针应用写入、读回和启动。

[固件验收来源](hss-spicdc-hw-20261107/README.md) · [DFU 工具与包指纹](https://packages.msys2.org/packages/mingw-w64-x86_64-dfu-util)
