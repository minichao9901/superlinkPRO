/* STM32F103ZE（高密度：512KB flash / 64KB RAM）—— 调试器压测靶子
 *
 * 与 H743 那份（`../stm32h743_dbgstress/ld/stm32h743.ld`）的差别：
 *   H7 的 RAM 是散的，被采样的变量**必须**放 AXI SRAM(0x24000000)（DTCM 探针走 AHB-AP 读不到）；
 *   F103 只有一块 SRAM(0x20000000..0x2000FFFF)，探针直接读得到 —— 于是 .data/.bss 就用它，
 *   栈也从它顶上往下长，不需要任何"分块"的讲究。
 *
 * 容量按 **ZE**（512KB / 64KB）写死：压测固件里有一个 3x2 的结构体数组、一个 double 矩阵
 * 和一个 12 字节 name[12] 的链表，C8（20KB RAM）装不下 —— 本靶子只服务 ZE 那块板。
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

  .ARM.extab : { KEEP(*(.ARM.extab* .gnu.linkonce.armextab.*)) } > FLASH
  .ARM.exidx : {
    __exidx_start = .;
    KEEP(*(.ARM.exidx* .gnu.linkonce.armexidx.*))
    __exidx_end = .;
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

  /* 明确报错：被采样的数据一旦溢出到别处，说明布局假设已经不成立 */
  ASSERT(_ebss <= ORIGIN(RAM) + LENGTH(RAM), "F103 SRAM overflow")
}
