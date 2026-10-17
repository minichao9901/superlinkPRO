/*
 * 调试器压力测试靶子 —— 控制流/调用链层（2026-10）
 *
 * 覆盖的调试器语义（每一项都对应一个真实的单步/断点难点）：
 *   · engine_linear()      —— `optimize("O0")` 的**线性语句序列**：一语句一条指令，
 *                             是"单步语义"的对照靶子（与 gdb 的 next 逐条比对）
 *   · engine_linear_os()   —— 同样的语句、默认 `-Os`：真实工程里的样子（会有折叠）
 *   · engine_deep_chain()  —— 6 层嵌套调用（**故意都写成"调用后再加工"**，不构成尾调用），
 *                             用来测单步进入/跳出（fin）能不能一层层爬回来
 *   · engine_rec_fib()     —— 经典递归（多重递归，栈上有多个同名帧）
 *   · engine_rec_ack()     —— 更深的递归（Ackermann(2,3)）
 *   · engine_mutual()      —— 互递归（is_even/is_odd 交替）
 *   · engine_dispatch()    —— **函数指针表**调用（BLX Rn）：单步进入要能跟进间接调用
 *   · engine_branchy()     —— 分支/循环/switch：测"单步跳过"落在哪条分支上
 *   · engine_uses_inline() —— 内联函数：源码级单步应**直接越过**它（没有独立地址）
 */
#ifndef ENGINE_H
#define ENGINE_H

#include <stdint.h>

uint32_t engine_leaf(uint32_t a, uint32_t b);

uint32_t engine_linear(volatile uint32_t *slot, uint32_t seed);
uint32_t engine_linear_os(volatile uint32_t *slot, uint32_t seed);

uint32_t engine_deep_chain(uint32_t seed);

uint32_t engine_rec_fib(uint32_t n);
uint32_t engine_rec_ack(uint32_t m, uint32_t n);
int      engine_mutual(uint32_t n);

uint32_t engine_dispatch(uint32_t which, uint32_t x);
uint32_t engine_branchy(uint32_t n);

/* 内联：**没有独立地址**，`b engine_inline_double` 应该明确说"下不到" */
static inline uint32_t engine_inline_double(uint32_t v){ return v * 2u + 1u; }
uint32_t engine_uses_inline(uint32_t v);

#endif /* ENGINE_H */
