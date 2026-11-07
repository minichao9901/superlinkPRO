# RP2350 工作台验收夹具

用 Pico SDK 2.2.0（含子模块）、CMake、Ninja 和 GNU Arm 工具链。在仓库根目录生成带 DWARF 的 Cortex-M33 ELF：

```powershell
cmake -S script_test/rp2350_workbench -B tmp/rp2350-fixture -G Ninja -DCMAKE_BUILD_TYPE=Debug -DPICO_SDK_PATH=E:/sdk/pico-sdk
cmake --build tmp/rp2350-fixture
```

`g_ticks` 是运行心跳，`fixture_cycle` 和 `fixture_leaf` 用于断点与源码单步；RTT 上行有心跳与下行回显。将 `g_flood` 置为非零可产生连续测试数据，`g_bench` 预留 64 KiB SRAM 测试区。此目录保存 [2026-11-08 RP2350 原版 CFG 真机验收](../../docs/validation/rp2350-cfg-shared-hw-20261108.md)使用的 C 源码，构建不接触探针或目标板。
