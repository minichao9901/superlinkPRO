/* H7B0: internal Flash code, AXI SRAM diagnostics, DTCM stack. */
ENTRY(Reset_Handler)
MEMORY
{
    FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 128K
    DTCM (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
    AXI (rwx)  : ORIGIN = 0x24000000, LENGTH = 1M
}
_estack = ORIGIN(DTCM) + LENGTH(DTCM);
SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > FLASH
    .text : {
        *(.text*) *(.rodata*)
        . = ALIGN(4);
        _etext = .;
    } > FLASH
    _sidata = LOADADDR(.data);
    .data : {
        . = ALIGN(4); _sdata = .;
        *(.data*)
        . = ALIGN(4); _edata = .;
    } > AXI AT> FLASH
    .bss (NOLOAD) : {
        . = ALIGN(4); _sbss = .;
        *(.bss*) *(COMMON)
        . = ALIGN(4); _ebss = .;
    } > AXI
    . = ALIGN(8); _end = .;
    PROVIDE(end = .);
}
