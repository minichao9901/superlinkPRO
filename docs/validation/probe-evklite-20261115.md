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

已发布独立固件 Release。BIN、HEX、构建清单和校验文件均匿名下载并核对大小与 SHA-256。该构建的实机 DFU 和 F103ZE ARM/JTAG 验收待办。 [固件 Release](https://github.com/minichao9901/superlinkPRO/releases/tag/probe-evklite-20261115) · [构建清单](probe-evklite-20261115.json)

构建和验收原始日志在 `tmp/probe-evklite-20261115-release/`，其中 `previous-output/` 保存了 11 月 9 日的七个原产物。
