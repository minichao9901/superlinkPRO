# Windows 工作台

本目录版本管理 `superlinkPRO-win64.zip` 和 `manifest.json`。ZIP包含完整的最新EXE、运行库、网页及原有Kingst ETM运行时；完整解压后双击 `superlinkPRO.exe`。

本机展开目录 `dist/superlinkPRO/` 不纳入Git，其中的 `data/` 是用户记录，更新和清理必须保留。
构建入口：`python -X utf8 toolbox.py bridge-build`。Pages工作流核验ZIP后仅复制完整包与清单到在线下载目录，不发布用户data。
