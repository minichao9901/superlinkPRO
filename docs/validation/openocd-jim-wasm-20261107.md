# 浏览器 Jim Tcl 配置验证

2026-11-07（北京时间）。使用 OpenOCD v0.12.0 所固定的 Jim Tcl 提交 `a77ef1a6218fad4c928ddbdc03c1aedc41007e70`，由 Emscripten 4.0.10 编译，在 Chrome 156 的独立 Worker 中执行。生成的 Wasm 为 223,625 字节，构建输出位于任务临时目录。 [OpenOCD 固定版本](https://github.com/openocd-org/openocd/tree/v0.12.0) · [Jim Tcl](https://github.com/msteveb/jimtcl/tree/a77ef1a6218fad4c928ddbdc03c1aedc41007e70)

| 原版输入 | 浏览器结果 |
|---|---|
| OpenOCD `target/stm32h7x.cfg`，启用双核、双 Bank 和 CTI | 真正执行 Tcl 变量、条件和 `swj_newdap` 过程。得到 39 条命令记录，包括 AP2、AP0 上的 CPU0、AP3 上的 CPU1、两个 CTI 及四个 Flash bank 声明 |
| HPM SDK v1.11.0 `hpm6800_all_in_one.cfg`，指定 CMSIS-DAP | 展开 `${HPM_SDK_BASE}`、`${PROBE}` 和默认 `${BOARD}`，按顺序加载 7 个引用，得到 69 条命令记录。目标 `hpm6880.cpu0`、TAP `0x1000563D`、IR 5、hart 0、JTAG 8 MHz |
| 负例 | 直接执行宿主程序、访问 socket、读取目录外文件、循环引用、写 Flash、寄存器写命令和复位请求均失败。无限 Tcl 循环触发 Worker 超时，随后新 Worker 执行正常表达式通过 |

原型为配置命令提供记录边界。它实际执行 Tcl 语法和脚本引用，保留 `configure -event` 的参数与事件体，尚未执行事件或创建真正的 OpenOCD 目标对象。浏览器验证未连接硬件，生产调试页继续使用现有导入器。 [机器证据](openocd-jim-wasm-20261107.json)

真实命令需要 OpenOCD 的配置/执行阶段约束、注册树和 JTAG/目标核心。固定上游版本的 `src/helper/command.c` 通过 Jim 注册命令并判断阶段，`src/jtag/tcl.c` 和 `src/target/target.c` 实现 TAP 与目标实例创建。下一道验收门槛是用这些实际实现运行同一组 CFG，在无硬件模式检查对象和阶段行为。再将 CMSIS-DAP 的主机后端映射到浏览器探针，验收安全 RAM 启动及真实内核调试。 [命令注册](https://github.com/openocd-org/openocd/blob/v0.12.0/src/helper/command.c) · [TAP 声明](https://github.com/openocd-org/openocd/blob/v0.12.0/src/jtag/tcl.c) · [目标实现](https://github.com/openocd-org/openocd/blob/v0.12.0/src/target/target.c)

## 复现

在任务目录准备 Emscripten SDK 4.0.10 并在该 SDK 内激活。准备上述固定 Jim Tcl、OpenOCD v0.12.0 脚本目录和 HPM SDK。下例从仓库根运行，`$build` 必须是新目录。

```powershell
python -X utf8 web/tools/dev/build-openocd-jim-wasm.py --emsdk $emsdk --jimtcl $jimtcl --output $build
python -X utf8 -m http.server 8897 --bind 127.0.0.1 --directory $build
```

另开终端，在 CDP 9333 启动隔离的 Chrome。将 `$tcl` 指向 OpenOCD 的 `tcl`，将 `$hpmSdk` 指向 `hpm_sdk` 根目录，`$out` 为新的 JSON 文件。

```powershell
node web/tools/selftest/dbg-jim-wasm-page.mjs http://127.0.0.1:8897/index.html $tcl $hpmSdk $out
```

第一次编译写系统 TEMP 时被拒绝。构建器现仅给 Emscripten 子进程指定输出目录内的 TEMP/TMP/TMPDIR。工作台服务器按设计拒绝 `/tmp/` 访问，浏览器验收改用单独的 loopback 静态服务，工作台路由保持原策略。
