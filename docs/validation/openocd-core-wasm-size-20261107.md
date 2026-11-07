# OpenOCD Wasm Flash 驱动体积对比

2026-11-07（北京时间）。工作区基准提交为 `ee00aa3cce6ba02cdba4e12a360eba2dc0d34b3c`。两版使用 OpenOCD `0.12.0+dev` 提交 `9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a`，该版本包含 HPM 的 `hpm_xpi` 驱动。 [上游源码](https://github.com/openocd-org/openocd/tree/9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a) · [Flash 驱动手册](https://openocd.org/doc/html/Flash-Commands.html)

| Flash 范围 | NOR 驱动 | NAND 控制器 | Wasm 原始大小（字节） | Brotli 下载大小（字节） |
|---|---:|---:|---:|---:|
| 全量 | 83 | 14 | 5,618,752 | 1,480,707 |
| STM32、HPM | 9 | 0 | 4,475,666 | 1,181,952 |
| 全量减精简 | 74 | 14 | 1,143,086 | 298,755 |

精简版原始大小减少 20.34%，Brotli 下载大小减少 20.18%。使用 Node.js 24.13.1、Brotli 1.2.0、压缩质量 9。仅计量 Wasm 响应体，JS、CFG 和 HTTP 头部另计。 [字节数和 SHA-256](openocd-core-wasm-size-20261107.json)

两版使用同一份 OpenOCD、Jim Tcl、libusb 静态库。Emscripten 4.0.10 以 `-O1` 编译 OpenOCD，以 `-Oz` 和 Asyncify 链接 CMSIS-DAP v2 与 libusb 1.0.30 WebUSB。目标类型、ELF 装载与调试代码相同。精简版只从 NOR、NAND 注册表移除驱动，保留 STM32 内部 Flash、STM32 QSPI、HPM XPI，以及 STM32 板级脚本用到的 CFI 和 `virtual`。

## 浏览器核对

- Chrome 156 的两个 Wasm Worker 列出相同顺序的 38 个上游 `target types`。STM32H7 双核 CFG 和 HPM SDK 1.11.0 的 HPM6800EVK CFG 在两版都成功注册目标及原生 Flash bank。
- nRF52 的原版 CFG 在两版 RAM 模式测试中都创建了 `nrf52.cpu`。测试跳过 CFG 的 `nrf5` bank 声明，单独检查 `nrf5` 驱动仅能在全量版注册。
- WebUSB 路径使用模拟空设备列表。验收范围是浏览器配置阶段、目标类型清单和 Flash 驱动注册。实际 RAM ELF 下载、源码调试和 Flash 编程均未在本次体积测试中做逐器件真机验收。

## 复现

准备固定提交的源码与子模块，以及 Emscripten 4.0.10 和 libusb 1.0.30。MSYS2 Autoconf 2.73 会把上游 `AC_CONFIG_FILES` 中的两个续行反斜杠误读为重复条目。在临时源码的 `configure.ac` 删除 `Makefile` 与 `testing/Makefile` 后的两个反斜杠，再为两版共用一次 bootstrap 和静态库构建。 [Windows 构建脚本](../../web/tools/dev/build-openocd-core-wasm.sh)

在仓库根目录运行下列命令。`$build` 指向编译 OpenOCD 的目录，`$usb` 指向同次构建的 libusb 静态库，`$out` 为输出目录的父目录。两次链接都用相同设置编译各自的 Flash 注册表。

```powershell
$revision = '9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a'
foreach ($profile in 'full', 'stm32-hpm-flash') {
    python -X utf8 web/tools/dev/link-openocd-core-wasm.py --emsdk $emsdk --source $source --source-revision $revision --build $build --libusb $usb --optimization Oz --profile $profile --output "$out/$profile"
    node -e 'const b=require("fs").readFileSync(process.argv[1]);const z=require("zlib");console.log(b.length,z.brotliCompressSync(b,{params:{[z.constants.BROTLI_PARAM_QUALITY]:9}}).length)' "$out/$profile/openocd-core.wasm"
}
```

用独立的 loopback 静态服务提供两个输出目录，并用隔离 Chrome 的 CDP 端口 9333 运行浏览器脚本。参数顺序为输出目录的页面 URL、OpenOCD 的 `tcl` 目录、HPM SDK 根目录、新的结果 JSON 路径、`cmsis-dap`、对应 profile 和 `hpm-flash`。 [浏览器回归](../../web/tools/selftest/dbg-openocd-core-wasm-page.mjs)
