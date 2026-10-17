/* STM32H743 **全 RAM 版**（`build.ps1 -Ram`）—— 当 flash 算法跑不起来时的正路。
 *
 * 为什么需要它：本机那块阿波罗 H743 板子的 **flash 算法跑不起来**（halt 通、AHB-AP 读写正常、
 * flash 全 0xFF、RDP 无保护，但写 flash 时 "timed out while waiting for target halted"，
 * 且 SRST 没接到探针 —— 详见 ../stm32h743_rtt_speed/README.md 的"两个 H7 特有的坑"第 2 条）。
 * 于是走"用 AHB-AP 直接把镜像写进 AXI SRAM、再把 SP/PC 指过去"的 RAM 运行方式（不需要 flash 算法）。
 *
 * ⚠️ RAM 运行的固件**必须自己设 VTOR**（代码里的 `SCB_VTOR = g_vectors`）：
 *    复位默认 VTOR = 0（= flash 别名），不放 RAM 地址的话 SysTick 一进中断就取到错向量。
 *    这一点在 flash 版无所谓，在 RAM 版是致命的 —— 本固件靠 SysTick 走 10 kHz 时基。
 *
 * 栈也放 AXI（与数据同段）：RAM 运行方式下探针够不着 DTCM，而这一版本来就什么都放 AXI，
 * 少一处"为什么栈不见了"的疑问。
 */
ENTRY(Reset_Handler)

MEMORY
{
  RAM (rwx) : ORIGIN = 0x24000000, LENGTH = 512K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
  .isr_vector : {
    KEEP(*(.isr_vector))
  } > RAM

  .text : {
    *(.text*)
    *(.rodata*)
    . = ALIGN(4);
    _etext = .;
  } > RAM

  _sidata = LOADADDR(.data);

  .data : {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > RAM AT> RAM

  .bss : {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  . = ALIGN(8);
  _end = .;
  PROVIDE(end = .);
}
