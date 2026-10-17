/**
 * STM32 各系列的 flashloader 算法（数据块 + 参数），烧录器在目标 RAM 里执行它们完成擦写。
 *
 * 出处：pyOCD（https://github.com/pyocd/pyOCD）内置目标定义，Apache-2.0。
 * 本仓库同为 Apache-2.0，随源分发并在此注明出处。数据块是编译好的 ARM Thumb 代码，
 * 末尾带 BKPT：执行完会自己 halt，主机轮询 DHCSR.S_HALT 等结果（见 flash/runner.js）。
 *
 * 覆盖系列：F0 / F1 / F4 / F7 / H7 / H7B0 / L0 / L4。
 * 其余系列（F2/F3/G0/G4/L1/L5/U5/C0/WB/WL）暂未内置算法，烧录器走「本地桥 · OpenOCD」。
 * 注意：STM32F1 用的是高密度算法（页参数 2KB），F103 中密度（1KB 页）芯片会被按 2KB 擦，
 * 功能不受影响（实测）；想省擦写寿命可以后续换成中密度算法。
 *
 * H7B0（2026-09-27 新增，用 tools/dev/extract-algo.py 从本机 pyOCD 抽的，别手抄）：
 *   · `page_size` 在**本工程里是"擦除粒度"**（runner 按它步进 erase_sector），H7B 的扇区是 **8KB**；
 *     它的*编程*页是 32KB（算法一次最多吃 32KB），我们按 8KB 分块喂，合法。
 *   · `write_granularity: 32` —— H7 的 flash **按 32 字节（256 位 flash word）编程**，
 *     尾块必须补 0xFF 到 32 字节的倍数，否则写不进去（F1/F4 那些 4 字节就够，别混）。
 *   · `flash_length` 取 128KB（H7B0 value line）。若你手上是 H7B3/H7A3（更大 flash），
 *     把这里改成 0x200000，并把 ld/ 里的 FLASH LENGTH 一起放大。
 *
 * 🚨 **`flash_length` 是算法自带的"标称区间"（来自 pyOCD 的 FlashRegion），不是芯片上限。**
 *    pyOCD 里这一栏常常填的是该系列**最小**成员的大小（F4 只有 64KB、F7 128KB、L4 256KB），
 *    拿它当范围检查的硬上限会把合法固件拒之门外，还给出"芯片选对了吗？"这种误导提示
 *    （代码审查抓到）。所以：硬上限用下面的 `SERIES_MAX_KB`（该系列最大成员），
 *    超过 `flash_length` 只是**提示**（真超了芯片会在编程时报错）。能读到 DEV_ID 的系列更准（见 F1）。
 */
/** 各系列最大 flash（KB）—— 官方数据手册里的最大成员；用来兜底范围检查 */
export const SERIES_MAX_KB = {
  stm32f103: 1024,   // F103xG（XL 密度）
  stm32f0: 256,
  stm32f4: 2048,
  stm32f7: 2048,
  stm32h7: 2048,
  stm32h7b0: 128,    // value line 就是 128KB
  stm32l0: 192,
  stm32l4: 1024,
};

/**
 * 这段固件能不能烧进这个系列 —— 纯函数（不碰 DOM / 探针），Node 自测里钉住。
 * @param {{flash_start:number, flash_length:number}} algo
 * @param {{addr:number, data:{length:number}}} seg
 * @param {{series?:string, devId?:number}} [opts] devId 只对 STM32F1 有意义（DEV_ID → 密度上限）
 * @returns {{ok:boolean, limitBytes:number, beyondNominal:boolean, why?:string}}
 */
