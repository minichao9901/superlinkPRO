# SuperLinkPRO EVKLite APP 构建与发布

以 SuperLinkPRO 主线 `342b0fdd76396293b8273a61f55e98f22337ed24` 的干净源码构建，最新固件源码变更为 `b474b93`。使用 HPM SDK 1.11.0、RISC-V GCC 13.2.0，CMake 目标 `hpm5301evklite`，采用 `flash_xip` 构建类型及工程自带 DFU 链接配置。

| 项目 | 已核对的值 |
| --- | --- |
| APP 版本 | `0.1` |
| 包内编译时间（北京时间） | `2026-11-15 14:07:29` |
| 包地址／ELF 入口 | `0x80020000`／`0x80020100` |
| DFU 文件 | `akaLinkPro_App_pack.bin`，195,080 B |
| CRC32 | `3dfbfae4` |
| BIN SHA-256 | `e7ec53719a7a697b8c004c89a9e621092b2952270048d42e09d4fc8efa24e5f7` |
| 产物提交／标签 | `493b0ed`／`probe-evklite-20261115` |

先在独立目录构建，交叉核对包内时间与日志，以及签名、长度、CRC、原始载荷、ELF 入口和打包 HEX。之后将七个产物替换到 `firmware/application_5301/build_dfu_evklite/output/`，逐一复算指纹，并用 `toolbox.py probe-flash --dry-run` 复核默认目录与公开下载的 BIN。Windows 上的 `make test-host` 包装命令遇到 shell 引号问题，按 Makefile 清单逐一直接运行 Python 脚本，19 项全部通过。

已发布独立固件 Release。BIN、HEX、构建清单和校验文件均匿名下载并核对大小与 SHA-256。 [固件 Release](https://github.com/minichao9901/superlinkPRO/releases/tag/probe-evklite-20261115) · [构建清单](probe-evklite-20261115.json)

构建和验收原始日志在 `tmp/probe-evklite-20261115-release/`，其中 `previous-output/` 保存了 11 月 9 日的七个原产物。

## 2026-11-15 实机 DFU 复验

使用 Release 匿名下载副本。升级前识别到唯一在线的 akaLinkPro A.0，USB 序列号末八位 `0AE800E0`，端口路径 `1-2.4`。RTT、J-Scope 处于空闲，DAP 接口可认领。用源码 `toolbox.py probe-flash` 和 dfu-util 0.11 通过 HID 自动进入 `0d28:0207`。

| 项目 | 实测结果 |
| --- | --- |
| 写前备份 | 从 `0x80020000` 读取完整 APP 区 901,120 B，SHA-256 `ba2dc30f293d9f4b4b0e0de08a98ff4ffad9ed6aae0d04c3fd26ef387595740e` |
| 可回退 APP | 包头和 CRC 有效，SHA-256 `3d273d5b64721268cb521807352bfef643d703ce026ad531c869d61674da7e6c` |
| 写后读回 | 195,080 B，与 Release BIN 逐字节一致，SHA-256 `e7ec53719a7a697b8c004c89a9e621092b2952270048d42e09d4fc8efa24e5f7` |
| 重新枚举 | 完整 USB 序列号及端口路径一致，返回 `0d28:0204`，COM52 恢复 |
| HID APP 编译时间 | `2026/11/14 15:00:07` → `2026/11/15 14:07:29`，与新包内时间一致 |
| APP／Bootloader 版本 | `0.1`／`1.0`，Bootloader 编译时间读回 `2026/09/26 22:30:24` |

退出 DFU 的日志记录 `Error during download get_status`。退出后的 USB DFU Runtime 和 HID 身份均已重新查询，确认同一设备运行新 APP。本轮范围是探针 APP 升级、校验与启动，F103ZE ARM/JTAG 功能验收仍待办。原始日志、回退包和写后读回保存在 `tmp/probe-evklite-dfu-20261115/`。
