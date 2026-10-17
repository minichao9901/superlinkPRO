#!/bin/sh
# 本地桥（macOS / Linux）：./start-bridge.sh [参数...]
cd "$(dirname "$0")" || exit 1
if ! command -v node >/dev/null 2>&1; then
  echo "[!] 找不到 node，请先安装 Node.js 18+"
  exit 1
fi
if [ $# -eq 0 ]; then
  exec node ./rtt-bridge.mjs --target stm32f103
else
  exec node ./rtt-bridge.mjs "$@"
fi
