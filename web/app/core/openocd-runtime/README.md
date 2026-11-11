# OpenOCD Wasm runtime

This build retains the full Flash registry and the CMSIS-DAP pipeline from web.12. The [H7 DBGMCU patch](../../../tools/dev/openocd-h7-dbgmcu.patch) reads H74x/H75x chip identity through AP2 and checks Flash capacity through the CPU MEM-AP before erase. The built-in H7 programmer enables the D1 debug clocks before CPU examination and verifies that it restores DBGMCU_CR on disconnect or before reset.

| File | SHA-256 |
| --- | --- |
| `openocd-core.mjs` | `c3e893b054999756781ce1b94c067b04c1bb437e50e48f1d8baef1579f25209f` |
| `openocd-core.wasm` | `d8017a5f31244efbc776cf7b24f1992e95aeb19aa5e9e95f86ce814146a3daf0` |

The runtime uses Emscripten 4.0.10, OpenOCD `9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a`, Jim Tcl `d5243a25c488dfe751ef218828f13516e04ea2ba` and libusb `87a55632db62c9bdc58cd31d3ccfa673f1bb017f`. Build with [build-openocd-core-wasm.sh](../../../tools/dev/build-openocd-core-wasm.sh) and [link-openocd-core-wasm.py](../../../tools/dev/link-openocd-core-wasm.py), using `--optimization Oz --profile full --browser-session` and the libusb archive, source and build directory. This pair shares the runtime built for zynqlinkPRO. [Source and build details](https://github.com/minichao9901/zynqlinkPRO/tree/main/web/app/core/openocd-runtime)

[Regression test](../../../tools/selftest/h743-openocd-flash.test.mjs) uses the shipped Wasm with an emulated H743 CMSIS-DAP target. Physical H743 Flash verification is pending.
