ENTRY(_start)

PHDRS
{
    ram PT_LOAD FLAGS(5);
}

/* Inside the SDK's HPM6880 ILM (0x00000000–0x0003FFFF). */
SECTIONS
{
    . = 0x00018000;
    .text : { *(.text) } :ram
}
