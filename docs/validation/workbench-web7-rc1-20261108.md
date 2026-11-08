# Windows 工作台 web.7-rc.1 本机试用包

2026-11-08（北京时间）。版本 `1.1.0-web.7-rc.1` 来自 `11441e50514f6b83358c0c332fe6f7486159a670` 加当时工作区改动，包内标记 `sourceDirty=true`。已核对的 ETM 运行时快照来源为 `c1a565739231fb0a564ad9aedd35f77ec44dd284`。

用 `python -X utf8 toolbox.py bridge-build` 构建和验证候选。冻结 EXE 通过启动检查，隔离 HTTP 服务实际返回包含 `SWJ_Pins` 的入口、合并脚本和复位模块。原生示例记录重解码、冻结 Trace CLI 查询及火焰图导出、HTTP ZIP 校验均通过。实体复位沿用 8902 源码预览的用户复验，本轮冻结 EXE 验收范围为离线。

8878 的旧工作台关闭后，将通过验证的候选安装到 `dist/superlinkPRO/`。原 `data` 的 9 个文件共 137,049,586 B，迁移前后 SHA-256 逐项相等。新 EXE 的 SHA-256 为 `5d06f91e2d08f40f5509309faae22c9218edefb04d9f0ef379c13be9954e4e06`。

`dist/superlinkPRO-win64.zip` 为 103,099,663 B，SHA-256 为 `f7ad4a74381dfc0a725b8772dca100240c83f7a7ada624ffda8997ac95fccb70`。ZIP 完整性检查通过，关键文件与已安装版本一致。此为本机试用交付。旧安装及验收输出位于 `tmp/workbench-package-befcb5b3/`。
