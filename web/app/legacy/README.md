# 旧版探针诊断模块

这里保留旧的 WebUSB DAP、RISC-V 调试会话和 Flash 算法，供源码诊断脚本及协议回归使用。依赖仍用 app 下的共享实现。

工作台调试、烧录、RTT Viewer 和 Trace 目标访问通过 OpenOCD Wasm。app/legacy 从 Pages 资产和新 Windows 包中排除。运行历史诊断脚本时，使用源码服务和独立验收会话。
