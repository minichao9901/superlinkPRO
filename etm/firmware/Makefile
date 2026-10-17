# firmware 一键构建 + OpenOCD 烧录（Windows，Git Bash / MSYS make）
#
#   make flash-f103    F103ZE 目标板：构建 build/ze，烧录（stm32f1x）
#   make flash-h7b0    STM32H7B0 CPU 70MHz：构建 build/70，烧录（stm32h7x）
#   make flash-h743    STM32H743 CPU 480MHz：构建 build/，烧录（stm32h7x）
#
# 每条命令：pwsh 7 构建 -> OpenOCD 软复位 -> 屏蔽中断 -> 擦写 -> 校验 -> 复位运行。
# 目标板 USB 无响应时，先拔插探针 USB 再烧（实测有效）。

SHELL := /bin/bash
RUN_PWSH := tools/run_pwsh.sh
OCD_SH   := tools/ocd_flash.sh

.PHONY: flash-f103 flash-h7b0 flash-h743 build-f103 build-h7b0 build-h743

build-f103:
	bash $(RUN_PWSH) "E:\web-etm-trace\firmware\tools\build_f103.ps1"
build-h7b0:
	bash $(RUN_PWSH) "E:\web-etm-trace\firmware\tools\build_h7b0.ps1"
build-h743:
	bash $(RUN_PWSH) "E:\web-etm-trace\firmware\tools\build_h743.ps1"

flash-f103: build-f103
	bash $(OCD_SH) stm32f1x.cfg stm32f103_cdc_trace/build/ze/fw.elf

flash-h7b0: build-h7b0
	bash $(OCD_SH) stm32h7x.cfg stm32h7b0_cdc_trace/build/70/fw.elf

flash-h743: build-h743
	bash $(OCD_SH) stm32h7x.cfg stm32h743_cdc_trace/build/fw.elf
