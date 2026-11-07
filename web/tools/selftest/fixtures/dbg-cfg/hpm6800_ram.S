.file 1 "hpm6800_ram.S"
.text
.globl _start
.type _start, @function
_start:
  addi a0, zero, 0
  j main
.size _start, .-_start

.globl main
.type main, @function
main:
  .loc 1 14 0 is_stmt 1
  addi a0, a0, 1
  .loc 1 16 0 is_stmt 1
  addi a0, a0, 1
  .loc 1 18 0 is_stmt 1
  j .
.size main, .-main
