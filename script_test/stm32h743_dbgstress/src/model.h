/*
 * 调试器压力测试靶子 —— 复杂数据结构层（2026-10）
 *
 * 目的：给「监视窗口 / 结构体树 / 命令 p」造一棵**尽量难解析**的对象树，
 *       每一处都对应一个真实的 DWARF 特性：
 *         · 嵌套结构体（结构体里放结构体、结构体里放数组、数组元素又是结构体）
 *         · 二维数组、double 矩阵（格式化要认 f64）
 *         · 联合体 union（同一段内存三种看法）
 *         · 位域（无符号多段 + **有符号位域** + 跨存储单元）
 *         · char 数组（C 字符串展示）、纯字节 blob（不可打印要退回 hex）
 *         · 指针链表（node_t *head 串起 nodes[]，测试指针跟随）
 *         · const 限定的全局对象（DW_TAG_const_type 那条链）
 *
 * ⚠️ 变量全部**非 static**：ELF 里要有符号，页面才能列出来。
 *    .data/.bss 落在 AXI SRAM(0x24xxxxxx) —— 见 ld 脚本（H7 的 DTCM 探针读不到）。
 */
#ifndef MODEL_H
#define MODEL_H

#include <stdint.h>

typedef enum {
  MODEL_IDLE  = 0,
  MODEL_RUN   = 1,
  MODEL_FAULT = 2,
  MODEL_DONE  = 3
} model_mode_t;

/* 最小单元：故意留出 padding 空洞（1+pad+2+4+8） */
typedef struct {
  uint8_t  ch;
  uint16_t idx;
  uint32_t flags;
  double   scale;
} cell_t;

/* 位域组：word/word2 是"手工复算的影子字"，测试用它反查每一位解得对不对 */
typedef struct {
  uint32_t word;                     /* 影子：bits 就是从它切出来的 */
  struct {
    uint32_t on     : 1;             /* bit0      */
    uint32_t level  : 3;             /* bit1..3   */
    uint32_t mode   : 2;             /* bit4..5   */
    uint32_t parity : 1;             /* bit6      */
    uint32_t rev    : 9;             /* bit7..15  */
    uint32_t spare  : 16;            /* bit16..31 */
  } bits;
  int32_t  scratch;                  /* 普通成员（把两个位域组隔到不同存储单元） */
  struct {
    int32_t  bias : 6;               /* **有符号位域**：负数要能解出 -32..31 */
    uint32_t tag  : 10;
    uint32_t rest : 16;
  } sbits;
  uint32_t word2;                    /* 影子：sbits 从它切出来 */
} flags_t;

typedef struct node_s {
  uint32_t       id;
  struct node_s *next;               /* 链表：最后一个指向 0 */
  char           name[12];           /* C 字符串 */
  cell_t         cell;               /* 结构体套结构体 */
} node_t;

/* 联合体：同一段内存的四种看法 */
typedef union {
  uint32_t raw;
  struct { uint16_t lo; uint16_t hi; } halves;
  float    as_f32;
  uint8_t  bytes[4];
} word_u;

typedef struct {
  uint32_t     magic;
  model_mode_t mode;
  flags_t      flags;
  word_u       word;
  node_t       nodes[4];             /* 结构体数组 */
  node_t      *head;                 /* 指向 nodes[0]，再顺着 next 走 */
  cell_t       grid[3][2];           /* 二维结构体数组 */
  double       matrix[2][2];         /* f64 矩阵 */
  const char  *label;                /* 指向 .rodata 里的字符串 */
  uint8_t      blob[8];              /* 不可打印字节：应退回 hex */
  int16_t      delta;                /* 有符号窄整型 */
  char         tag[8];               /* 短字符串（可能没有 NUL 结尾 → 要能兜住） */
} model_t;

extern volatile model_t  g_model;        /* 主对象：监视窗口展开的那一棵 */
extern model_t           g_model_plain;  /* 非 volatile 对照（同一套字段） */
extern const model_t     g_model_const;  /* const 限定对照（住 .rodata / flash） */
extern volatile uint32_t g_model_epoch;  /* 每次 update 自增：看"内存是不是活的" */
extern volatile uint32_t g_model_bf_seed;

void     model_init(void);
uint32_t model_update(uint32_t step);
void     model_bitfield_touch(uint32_t n);
uint32_t model_checksum(void);

#endif /* MODEL_H */
