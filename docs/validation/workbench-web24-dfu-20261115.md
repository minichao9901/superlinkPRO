# SuperLinkPRO web.24 随包 DFU 实机验收

web.23 可执行手册的 APP 检查成功，烧录步骤在查找 dfu-util 时退出。web.24 将 dfu-util 0.11、libusb、winpthreads 放在固定工作台的 `tools/dfu-util/`，冻结 CLI 以包内绝对路径调用。复用终端的 `--dfu-util` 覆盖选项。组件文件、许可证和对应的 MSYS2 源码包均有指纹。 [组件清单](../../tools/vendor/dfu-util/manifest.json)

## 网页实机验证

使用构建源码 `357dcdc` 生成的 `1.1.0-web.24` 冻结候选，从独立本机数据目录打开可执行手册。启动进程的 PATH 仅含 Windows 系统目录。填写已发布的 EVKLite APP BIN 路径，先点击检查按钮，再点击升级按钮。两个命令均退出 0，升级结果的 `dfuUtil` 指向候选包内工具。

| 项目 | 结果 |
| --- | --- |
| 探针 | akaLinkPro A.0，序列号末八位 `0AE800E0` |
| DFU 切换 | HID 自动切换，备份完整 APP 区 901,120 B |
| 写后读回 | 195,080 B 与 Release APP 逐字节一致 |
| APP SHA‑256 | `e7ec53719a7a697b8c004c89a9e621092b2952270048d42e09d4fc8efa24e5f7` |
| 重枚举 | 同一完整序列号、同一个 USB 端口 `1-2.4`，运行态 `0d28:0204`，COM5 |
| HID APP 编译时间 | `2026/11/15 14:07:29`，与包头一致 |

本轮对同一探针重刷当前已发布的 APP，验证范围为网页入口、随包 DFU、写入回读和运行态恢复。22 项相关 Python 检查及冻结候选验证通过。备份、读回及网页命令输出保存在 `tmp/workbench-web24-release-20261115/real-browser/`。

退出 DFU 的日志记录 `Error during download get_status`。手册命令随后通过同一探针的 USB 重枚举检查，独立 HID 查询确认编译时间。

## 固定安装与构建

将通过实测的同一份候选安装到 `dist/superlinkPRO/`，实际启动固定 EXE 并核对显示的版本和构建时间。更新前后 82 个用户数据文件的 SHA-256 逐一一致，固定安装中的 dfu-util 在仅系统 PATH 下可运行。旧程序回退副本和比对记录放在 `tmp/workbench-web24-release-20261115/`。

| 项目 | 值 |
| --- | --- |
| 工作台来源 | `357dcdcc8ccdd4041ace58c1a231f2899233c79a` |
| 发布提交 | `358a081661627bb762f545daa1e5f9bbe2a92956` |
| Web 构建 UTC | `2026-11-15T09:36:15.719Z` |
| Windows 构建 UTC | `2026-11-15T09:40:01Z` |
| Windows ZIP | 104,122,357 B，SHA-256 `f2afe98776e1d74c97d8fade4a8515f1e3b23590985b8f2cad2d4af22a9f7b16` |

## 公开发布

Pages 部署 `43700766612` 对应发布提交 `358a081`。匿名打开 Pages，确认显示 web.24 和两项构建时间。Pages 与 GitHub Release 的 Windows ZIP 分别匿名完整下载，大小和 SHA-256 均与本地相同。配套源码包在两个入口也通过匿名下载和完整哈希校验。Release 标签为 `workbench-v1.1.0-web.24`。 [Pages](https://minichao9901.github.io/superlinkPRO/#download) · [部署记录](https://github.com/minichao9901/superlinkPRO/actions/runs/43700766612) · [Release](https://github.com/minichao9901/superlinkPRO/releases/tag/workbench-v1.1.0-web.24)
