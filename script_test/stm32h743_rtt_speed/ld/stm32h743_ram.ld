/* STM32H743：**全 RAM 版**（给 flash 算法跑不起来时用）
 * 用 OpenOCD 的 AHB-AP 直接写 AXI SRAM（不需要 flash 算法），再把 SP/PC 指过来跑。
 * 入口地址 = Reset_Handler|1，栈顶 = _estack，都在这里定义。
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
