// OpenOCD 9f2d8762b3a75b4d7b2155affcf3d8bb8eb8849a
export const upstreamFlashConfigs = {
  "stm32f103": [
    "target/stm32f1x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32f0": [
    "target/stm32f0x.cfg",
    "mem_helper.tcl"
  ],
  "stm32f2": [
    "target/stm32f2x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32f3": [
    "target/stm32f3x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32f4": [
    "target/stm32f4x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32f7": [
    "target/stm32f7x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32g0": [
    "target/stm32g0x.cfg",
    "mem_helper.tcl"
  ],
  "stm32g4": [
    "target/stm32g4x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32h7": [
    "target/stm32h7x_dual_bank.cfg",
    "target/stm32h7x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32h7b0": [
    "target/stm32h7x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32l0": [
    "target/stm32l0.cfg",
    "mem_helper.tcl"
  ],
  "stm32l1": [
    "target/stm32l1.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32l4": [
    "target/stm32l4x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32l5": [
    "target/stm32l5x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl",
    "target/stm32x5x_common.cfg"
  ],
  "stm32u5": [
    "target/stm32u5x.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl",
    "target/stm32x5x_common.cfg"
  ],
  "stm32c0": [
    "target/stm32c0x.cfg",
    "mem_helper.tcl"
  ],
  "stm32wb": [
    "target/stm32wbx.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "stm32wl": [
    "target/stm32wlx.cfg",
    "target/swj-dp.tcl",
    "mem_helper.tcl"
  ],
  "nrf52": [
    "target/nordic/nrf52.cfg",
    "mem_helper.tcl"
  ],
  "rp2350": [
    "target/rp2350.cfg"
  ]
};

// HPM SDK v1.11.0: board ID to all-in-one entry and single-core SoC.
export const hpmSdkFlashConfigs = {
  "hpm5300evk": [
    "5300",
    "5300"
  ],
  "hpm5301evklite": [
    "5300",
    "5300"
  ],
  "hpm5e00evk": [
    "5e00",
    "5e00"
  ],
  "hpm6200evk": [
    "6280",
    "6280-single-core"
  ],
  "hpm6300evk": [
    "6300",
    "6360"
  ],
  "hpm6750evk2": [
    "6750",
    "6750-single-core"
  ],
  "hpm6750evkmini": [
    "6750",
    "6750-single-core"
  ],
  "hpm6800evk": [
    "6800",
    "6880"
  ],
  "hpm6e00evk": [
    "6e00",
    "6e80-single-core"
  ],
  "hpm6p00evk": [
    "6p00",
    "6p80-single-core"
  ]
};
