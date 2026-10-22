# ETM 便携桥 · Windows 64 位

首次使用从工程页面下载完整包，解压后双击 `start-etm-bridge.bat`。包中包含 Python、Python 依赖、工具箱网页、ETM 网页与采集分析服务、OpenCSD 解码器和一个 SPI Demo。无需另外下载两个工程、安装 Python 或构建解码器。KingstVIS 及其硬件驱动仍由用户安装并开启 Socket。

启动文件小包包含 BAT、PowerShell、端口配置和说明，仅用于已解压完整包。它不单独包含运行环境。采集结果存放在完整包同目录的 `data/records`；升级前请保留 `data`。桥只监听 `127.0.0.1`，按 Ctrl+C 退出。

从 `1.0.14-native.9` 起，EXE 默认启用原生解码及完整查询优化，双击启动即可；缺失配置或旧的仅端口配置也会启用。启动日志及 `/etm-launcher.json` 的 engine 字段可核对选择。需要对照时可设置 `engine: "python"` 或传入 `--engine python`，退出旧工作台后重启生效。详见仓库 `docs/etm-native-engine.md`；尚未完成新的真机验收，采集上限仍为 200ms。

`1.0.14-native.11` 起，trace-store 的四种执行流导出默认使用随包 `etm_export.exe`，已有记录无需重解码；旧 JSONL/内联格式保留原入口。完整包缺少导出器会明确报错。开发对照可设置 `ETM_EXPORT_ENGINE=python`，不会改变解码引擎选择。

## 仓库结构

- `backend/`：从已验收 ETM 版本复制的运行源码、网页、解码器、单个 Demo 和示例 ELF；不包含 SDK 或用户记录。
- `etm_bridge.py`：便携桥入口，固定资源定位与记录目录。
- `provenance.json`：来源提交、原始文件哈希与本地适配。
- `downloads/etm-bridge-win64.zip`：供 GitHub Pages/本地网页直接下载的完整包。
- `downloads/manifest.json`：版本、大小和 SHA256。

## 开发者构建

普通使用无需构建。重新生成下载包时，在 Windows 64 位环境运行：

```powershell
python -m pip install -r bridge/etm/requirements-build.txt
node tools/dev/embed-etm-kit.mjs
npm run build:web
python tools/dev/build-etm-package.py
```

日常构建只使用本仓库的快照。更新 ETM 运行源码时，先指定受审查的来源：

```powershell
python tools/dev/sync-etm-runtime.py --etm-root ../etm
```

核对 `provenance.json` 和源码差异后，再重建并验收。源码模式开发入口仍保留在 `tools/dev/start-etm.ps1`；它的仓库路径参数只供开发使用。

运行资源与用户数据分开放置：资源按 PyInstaller 的运行目录规则定位，记录存到 EXE 同目录而非临时解包目录。参见 [PyInstaller 官方运行目录说明](https://pyinstaller.org/en/stable/runtime-information.html)。第三方许可证在完整包 `licenses/` 中。
