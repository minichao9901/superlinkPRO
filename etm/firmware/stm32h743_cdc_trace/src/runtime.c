/*
 * 运行时辅助 —— H743 工程从 H7B0 工程补齐的缺失文件。
 *
 * 背景（2026-10-17 实测定因）：
 *   链接时用 `-nostartfiles`（ST 启动文件自带栈/向量，不需要 crt0），于是 newlib 的
 *   crti.o/crtn.o 不参与链接；但 ST 的 Reset_Handler 会调 `__libc_init_array()`，
 *   而 newlib 的实现里**无条件**调用 `_init()`。
 *   H743 的链接脚本原来用 `PROVIDE(_init = 0)` 兜这个引用 → 反汇编可见
 *   `bl ___init_veneer` → `ldr.w pc,[pc]` 装载 **0x00000001** → UNDEFINSTR HardFault，
 *   镜像永远进不了 main()。H7B0 工程有本文件提供真实 `_init`，H743 移植时漏了。
 *
 * 结论：必须提供真实函数，禁止以地址 0 代替函数。
 */
void _init(void) {}
void _fini(void) {}
