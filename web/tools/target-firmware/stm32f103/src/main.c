/*
 * STM32F103 测试固件：给「串口 / RTT 工具箱」当靶子用。
 *
 * 一条命令通道，两种出口：
 *   · UART0 = USART1，PA9(TX)/PA10(RX)，115200 8N1（HSI 8MHz 直出，不用 PLL）
 *   · SEGGER RTT：ch0 "Terminal"（带 ANSI 颜色，用来试终端渲染）
 *                 ch1 "Log"（纯文本，用来试第二个通道）
 *   命令既能从串口敲，也能从 RTT 下行通道敲；输出同时进串口和 RTT ch0。
 *
 * 命令：help / info / uptime / echo <文本> / led on|off|toggle /
 *       hex（8 字节二进制）/ ansi（彩色）/ long（长行）/ flood [KB] / reboot
 */
#include <stdarg.h>
#include <stdint.h>
#include <string.h>

#include "stm32f103_regs.h"
#include "SEGGER_RTT.h"

#define FW_NAME    "STM32F103 RTT/UART test"
#define FW_VER     "1.0"

volatile uint32_t g_ms;
void SysTick_Handler(void){ g_ms++; }

static char g_logBuf[1024];              /* ch1 自己带的缓冲 */
static int  g_led = 0;

/* ------------------------------------------------------------------ */
/* 输出                                                               */
/* ------------------------------------------------------------------ */
static int num2str(char *dst, uint32_t v, int base, int upper){
  char tmp[12];
  int n = 0;
  const char *dig = upper ? "0123456789ABCDEF" : "0123456789abcdef";
  if (!v) tmp[n++] = '0';
  while (v){ tmp[n++] = dig[v % base]; v /= base; }
  for (int i = 0; i < n; i++) dst[i] = tmp[n - 1 - i];
  return n;
}

static int vfmt(char *buf, int cap, const char *fmt, va_list ap){
  int o = 0;
  for (const char *p = fmt; *p && o < cap - 1; p++){
    if (*p != '%'){ buf[o++] = *p; continue; }
    p++;
    char pad = ' ';
    while (*p == '0'){ pad = '0'; p++; }
    int width = 0;
    while (*p >= '0' && *p <= '9'){ width = width * 10 + (*p++ - '0'); }
    char body[16];
    int bl = 0, neg = 0;
    switch (*p){
      case 'd': {
        int32_t v = va_arg(ap, int32_t);
        if (v < 0){ neg = 1; v = -v; }
        bl = num2str(body, (uint32_t)v, 10, 0);
        break;
      }
      case 'u': bl = num2str(body, va_arg(ap, uint32_t), 10, 0); break;
      case 'x': bl = num2str(body, va_arg(ap, uint32_t), 16, 0); break;
      case 'X': bl = num2str(body, va_arg(ap, uint32_t), 16, 1); break;
      case 's': {
        const char *s = va_arg(ap, const char *);
        if (!s) s = "(null)";
        while (*s && o < cap - 1) buf[o++] = *s++;
        continue;
      }
      case 'c': buf[o++] = (char)va_arg(ap, int); continue;
      case '%': buf[o++] = '%'; continue;
      default:  buf[o++] = '%'; if (*p) buf[o++] = *p; continue;
    }
    int total = bl + neg;
    for (int i = total; i < width && o < cap - 1; i++) buf[o++] = pad;
    if (neg && o < cap - 1) buf[o++] = '-';
    for (int i = 0; i < bl && o < cap - 1; i++) buf[o++] = body[i];
  }
  buf[o] = '\0';
  return o;
}

/**
 * 串口输出。
 * 🚨 两条教训：
 *  ① 必须有忙等上限：DAPLink 的 CDC 串口在没有主机读的时候会堵，死等 TXE 会把整个主循环
 *     （连带 RTT 下行命令处理）卡死 —— 现象是"RTT 日志还在刷，但发命令没反应"。
 *  ② 上限也要**小**：200000 次 ≈ 75ms/字符，一行 60 字符就是 4.5 秒，主循环基本被拖死。
 */
static void uart_write(const char *s, int n){
  for (int i = 0; i < n; i++){
    uint32_t guard = 20000;                        /* ≈7ms @8MHz 的忙等上限 */
    while (!(USART1_SR & USART_SR_TXE) && --guard) { }
    if (!guard) return;                            /* 串口堵住：这段就不要了 */
    USART1_DR = (uint8_t)s[i];
  }
}

/** 同时进 RTT ch0 + 串口。
 *  🚨 **顺序要紧：先写 RTT，再写串口**。串口那一侧可能被 DAPLink 的 CDC 堵住，
 *     反过来（先串口后 RTT）会把 RTT 输出一起饿死 —— 实测现象是"ch1 正常、ch0 一个字节都没有"。 */
