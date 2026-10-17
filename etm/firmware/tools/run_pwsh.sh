#!/usr/bin/env bash
# 用 pwsh 7（绝对路径）运行一个 .ps1。bash 的 PATH 传不到 Windows 进程，所以不靠 PATH 找 pwsh。
# 用法：run_pwsh.sh <脚本的 Windows 路径>
set -euo pipefail
PWSH='/c/Program Files/PowerShell/7/pwsh.exe'
exec "$PWSH" -NoProfile -ExecutionPolicy Bypass -File "$1"
