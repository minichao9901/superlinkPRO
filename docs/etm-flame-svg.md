# 火焰图 SVG 导出

Web、命令行和 Windows 离线 EXE 共用 `etm/tools/flame_svg.py`，无需连接目标板，也不依赖配套源码正文。需有已解码记录。

## Web

在「火焰图」中选择「按执行顺序」或「按调用权重」，调整序号区间，点击「导出当前火焰图 SVG」。文件页「导出格式 → 当前区间火焰图 SVG」也可使用：火焰图页保留当前模式、权重及聚合图放大节点；在其他视图导出时默认按执行顺序。

- **按执行顺序**：从原始指令记录读取完整 `callPath`，连续相同调用前缀合并成矩形。父调用改变或遇到事件边界时断开。保留原始深度、未知祖先及执行序号空隙。不会把 Web 总览的分桶占位「多段调用」写入 SVG。
- **按调用权重**：与 Web 当前调用树一致，按路径指令记录数或可用的周期数加权；保留当前放大节点。按权重排列，不表示执行顺序。没有周期证据时拒绝周期模式。
- 根在上、子调用向下，函数全名直接显示在其矩形内，没有路径附录。所有矩形共用一个横向比例，按最难容纳的函数名计算最小宽度，保持父子包含关系和原始宽度比例。
- **横轴执行序号不等于时间**。原记录有完整调用深度才有相应层数；不补造采集前调用栈。初始化区间可能有单指令函数，因此完整名称图可宽达数百万像素，这是保持比例和全名同时可见的结果。
- SVG 为独立矢量文件，可水平滚动/缩放。大量调用可先在 Web 选小区间。超过 200000 个调用段/节点时明确报错，要求缩小区间；不静默省略深度。一个很大的区间仍需扫描原始事件，导出可能比 Web 分桶总览慢。

## CLI

源码入口：`python toolbox.py trace export`。离线包入口：`etm-trace.cmd export` 或 `etm-bridge.exe --cli export`，后续参数相同。

```powershell
python toolbox.py trace export --record .formal-trace-20261024/tcp-two-flows --format flame-svg --flame-mode timeline --from-seq 1714288 --to-seq 1718201 --out tcp-echo.svg
etm-trace.cmd export --record E:/records/capture-example --format flame-svg --flame-mode aggregate --flame-weight records --out aggregate.svg
```

| 参数 | 说明 |
|---|---|
| `--record` | 已解码记录目录；支持 TraceStore 和带完整调用栈的旧记录 |
| `--format flame-svg` | 输出 SVG；`--out` 必须为新文件，不覆盖已有文件 |
| `--flame-mode timeline/aggregate` | 默认 timeline，执行顺序/调用权重 |
| `--from-seq`、`--to-seq` | 同时提供，包含两端的执行序号；省略时导出全记录 |
| `--flame-weight records/cycles` | 默认 records；cycles 仅适用于有周期数据的聚合图 |
| `--flame-root 0.2` | 仅聚合图；原始调用树 children 索引路径，不是按权重排序后的下标；空为全树 |

`capabilities` 的 `softwareCapabilities.flameSvg` 声明模式、权重、完整调用栈和节点预算。其他通用参数见 [CLI 参数总表](toolbox-cli-options.md)。

## HTTP

`GET /api/download/<record-id>/flame-svg?fromSeq=1714288&toSeq=1718201&mode=timeline`

聚合图用 `mode=aggregate&weight=records&rootPath=0.2`。`fromSeq/toSeq` 同时提供或同时省略。成功返回 `image/svg+xml` 附件；失败返回 HTTP 400 JSON 错误，Web 在页面中显示错误。嵌入工作台时前缀为 `/etm/api/`，远端网页使用其已连接本机桥的 API 地址。

## 四个实际示例和验证

[离线总览/完整名称查看器](examples/etm-flames/view-exact-flames.html) 可直接用浏览器打开；总览缩小横轴、隐藏放不下的文字，「完整名称 1:1」显示全部名字。原始 SVG 始终包含全部名字。

| 记录和阶段 | 区间（包含两端） | 层数（含未知祖先标记） | 连续调用段 |
|---|---:|---:|---:|
| [TCP 网络初始化](examples/etm-flames/tcp-initialization-exact-flame.svg) | 3498186～3519263 | 11 | 1501 |
| [TCP 首轮 Echo](examples/etm-flames/tcp-echo-exact-flame.svg) | 1714288～1718201 | 16 | 122 |
| [UDP 网络初始化](examples/etm-flames/udp-initialization-exact-flame.svg) | 2814069～2834448 | 11 | 1491 |
| [UDP 首轮 Echo](examples/etm-flames/udp-echo-exact-flame.svg) | 921842～924860 | 18 | 95 |

初始化指 BSP_Config 至 trace_hook_ready，包括 LwIP、网口、echo 服务初始化，排除更早的 Board_Init 延时。Echo 从首次有效收包回调向外定位完整 ethernetif_input 调用，覆盖收包、回调、回显发送和返回；不使用任意固定序号猜测阶段。选区内未验收真实逐指令时间。

复现：

```powershell
python tools/export_network_echo_flames.py --records .formal-trace-20261024 --output docs/examples/etm-flames
python tools/validate_network_flames.py --records .formal-trace-20261024 --report docs/validation/flame-svg-20261027.json
```

验收逐一检查 48391 条原始指令的调用栈覆盖、父子包含关系、完整名称宽度，并比较实际 Web HTTP 和 CLI 导出的 SVG 字节。该验证针对导出层，不代替此前解码器正确性和真机完整性验收。四份 SVG 和精简报告入库；原始记录保留在本机，不随发布包分发。
