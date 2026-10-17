/*
 * 给 HPM flashloader 用的极小 libc 替身（SPDX-License-Identifier: BSD-3-Clause）。
 *
 * 为什么需要：算法本身只用到 `memset` 一个 libc 函数，而链接完整 newlib 会带进
 * malloc 表、impure_data、GOT 等一大堆东西，把 blob 从几 KB 撑到 16.8 KB
 * （实测：-nostdlib + 本文件 = 3.1 KB；去掉 -nostdlib = 16.8 KB）。
 * 算法是要通过调试链路逐字写进目标 SRAM 的，blob 小一个数量级 = 烧录前少等十几秒。
 *
 * 语义：只保证"按字节写 value 的低 8 位"，与 C 标准一致；不返回 dst 之外的东西。
 */
#include <stddef.h>

void *memset(void *dst, int value, size_t n)
{
    unsigned char *p = (unsigned char *)dst;
    unsigned char v = (unsigned char)value;

    while (n-- != 0U) {
        *p++ = v;
    }
    return dst;
}
