ENTRY(Reset_Handler)

MEMORY
{
    SRAM (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}

_stack_top = ORIGIN(SRAM) + LENGTH(SRAM);

SECTIONS
{
    .isr_vector : { KEEP(*(.isr_vector)) } > SRAM
    .text : { *(.text .text.*) *(.rodata .rodata.*) } > SRAM
    .data : { *(.data .data.*) } > SRAM
    .bss (NOLOAD) : { *(.bss .bss.* COMMON) } > SRAM
}
