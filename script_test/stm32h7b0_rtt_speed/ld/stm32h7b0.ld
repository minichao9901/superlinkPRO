/* STM32H7B0 · 128KB flash / 128KB DTCM
 *
 * 🚨 内存布局是 H7 上最容易踩的地方：H7 的 RAM 是**散的** ——
 *    DTCM        0x20000000  128KB   内核直连、零等待，**不需要开任何时钟**
 *    AXI SRAM    0x24000000  1MB     要开 D1 域时钟（本工程不用，留作参考）
 *    AHB SRAM    0x30000000  等       D2/D3 域
 *    本工程把代码放 flash、数据/栈/RTT 缓冲全放 **DTCM**：最省事也最快，
 *    "吞吐测试"这条通路越干净越好。
 *
 * flash 长度按 H7B0（value line，128KB）写；如果你手上是 H7B3/H7A3（更大 flash），
 * 把 LENGTH 改大即可（H7B 是双 bank，第二 bank 在 0x08100000，本脚本只管第一段）。
 */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 128K
  DTCM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

_estack = ORIGIN(DTCM) + LENGTH(DTCM);

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
  } > DTCM AT> FLASH

  .bss : {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > DTCM

  . = ALIGN(8);
  _end = .;
  PROVIDE(end = .);
}
