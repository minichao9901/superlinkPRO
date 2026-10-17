/* STM32H743（正点原子阿波罗 H743）：128KB flash 起步 + 512KB AXI SRAM
 *
 * ⚠️ 关键：**不能用 DTCM(0x20000000)**。H7 的 ITCM/DTCM 是内核私有总线，外部调试器
 * 走 AHB-AP **读不到**；RTT 的控制块与环形缓冲必须放在 AXI SRAM(0x24000000) 上，
 * 探针才能通过 SWD 读到它。栈也一起放 AXI SRAM，省得多分一段。
 */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 128K
  RAM   (rwx) : ORIGIN = 0x24000000, LENGTH = 512K
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
