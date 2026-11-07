.file 1 "rv_ram.S"
.text
.globl main
.type main, @function
main:
  .loc 1 7 0 is_stmt 1
  addi a0, a0, 1
  .loc 1 9 0 is_stmt 1
  addi a0, a0, 1
  .loc 1 11 0 is_stmt 1
  j .
.size main, .-main
