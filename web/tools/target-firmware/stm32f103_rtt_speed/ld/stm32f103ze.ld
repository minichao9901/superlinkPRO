/* STM32F103ZE 之类（高密度：512KB flash / 64KB RAM）
 *
 * 与 c8 版的区别只有容量：ZE 的 64 KB SRAM 让我们能把 RTT 上行缓冲开大
 * （-DBUFFER_SIZE_UP=32768），狂发时主机轮询间隔造成的饥饿更少。
 *
 * 注意 SRAM 覆盖 0x20000000..0x2000FFFF —— 这也正是原作者
 * script_test/swd/benchmark_readback.tcl 里 file_size=65536 的那块区域，
 * 所以"64 KB load_image/dump_image"基准可以直接跑（跑的时候目标必须 halt，
 * 整片 SRAM 会被覆盖，包括 RTT 控制块）。
 */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 512K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
  .isr_vector : {
    KEEP(*(.isr_vector))
  } > FLASH

  .text : {
    *(.text*)
    *(.rodata*)
    . = ALIGN(4);
    _etext = .;
  } > FLASH

  _sidata = LOADADDR(.data);

  .data : {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > RAM AT> FLASH

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