static void out(const char *fmt, ...){
  char buf[512];                      /* ⚠️ 别小于 256：help 那种长文本会被静默截断（踩过） */
  va_list ap;
  va_start(ap, fmt);
  int n = vfmt(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  SEGGER_RTT_Write(0, buf, n);
  uart_write(buf, n);
}

/** 只进串口 */
static void outuart(const char *fmt, ...){
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  int n = vfmt(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  uart_write(buf, n);
}

/** 只进 RTT ch1（日志通道） */
static void outlog(const char *fmt, ...){
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  int n = vfmt(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  SEGGER_RTT_Write(1, buf, n);
}

static void out_bytes(const uint8_t *b, int n){
  SEGGER_RTT_Write(0, b, n);
  uart_write((const char *)b, n);
}

/* ------------------------------------------------------------------ */
/* 硬件                                                               */
/* ------------------------------------------------------------------ */
static void hw_init(void){
  RCC_APB2ENR |= RCC_APB2ENR_AFIOEN | RCC_APB2ENR_IOPAEN | RCC_APB2ENR_IOPCEN | RCC_APB2ENR_USART1EN;

  /* PA9 = 复用推挽 50MHz，PA10 = 浮空输入 */
  GPIOA_CRH = (GPIOA_CRH & ~((0xFu << 4) | (0xFu << 8))) | (0xBu << 4) | (0x4u << 8);
  /* PC13 = 推挽输出 2MHz（Blue Pill 的板载 LED，低有效） */
  GPIOC_CRH = (GPIOC_CRH & ~(0xFu << 20)) | (0x2u << 20);
  GPIOC_BSRR = (1u << 13);                       /* 熄灭（高 = 灭） */

  /* 8MHz / 115200 → BRR = 0x45（实际 115942，误差 0.6%） */
  USART1_BRR = 0x45;
  USART1_CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;

  SYST_RVR = 8000 - 1;                            /* 8MHz → 1ms */
  SYST_CVR = 0;
  SYST_CSR = 7;                                   /* ENABLE | TICKINT | CLKSOURCE */
}

static int uart_getc(void){
  if (USART1_SR & USART_SR_RXNE) return (int)(USART1_DR & 0xFF);
  return -1;
}

static void led_set(int on){
  g_led = on;
  if (on) GPIOC_BRR = (1u << 13);                 /* 低电平点亮 */
  else    GPIOC_BSRR = (1u << 13);
}

/* ------------------------------------------------------------------ */
/* 命令                                                               */
/* ------------------------------------------------------------------ */
static void cmd_flood(int kb){
  static uint8_t chunk[64];
  for (int i = 0; i < 64; i++) chunk[i] = 'A' + (i % 26);
  out("[flood] 灌 %d KB 到 RTT ch0（缓冲 4KB，看主机侧“缓冲读满/溢出丢弃”）\r\n", kb);
  int total = kb * 1024;
  for (int sent = 0; sent < total; sent += sizeof(chunk)){
    SEGGER_RTT_Write(0, chunk, sizeof(chunk));
  }
  out("[flood] 完事，实际写进 %u 字节（SEGGER_RTT_GetAvailWriteSpace 剩余 %u）\r\n",
      (unsigned)total, (unsigned)SEGGER_RTT_GetAvailWriteSpace(0));
}

static void shell(char *line){
  while (*line == ' ') line++;
  if (!*line) return;

  if (!strcmp(line, "help")){
    out("-- STM32F103 测试固件命令 --\r\n"
        "  help            这条帮助\r\n"
        "  info            芯片/时钟/RTT 信息\r\n"
        "  uptime          运行时间\r\n"
        "  echo <文本>     原样回显\r\n");
    out("  led on|off|toggle  PC13 LED\r\n"
        "  hex             发 8 字节二进制（试 HEX 显示）\r\n"
        "  ansi            发 ANSI 彩色（试终端渲染）\r\n"
        "  long            发一条长行（试自动换行）\r\n"
        "  flood [KB]      往 RTT ch0 猛灌数据（默认 32KB）\r\n"
        "  reboot          软复位\r\n");
  } else if (!strcmp(line, "info")){
    out("芯片      : DBGMCU_IDCODE=0x%x  CPUID=0x%x\r\n", (unsigned)DBGMCU_IDCODE, (unsigned)SCB_CPUID);
    out("Flash     : %u KB（0x1FFFF7E0）\r\n", (unsigned)(FLASH_SIZE_REG & 0xFFFF));
    out("主频      : HSI 8 MHz（未开 PLL；UART 由 HSI 直出）\r\n");
    out("UART      : USART1 PA9/PA10 115200 8N1\r\n");
    out("RTT       : ch0 Terminal(4KB, up) / ch1 Log(1KB, up) / ch0 down(256B)\r\n");
    out("固件      : %s v%s 编译于 %s %s\r\n", FW_NAME, FW_VER, __DATE__, __TIME__);
  } else if (!strcmp(line, "uptime")){
    out("uptime: %u ms (%u.%03u s)\r\n", (unsigned)g_ms, (unsigned)(g_ms / 1000), (unsigned)(g_ms % 1000));
  } else if (!strncmp(line, "echo ", 5)){
    out("echo: %s\r\n", line + 5);
  } else if (!strcmp(line, "led on")){ led_set(1); out("LED = ON\r\n"); }
  else if (!strcmp(line, "led off")){ led_set(0); out("LED = OFF\r\n"); }
  else if (!strcmp(line, "led toggle") || !strcmp(line, "led")){ led_set(!g_led); out("LED = %s\r\n", g_led ? "ON" : "OFF"); }
  else if (!strcmp(line, "hex")){
    const uint8_t b[8] = { 0x01, 0x03, 0x00, 0x00, 0x00, 0x0A, 0xC5, 0xCD };
    out("\r\n(bin) ");
    out_bytes(b, sizeof(b));
    out("\r\n");
  } else if (!strcmp(line, "ansi")){
    out("\x1b[31m红\x1b[32m绿\x1b[33m黄\x1b[34m蓝\x1b[35m紫\x1b[36m青\x1b[0m 普通  \x1b[1m粗体\x1b[0m  \x1b[7m反显\x1b[0m\r\n");
    out("\x1b[90m灰色（暗）\x1b[0m  256 色: \x1b[38;5;208m208\x1b[0m \x1b[38;5;45m45\x1b[0m\r\n");
  } else if (!strcmp(line, "long")){
    for (int i = 0; i < 3; i++){
      out("LONG[%d] 0123456789 abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ "
          "汉字也会出现，用来看看换行和编码对不对。\r\n", i);
    }
  } else if (!strncmp(line, "flood", 5)){
    int kb = 32;
    if (line[5] == ' '){ kb = 0; for (char *p = line + 6; *p >= '0' && *p <= '9'; p++) kb = kb * 10 + (*p - '0'); }
    if (kb <= 0) kb = 32;
    cmd_flood(kb);
  } else if (!strcmp(line, "reboot")){
    out("reboot ...\r\n");
    for (volatile int i = 0; i < 200000; i++) { }
    nvic_system_reset();
  } else {
    out("未知命令：%s（敲 help）\r\n", line);
  }
}

/* ------------------------------------------------------------------ */
int main(void){
  hw_init();
  SEGGER_RTT_Init();
  SEGGER_RTT_ConfigUpBuffer(1, "Log", g_logBuf, sizeof(g_logBuf), SEGGER_RTT_MODE_NO_BLOCK_SKIP);

  out("\r\n\x1b[36m=== %s v%s ===\x1b[0m\r\n", FW_NAME, FW_VER);
  out("串口: USART1 PA9/PA10 @115200 8N1    RTT: ch0=Terminal ch1=Log\r\n");
  out("敲 help 看命令。\r\n\r\n");

  char line[128];
  int lp = 0;
  uint32_t t_ansi = 0, t_log = 0, t_hb = 0, n = 0;

  for (;;){
    int c = uart_getc();
    if (c < 0){
      char ch;
      if (SEGGER_RTT_Read(0, &ch, 1) == 1) c = (uint8_t)ch;
    }

    if (c >= 0){
      if (c == '\r' || c == '\n'){
        if (lp){ line[lp] = '\0'; out("\r\n"); shell(line); lp = 0; }
      } else if (c == 0x7f || c == 0x08){
        if (lp){ lp--; out("\b \b"); }
      } else if (c >= 0x20 && lp < (int)sizeof(line) - 1){
        line[lp++] = (char)c;
        out("%c", c);                       /* 设备回显：串口和 RTT 两边都能看见 */
      }
    }

    if (g_ms - t_ansi >= 1000){
      t_ansi = g_ms;
      n++;
      out("\x1b[32m[RTT ch0]\x1b[0m tick=#%u uptime=%u.%03us led=%s\r\n",
          (unsigned)n, (unsigned)(g_ms / 1000), (unsigned)(g_ms % 1000), g_led ? "ON" : "OFF");
    }
    if (g_ms - t_log >= 500){
      t_log = g_ms;
      outlog("[ch1 Log] ms=%u counter=%u\r\n", (unsigned)g_ms, (unsigned)n);
    }
    if (g_ms - t_hb >= 3000){
      t_hb = g_ms;
      outuart("[UART] 心跳 ms=%u（只走串口，用来分辨两路输出）\r\n", (unsigned)g_ms);
    }
  }
}
