# KVDAT 采集与快速浏览（1.0.13）

已合入 `main` 并发布为 **1.0.13**（来源分支 `codex/etm-kvdat-fastpath`）。目标是减少“已有 KVDAT → 完整解码/索引 → Web 执行流/火焰图”的耗时，保留原始证据和旧记录兼容性。最终性能数字、受测输入和报告路径见提交验证字段与 `docs/validation/20261019-kvdat-fastpath-bd482e70/`；不把取样耗时当作全流程耗时。

## 数据路径

新网页请求保存 KVDAT，省去大 CSV 中转。旧客户端未传 rawFormat 时仍保存 CSV；已有 CSV、ETM BIN 与记录包继续支持。

```text
KVDAT → 结构校验/边沿读取 → 共用的滤波和 DDR 取样
      → TPIU 解复用 → OpenCSD → 原语义 JSONL/统计
      → 可重建浏览索引/紧凑统计块 → Web 分页与执行顺序图
```

KVDAT 当前只接受已核对的 3.6.x 布局：小于 2^32 样本、32 位边沿位置、标记为零、通道结束哨兵完整。未知布局明确报错；不猜测缺失数据。原生保存完成以结构完整为准，不能只看 ACK 或空文件短暂稳定。普通 LA 采样 BIN 和解复用的 ETM BIN 是不同数据，不能混用。

## 性能实现和兼容性

- 线性指令组只在源位置/函数/执行状态相同且流类型为 linear 时合并统计，输出仍逐条保留；调用、返回、中断和未知状态仍按原规则处理。
- JSONL 的字段、换行和事件顺序保持原样；写入改成较大的缓冲块。
- 浏览索引 v7 归一化重复源码/搜索字段，使用稀疏导航和构建时生成的默认函数轨迹图。
- 可选 `etm_index` 原生程序只处理已经验证的 sidecar 和原始 JSONL；同一次索引原子发布模板、偏移及统计块。SQLite 数据库是派生产物，可删后重建。
- 紧凑统计块每行 20 字节，整数累加周期，窗口图不再为数百万事件重复解析 JSON。超出已验证统计格式时回退 JSONL。原始 JSONL 仍用于逐条查看和导出。
- 窗口缓存按记录跨 HTTP 请求复用，受输入版本签名控制，并限制记录/区间数量。首次计算和内存热缓存分别计时。
- 旧索引会重建。没有有效 sidecar 或没有原生索引器的旧记录，仍可通过 Python 路径读取，首次窗口可能较慢；重新解码可生成完整的快速浏览数据。

## 构建

Python 依赖见 `etm/requirements.txt`。KVDAT 和快速窗口需要 NumPy。

原生索引器使用 SQLite C API，SQLite 为公有领域软件。CMake 找到 SQLite3 开发库时构建 `etm_index`；源码模式缺少它时自动使用 Python 索引实现。

```text
cmake -S etm -B etm/build-native -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe \
  -DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe
cmake --build etm/build-native --target etm_index --config Release
```

Windows 使用 Unicode 参数和路径；实验构建静态链接 SQLite，不需要用户额外安装 SQLite DLL。便携包构建需要包含新增模块和 `etm_index.exe`，源码更新本身不会修改已经解压的旧 EXE。

诊断可用 `/etm/api/health` 查看 `frontend.kvdat`、`nativeIndexer`。设置进程环境 `ETM_PYTHON_INDEX=1` 可强制新建索引用 Python 构建；`ETM_PYTHON_WINDOW=1` 可让窗口走 JSONL 参考实现，便于独立对照。

## 验证口径

同时报告已有输入到首屏、完整索引完成、首次完整执行顺序图、再次打开/相同区间热缓存耗时。首屏不是仅显示摘要：完整事件、查询和索引应已可用。LA 上传/保存时间单独报告；没有实测就不计入。

至少核对两份不同字节相位和边界状态的真机输入；比较 ETM/TPIU/native/events/packets/elements/sources 七项产物，并比较 summary、quality、analysis、首次和局部窗口、默认轨迹图、分页与导航。保留所有未知、未同步、overflow 和边界，不以删除数据换取速度。

当前单次 200 ms 限制继续保留。尚不宣称实时持续采集、逐指令时间戳或任意负载无损，也不把这轮离线窗口优化等同于 J-Trace 的持续流式能力。
