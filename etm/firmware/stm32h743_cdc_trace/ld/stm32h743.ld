/*
 * STM32H743 链接脚本（2MB Flash / 128KB DTCM / 512KB AXI SRAM）
 * 目标板：正点原子阿波罗 STM32H743（HSE 25MHz）
 *
 * 段布局要点：
 *   .isr_vector / .text / .rodata            -> Flash
 *   .data / .bss                             -> AXI SRAM (0x24000000)
 *   .noncacheable                            -> AXI SRAM，USB 端点缓冲专用（不吃 cache，32B 对齐）
 *   .rtt_cb    @0x20002000 (DTCM, NOLOAD)    -> SEGGER RTT 控制块固定地址
 *   .trace_diag@0x20003000 (DTCM, NOLOAD)    -> 诊断变量固定地址，外部 SWD 工具直接读
 *   栈顶                                     -> DTCM 末尾 0x20020000
 *
 * 注：H743 的 RTT 控制块必须在 AXI SRAM 或 DTCM 里"探针够得着"的地方；
 *     这里选 DTCM 的固定地址，与 h7b0_spi_trace / h7b0_cdc_trace 保持一致。
 */
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 2048K
  DTCM  (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
  AXI   (rwx) : ORIGIN = 0x24000000, LENGTH = 512K
}

_estack = ORIGIN(DTCM) + LENGTH(DTCM);
_Min_Heap_Size  = 0x400;
_Min_Stack_Size = 0x800;

SECTIONS
{
  .isr_vector : {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } > FLASH

  .text : {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    KEEP(*(.init))
    KEEP(*(.fini))
    . = ALIGN(4);
    _etext = .;
  } > FLASH

  .rodata : {
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } > FLASH

  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } > FLASH
  .ARM.exidx : { __exidx_start = .; *(.ARM.exidx*) __exidx_end = .; } > FLASH

  .preinit_array : {
    PROVIDE_HIDDEN(__preinit_array_start = .);
    KEEP(*(.preinit_array*))
    PROVIDE_HIDDEN(__preinit_array_end = .);
  } > FLASH
  .init_array : {
    PROVIDE_HIDDEN(__init_array_start = .);
    KEEP(*(SORT(.init_array.*)))
    KEEP(*(.init_array*))
    PROVIDE_HIDDEN(__init_array_end = .);
  } > FLASH
  .fini_array : {
    PROVIDE_HIDDEN(__fini_array_start = .);
    KEEP(*(SORT(.fini_array.*)))
    KEEP(*(.fini_array*))
    PROVIDE_HIDDEN(__fini_array_end = .);
  } > FLASH

  _sidata = LOADADDR(.data);

  .data : {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > AXI AT> FLASH

  .bss (NOLOAD) : {
    . = ALIGN(4);
    _sbss = .;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > AXI

  .noncacheable (NOLOAD) : {
    . = ALIGN(32);
    *(.noncacheable)
    . = ALIGN(32);
  } > AXI

  . = ALIGN(8);
  _end = .;
  PROVIDE(end = .);
  _heap_limit = ORIGIN(DTCM) + LENGTH(DTCM);

  ._user_heap_stack (NOLOAD) : {
    . = ALIGN(8);
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  } > DTCM

  .rtt_cb 0x20002000 (NOLOAD) : { KEEP(*(.rtt_cb)) } > DTCM
  .trace_diag 0x20003000 (NOLOAD) : { KEEP(*(.trace_diag)) } > DTCM

  /DISCARD/ : { *(.ARM.attributes) }
}

/* src/runtime.c 提供真实的 _init 函数；禁止以地址 0 代替函数。
 * （H743 原来这里是 PROVIDE(_init = 0)，被 __libc_init_array 真实调用后
 *   跳到 0x00000001 → UNDEFINSTR HardFault，见 src/runtime.c 的说明。） */
