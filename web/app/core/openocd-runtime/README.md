# OpenOCD Wasm runtime

This build retains the upstream STM32H7 Flash driver and the CMSIS-DAP pipeline. The Flash list loads original OpenOCD target CFGs and HPM SDK v1.11.0 board/SoC CFGs through the raw importer. The H743 source path uses the MPU-aware work-area policy and confirms a single reset with a direct DHCSR read.

| File | SHA-256 |
| --- | --- |
| `openocd-core.mjs` | `5a55691c948f503d13bc233902d4a74782bee82e7276d9ad7d52d8394053700a` |
| `openocd-core.wasm` | `1977164ce62a8595753be9dfa741751c0b3f2459a95aa6bd03f25afe72d554fe` |

The runtime uses Emscripten 4.0.10, OpenOCD `9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a`, Jim Tcl `d5243a25c488dfe751ef218828f13516e04ea2ba` and libusb `87a55632db62c9bdc58cd31d3ccfa673f1bb017f`. Build with [build-openocd-core-wasm.sh](../../../tools/dev/build-openocd-core-wasm.sh) and [link-openocd-core-wasm.py](../../../tools/dev/link-openocd-core-wasm.py), using `--optimization Oz --profile full --browser-session` and the libusb archive, source and build directory. This pair shares the runtime built for zynqlinkPRO. [Source and build details](https://github.com/minichao9901/zynqlinkPRO/tree/main/web/app/core/openocd-runtime)

[Regression test](../../../tools/selftest/h743-openocd-flash.test.mjs) uses the shipped Wasm with an emulated H743 CMSIS-DAP target. Physical H743 Flash verification is pending.