export function checkFlashRange(algo, seg, opts = {}){
  const f1 = opts.series === 'stm32f103' && opts.devId != null ? F1_DEV[opts.devId & 0xfff] : null;
  const limitKb = f1 ? f1.kb : (SERIES_MAX_KB[opts.series] || 0);
  const limitBytes = limitKb > 0 ? limitKb * 1024 : algo.flash_length;
  const end = seg.addr + (seg.data?.length || 0);
  if (seg.addr < algo.flash_start){
    return { ok: false, limitBytes, beyondNominal: false, why: 'below' };
  }
  if (end > algo.flash_start + limitBytes){
    return { ok: false, limitBytes, beyondNominal: true, why: 'over' };
  }
  return { ok: true, limitBytes, beyondNominal: end > algo.flash_start + algo.flash_length };
}

/** STM32F1 的 DEV_ID → 擦除粒度 + flash 容量上限（与 OpenOCD 的 stm32f1x 同款做法）。
 *  `kb` 是该密度档的最大容量 —— 读了 DEV_ID 就不再靠系列最大值猜。 */
export const F1_DEV = {
  0x410: { page: 1024, kb: 128, name: '中容量 64~128KB' },
  0x412: { page: 1024, kb: 32,  name: '小容量 16~32KB' },
  0x414: { page: 2048, kb: 512, name: '大容量 256~512KB' },
  0x418: { page: 2048, kb: 256, name: '互联型 64~256KB' },
  0x420: { page: 1024, kb: 128, name: '超值型 低/中容量' },
  0x422: { page: 2048, kb: 512, name: '超值型 高容量' },
  0x428: { page: 2048, kb: 1024, name: '超值型 XL 1MB' },
};
export const ALGOS = {
  "stm32f103": {
    "code": "AL4K4A14LQZoQAgkQAAA01hAZB760UkcUh4AKvLRcEc5SDhJQWA5SUFgACBwRzZJNCDIYAAgcEcAIHBHMkoAtRBpAAYB1f/36//QaMAH/NEQaUDwBAAQYRBpQPBAABBh0GjAB/zREGkg8AQAEGEAIAC9JUoAtQNGEGkABgHV//fP/9FoyQf80RBpQPACABBhU2EQaUDwQAAQYdBowAf80RBpIPACABBhACAAvXC1Fk0DRg5GKGkAJAAGAdX/97D/6GjAB/zRFOBA8AEAKGEQiBiA6GjAB/zREIgZiIhCBdAoaSDwAQAoYQEgcL2SHJscZBwoabTrVg/m0yDwAQAoYQAgcL0jAWdFACACQKuJ780AAAAA",
    "load_address": 536870912,
    "pc_init": 536870959,
    "pc_eraseAll": 536870973,
    "pc_erase_sector": 536871027,
    "pc_program_page": 536871085,
    "static_base": 536871424,
    "begin_stack": 536881152,
    "page_buffers": [
      536875008,
      536877056
    ],
    "flash_start": 134217728,
    "flash_length": 524288,
    "page_size": 2048
  },
  "stm32f0": {
    "code": "AL4K4A14LQZoQAgkQAAA01hAZB760UkcUh4AKvLRcEdTSFRJSGBUSEhgACBwRxC1A0Y0IE9M4GAAIBC9AUYAIHBHALUAIgAjSkgCaYAgEECAKAHR//fk/wC/RkjDaNgHwA/60UNIAmkEIAJDQUgCYQJpQCACQz9IAmEAvz1Iw2jYB8AP+tE7SAJpBCEQRohDAkY4SAJhACAAvRC1A0YAIgAkNEgCaYAgEECAKAHR//e3/wC/L0jEaOAHwA/60S1IAmkCIAJDK0gCYUNhAmlAIAJDKEgCYQC/JkjEaOAHwA/60SRIAmkCIRBGiEMCRiFIAmEAIBC997UVRgAiACYAJwCbLEYbSAJpgCAQQIAoAdH/94b/AL8XSMZo8AfAD/rRG+AUSAJpASACQxJIAmEgiBiAAL8PSMZo8AfAD/rRIIgZiIhCBtALSAJpUghSAAJhASD+vZscpBx/HAGYQAi4Qt/YBEgCaVIIUgACYQAg8OcjAWdFACACQKuJ780AAAAA",
    "load_address": 536870912,
    "pc_init": 536870959,
    "pc_eraseAll": 536870979,
    "pc_erase_sector": 536871067,
    "pc_program_page": 536871159,
    "static_base": 536871328,
    "begin_stack": 536875008,
    "page_buffers": [
      536871936,
      536872960
    ],
    "flash_start": 134217728,
    "flash_length": 65536,
    "page_size": 1024
  },
  "stm32f4": {
    "code": "AL4K4A14LQZoQAgkQAAA01hAZB760UkcUh4AKvLRcEcBRgADAA4gKALTQAkAHQXgECgC0wAJwBwA4IAIyQIB1RAhCENwR0ZIRElBYEVJQWAAIQFgwWjwIhFDwWBAaYAGBtRCSEBJAWAGIUFgQEmBYAAgcEc6SAFpQgURQwFhACBwRzC1NkgBaQQkIUMBYQFpZQMpQwFhAWmiAxFDAWE1STJKAOARYMNo2wP71AFpoUMBYQFpqUMBYQAgML0wtf/3r/8nScpo8CMaQ8pgAiQMYQppwAYADgJDCmEIaeIDEEMIYSRIIUoA4BBgzWjtA/vUCGmgQwhhyGgABgAPA9DIaBhDyGABIDC9cLUVTckciQjraIkA8CYzQ+tgACMrYRZLF+AsaRxDLGEUaARg7GjkA/zULGlkCGQALGHsaCQGJA8E0OhoMEPoYAEgcL0AHRIdCR8AKeXRACBwvQAAIwFnRQA8AkCrie/NVVUAAAAwAED/DwAAqqoAAAECAAAAAAAA",
    "load_address": 536870912,
    "pc_init": 536870983,
    "pc_eraseAll": 536871043,
    "pc_erase_sector": 536871101,
    "pc_program_page": 536871177,
    "static_base": 536871281,
    "begin_stack": 536872960,
    "page_buffers": [
      536875008,
      536879104
    ],
    "flash_start": 134217728,
    "flash_length": 65536,
    "page_size": 16384
  },
  "stm32f7": {
    "code": "AL4K4A14LQZoQAgkQAAA01hAZB760UkcUh4AKvLRcEe/80+PcEfAAsANQCgC04AJAB1wRyAoAtNACcAccEfACHBHSUhHSUFgSElBYAAhAWDBaPAiEUPBYEBpgAYG1EVIQ0kBYAYhQWBDSYFgACBwRz1IAWlCBRFDAWEAIHBHELU5SAFpBCQhQwFhAWmiAxFDAWE6STdKAOARYMNo2wP71AFpoUMBYQAgEL0wtf/3u/8tScpo8CMaQ8pgAiQMYQppwAYADgJDCmEIaeIDEEMIYb/zT48pSCdKAOAQYM1o7QP71AhpoEMIYchoAAYADwPQyGgYQ8hgASAwvfC1GkzJHIkI5WiJAPAjHUPlYAAjI2EBJ/8GGU0h4CNpGU4zQyNhxgL2CvYZE2gzYL/zT48RTgDgNWDjaNsD+9QjaVsIWwAjYeNoGwYbDwXQ4GjwIQhD4GABIPC9AB0JHxIdACnb0QAg8L0jAWdFADwCQKuJ781VVQAAADAAQP8PAACqqgAAAQIAAAAAAAA=",
    "load_address": 536870912,
    "pc_init": 536870979,
    "pc_eraseAll": 536871039,
    "pc_erase_sector": 536871083,
    "pc_program_page": 536871163,
    "static_base": 536871300,
    "begin_stack": 536871936,
    "page_buffers": [
      536875008,
      536876032
    ],
    "flash_start": 134217728,
    "flash_length": 131072,
    "page_size": 32768
  },
  "stm32h7": {
    "code": "AL7957/zT49wR0r2qiD+SQhgQBAIYAYgCR0IYEr2qiD6SQhgQBAIYAYgCR0IYEDy/xD3SQhgfyAJHwhgcEcQtQNGDEZytk/0gHDySQhgByDxSQhgT/TeAEhhAL/uSABpAPAEAAAo+dHrSMBoAPABACCx6kjoSUhg6UhIYE/03gDoSQhgAL/nSAAfAGgA8AQAACj40eNICDgAaADwAQA4sd5I4EkQOQhg3UjbScH4BAH/96v/3EjYSUhh2kkIYAAgEL0BRgAgcEfYSNNJSGEAv9FIAGkA8AQAACj50dNI0UkIYAC/z0gAHwBoAPAEAAAo+NEAv8hIAGkA8AQAACj50cVIwGgg8DAAw0nIYAhGwGhA8AgAyGAIRsBoQPCAAMhgAL+9SABpAPAEAAAo+dG6SMBoIPAIALhJyGAAv7lIAB8AaADwBAAAKPjRtkgIOABoIPAwALBJwfgMAQhG0PgMAUDwCADB+AwBCEbQ+AwBQPCAAMH4DAEAv6pIAB8AaADwBAAAKPjRp0gIOABoIPAIAKFJwfgMAQAgcEdwtQNGGkYD9QA2ACSS4MLzQ0Wy8QBvP9Oy8QFvPNKXSEBpm0kOMQhDlUlIYQHg//cf/5JIAGkA8AQAACj30Y9IwGhH9jBxiEONSchgRPAEAEDqBSBA8DAAyWgIQ4hJyGAIRsBoQPCAAMhgAeD/9wD/g0gAaQDwBAAAKPfRgEjAaCDwBAB+SchgCEYAaQDwAQAAKEzQASBwvXxIAGh9SQ4xCEN2ScH4FAEB4P/34f52SAAfAGgA8AQAACj20XNICDgAaEf2MHGIQ21JwfgMAUTwBAGl8QgAQeoAIEDwMABnSdH4DBEIQ2VJwfgMAQhG0PgMAUDwgADB+AwBAeD/97j+YkgAHwBoAPAEAAAo9tFeSAg4AGgg8AQAWUnB+AwBWkgAHwBoAPABAAixASCz5wL1ADIAv7JCf/ZqrwAgq+ct6fdNBUaSRi9GAZw6RtBGACO38QBvCtO38QFvB9JISEBpTEkOMQhDRUlIYQfgR0gAaEhJDjEIQ0FJwfgUAa7g//d3/rLxAG8K07LxAW8H0jtIwGhH9jBxiEM5SchgCOA6SAg4AGhH9jBxiEM0ScH4DAGy8QBvBtOy8QFvA9IyIC9JyGAD4DIgMEkIOQhgICwP0wAjCeDY+AAQ2PgEABFgUGAI8QgICDJbHAQr89sgPBXgFkbDRgAjBOAb+AELBvgBC1sco0L40wAjA+D/IAb4AQtbHMTxIACYQvfYACT/9yX+svEAbwzTsvEBbwnSAeD/9x/+EkgAaQDwBAAAKPfRCeAB4P/3Ff4QSAAfAGgA8AQAACj20QpIAGkAIFizsvEAbx7TsvEBbxvSBUjAaBPgAEgAWABMAFgELABA1EQCWAAgAFIjAWdFq4nvzRQhAFIAAO4PAADvDyDwAgBFSchgBuBFSABoIPACAEJJwfgMAQAgvej+jbLxAG8J07LxAW8G0jxIwGgg8AIAOknIYAbgOUgAaCDwAgA2ScH4DAEALH/0Tq8AIOTnNEgAaEDwAQAySQhgMUgQMABoMUkIQC9JEDEIYC1IAGguSQhAK0kIYAhGAGgg9IAgCGAoSBAwAGgg9P4AJUkQMQhgACAjSWAxCGAiSABoIPAYACBJCGAfSBAwAGgdSRAxCGAIRgBoCGAIRgBoQPQAcAhgyAUaSQhgcEcAtQAi//eE/RJIAmkB4BBIAmkC8AEAACj50Q5IAB0CaALgDEgAHQJoAvABAAAo+NEISMJoAeAGSMJoACAAKPrRBUgCaAHgA0gCaAAgACj60QC9ACAAUgwhAFIARAJYDMB/+P//9v4I7QDgAAAAAA==",
    "load_address": 536870912,
    "pc_init": 536870975,
    "pc_eraseAll": 536871113,
    "pc_erase_sector": 536871323,
    "pc_program_page": 536871641,
    "static_base": 536872292,
    "begin_stack": 536878448,
    "page_buffers": [
      536872304
    ],
    "flash_start": 134217728,
    "flash_length": 1048576,
    // H7 erase sectors are 128 KB, but the RAM flash-algorithm buffer is 1 KB.
    // Keep erase geometry separate from the size of each program call.
    "page_size": 131072,
    "program_buffer_size": 1024,
    "write_granularity": 32
  },
  "stm32l0": {
    "code": "AL4K4A14LQZoQAgkQAAA01hAZB760UkcUh4AKvLRcEcBKgHQAioX0TtIgWkPIhICEUOBYTlJwWA5ScFgOUkBYTlJAWHAacACBtQ5SDdJAWAGIUFgN0mBYAAgcEcBKAHQAigI0SxIQWgCIhFDQWBBaAEiEUNBYAAgcEcwtSZJSmhMFSJDSmBKaAglKkNKYAAiAmApSCZKAOAQYItp2wf70UhooENIYEhoqENIYAAgML0BIHBH8LUYTAAjJRUIJj8xiQmMRiTgYWgpQ2FgYWgxQ2FgQCGAyoDACR8AKfrRFkmnaf8HAtASTzlg+eehaQkFCQ8G0KBpDyEJAghDoGEBIPC9YWipQ2FgYWixQ2FgWxycRdjYACDwvQAgAkDvzauJBQQDAr+unYwWFRQTVVUAAAAwAED/DwAAqqoAAAAAAAA=",
    "load_address": 536870912,
    "pc_init": 536870945,
    "pc_eraseAll": 301989889,
    "pc_erase_sector": 536871035,
    "pc_program_page": 536871093,
    "static_base": 536871228,
    "begin_stack": 536871936,
    "page_buffers": [
      536875008
    ],
    "flash_start": 134217728,
    "flash_length": 32768,
    "page_size": 128
  },
  "stm32l4": {
    "code": "AL4K4A14LQZoQAgkQAAA01hAZB760UkcUh4AKvLRcEe/80+PcEdYSABoWEkABQANQBgA0AEgcEdVSABqgALAD3BHALUCRv/37v8BKAjR//fz/wEoBNFPSIJCAdMBIAC9ACAAvQC1Akb/993/ASgC0NACgA0AvUhJ0AoIQBED+dX/MAEwAL1CSERJgWBESYFgACEBYENJAWEAasADBtRDSEFJAWAGIUFgQUmBYAAgcEcBIDdJwAdIYQAgcEcBIHBHM0g4SQFhwRNBYUFpASISBBFDQWE3STVKAOARYANp2wP71AAhQWEIRnBHELUERv/3qP8DRiBG//e1/yVJKUwMYcIA2AKSHAJDSmFIaQEiEgQQQ0hhv/NPjyZIJEoA4BBgC2nbA/vUACBIYQhpIEAB0AxhASAQvfC1yR0VTckIGU/JAC9hACNrYRpMGOABI2thE2gDYFNoQ2C/80+PE0sA4BxgLmn2A/vUACNrYStpO0IC0C9hASDwvQgwCDkIMgAp5NEAIPC9AAAAIATgy/v//wAgAkAAAAgI/wIAACMBZ0Wrie/N+sMAAFVVAAAAMABA/w8AAKqqAAAAAAAA",
    "load_address": 536870912,
    "pc_init": 536871047,
    "pc_eraseAll": 536871105,
    "pc_erase_sector": 536871147,
    "pc_program_page": 536871223,
    "static_base": 536871352,
    "begin_stack": 536871936,
    "page_buffers": [
      536875008,
      536876032
    ],
    "flash_start": 134217728,
    "flash_length": 262144,
    "page_size": 2048
  },
  "stm32h7b0": {
      "code": "AL7950C6cEfAunBHT+owAHBHAAC/80+PcEcQtQNG6kjqTGBhAL/pSABpAPABAAAo+dHnSOVMYGDmSGBg4kjmTCBgAL/kSAAfAGgA8AEAACj40d9I4EwQPCBg3kjbTMT4BAEgRsBpACAQvQFG10jAaEDwAQDVStBg10gIOABoQPABAML4DAEAIHBHAL/PSABpAPABAAAo+dHLSMxJSGEIRsBoIPABAMhgCEbAaEDwCADIYAhGwGhA8CAAyGAAv8NIAGkA8AEAACj50cBIwGgg8AgAvknIYAC/v0gAHwBoAPABAAAo+NG4SLtJCGC3SND4DAEg8AEAtUnB+AwBCEbQ+AwBQPAIAMH4DAEIRtD4DAFA8CAAsEkIOQhgAL+uSAAfAGgA8AEAACj40atICDgAaCDwCAClScH4DAEAIHBHELUBRsHzRzKx8QBvNtOx8QFvM9KeSEBpoUsYQ5xLWGEAv5pIAGkA8AQAACj50ZdIwGgg9P5QlUvYYBhGwGgEI0PqghMYQ5FL2GAYRsBoQPAgANhgAL+NSABpAPAEAAAo+dGKSMBoIPAEAIhL2GAYRgBpAPABAPCzASAQvYdIAGiHSxhDgkvD+BQBAL+DSAAfAGgA8AQAACj40X9ICDgAaCD0/lB6S8P4DAF7SAg4AGii8YADBCRE6oMTGEN0S8P4DAEYRtD4DAFA8CAAw/gMAQC/cUgAHwBoAPAEAAAo+NFuSAg4AGgg8AQAaEvD+AwBakgAHwDgBeAAaADwAQAIsQEguuf/9+f+ACC25/C1A0YWRhpGNUYAJAC/XEgAaQDwAQAAKPnRWEhZT3hhAL9aSAAfAGgA8AEAACj40VNIVk84YJzgUkjAaCDwAQBQT/hgOEbAaEDwAgD4YE9ICDgAaCDwAQDH+AwBOEbQ+AwBQPACAMf4DAEQKQzTACQG4C9oaGgXYFBgCDUIMmQcAiz22xA5KOAAJATgFfgBCwL4AQtkHIxC+NMAJAPg/yAC+AELZBzB8RAAoEL32LPxAG8J07PxAW8G0jFIwGhA8EAAL0/4YAfgMUgIOABoQPBAACtPx/gMAQAh//d2/rPxAG8K07PxAW8H0gC/JUgAaQDwAQAAKPnRB+AAvyRIAB8AaADwAQAAKPjRHUgAaQAgH08/Hz9oAEOwsbPxAG8J07PxAW8G0hZIwGgg8AIAFE/4YAfgFkgIOABoIPACABBPx/gMAQEg8L2z8QBvCdOz8QFvBtILSMBoIPACAAlP+GAH4ApICDgAaCDwAgAFT8f4DAEAKX/0YK8AIOTnAAAAAK8PACAAUiMBZ0Wrie/NFCEAUgAA7w8AAAAA",
      "load_address": 536870912,
      "pc_init": 536870939,
      "pc_eraseAll": 536871051,
      "pc_erase_sector": 536871231,
      "pc_program_page": 536871499,
      "static_base": 536871904,
      "begin_stack": 536941552,
      "page_buffers": [
          536871920
      ],
      "flash_start": 134217728,
      "flash_length": 131072,
      "page_size": 8192,
      "write_granularity": 32
  },
};
