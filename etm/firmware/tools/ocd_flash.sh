#!/usr/bin/env bash
# 用 OpenOCD + CMSIS-DAP（DAPLink）烧录 ELF，校验后复位运行。
# 用法：ocd_flash.sh <stm32f1x.cfg | stm32h7x.cfg> <elf 路径>
#
# 流程（与 web 烧录器的前置步骤一致，见 CLAUDE.md）：
#   1) halt + AIRCR 软复位，清掉上一次擦除留下的 LOCKUP
#   2) 屏蔽 SysTick 与 NVIC（擦除时第一个扇区就是向量表，中断进来会 LOCKUP）
#   3) flash write_image erase（按 ELF 擦写）+ verify_image 逐字节校验
#   4) 校验没有输出 "verified" 就判失败，返回非 0
set -euo pipefail

TARGET_CFG="$1"
ELF="$2"
OCD_DIR="${OCD_DIR:-/e/Share/env-windows/rtt-debugger-support-package-0.4.0/bin}"
OCD="$OCD_DIR/openocd.exe"
SCR="$OCD_DIR/scripts"

[ -f "$ELF" ] || { echo "找不到 ELF：$ELF" >&2; exit 1; }
WIN_ELF="$(cygpath -m "$ELF")"   # OpenOCD 是 Windows 程序，必须用 E:/ 形式的路径

# 屏蔽 SysTick + NVIC ICER0..7
MASK_ARGS=(-c "mww 0xE000E010 0")
for i in 0 1 2 3 4 5 6 7; do
    MASK_ARGS+=(-c "mww $(printf '0x%X' $((0xE000E180 + i * 4))) 0xFFFFFFFF")
done

LOG="$(mktemp)"
set +e
timeout 400 "$OCD" -s "$SCR" \
    -f interface/cmsis-dap.cfg -c "transport select swd" \
    -f "target/$TARGET_CFG" -c "adapter speed 1000" \
    -c "init" -c "halt" \
    -c "mww 0xE000ED0C 0x05FA0004" -c "sleep 100" -c "reset halt" \
    "${MASK_ARGS[@]}" \
    -c "flash write_image erase \"$WIN_ELF\"" \
    -c "verify_image \"$WIN_ELF\"" \
    -c "reset run" -c "shutdown" >"$LOG" 2>&1
RC=$?
set -e

grep -v -E "DEPRECATED|CMSIS-DAP:|SWCLK|Listening|gdb|Open On-Chip|Licensed|bugs.html" "$LOG" || true

if [ $RC -ne 0 ] || ! grep -q "^verified" "$LOG" || grep -q "^Error" "$LOG"; then
    echo "烧录或校验失败（openocd exit=$RC）" >&2
    rm -f "$LOG"
    exit 1
fi
echo "烧录并校验通过：$ELF"
rm -f "$LOG"
