ENTRY(main)
PHDRS { ram PT_LOAD FLAGS(5); }
SECTIONS { . = 0x20000100; .text : { *(.text) } :ram }
