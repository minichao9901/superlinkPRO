/*********************************************************************
*        SEGGER RTT 配置（STM32F103 测试固件用）
*
* 与 ESP32-S31 那份的区别：裸机、单线程（都在主循环里读写），
* 所以 LOCK/UNLOCK 空着；如果需要从中断里也写 RTT，再加 PRIMASK 关中断。
*
*   BUFFER_SIZE_UP 12288 —— ch0 用；主机轮询有间隔，缓冲要够大：每次取数拿满整个环，
*     开机日志才不会被丢。ch1 的缓冲在 main.c 里单独给（1024）。
*   BUFFER_SIZE_DOWN 256 —— 命令行输入够用。
*   NO_BLOCK_SKIP —— 日志绝不阻塞目标（宁可丢日志也不能让固件卡住）。
*********************************************************************/
#ifndef SEGGER_RTT_CONF_H
#define SEGGER_RTT_CONF_H

#define BUFFER_SIZE_UP                  (12288)   /* 12 KB: 4 KB 时每次取数都撞满，吞吐被轮询次数吃掉 */
#define BUFFER_SIZE_DOWN                (256)
#define SEGGER_RTT_MAX_NUM_UP_BUFFERS   (2)
#define SEGGER_RTT_MAX_NUM_DOWN_BUFFERS (1)
#define SEGGER_RTT_MODE_DEFAULT         SEGGER_RTT_MODE_BLOCK_IF_FIFO_FULL
#define SEGGER_RTT_LOCK()
#define SEGGER_RTT_UNLOCK()

#endif
