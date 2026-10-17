/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : RTT 吞吐测试固件（基于板子自带 SDK demo 改造）
  ******************************************************************************
  * 这个文件是从 STM32H7B0VBT6 KIT 的官方 demo 改的：
  *   来源：<板子 SDK>\SDK\DEMO\USART\Core\Src\main.c
  *   改动：**只动主循环** ——
  *     · 去掉 USART/GPIO/BSP 相关代码（测 RTT 用不上，少一个变量）
  *     · 主循环改成 SEGGER RTT 死循环发 "hello world!\n"（BLOCK_IF_FIFO_FULL）
  *     · 加几个全局量给主机侧对账（g_bytes / g_loops / g_ms / g_sysclk_hz / g_clk_src）
  *   时钟部分 **SystemClock_Config() 一字未改** —— 这就是用 demo 当底座的意义：
  *   HSE 25MHz → DIVM1=5 → 5MHz → ×112 → 560MHz VCO → /2 = 280MHz，VOS0，FLASH_LATENCY_7，
  *   全是 ST 官方验证过的写法（尤其是 HAL_PWREx_ConfigSupply 里"供电配置已锁定就跳过"的判断，
  *   手写寄存器版最容易在这儿栽 —— 见 README 踩坑第 12 条）。
  *
  * 为什么 RTT 能反映吞吐：BLOCK_IF_FIFO_FULL 下缓冲满就阻塞，
  *   于是**目标写多快完全由主机取多快决定**，主机读到的字节/秒就是 RTT 实际吞吐。
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "SEGGER_RTT.h"
#include <stdio.h>
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */
/* 主机侧读这几个量来交叉验证：目标到底写了多少、还活着没、跑在什么时钟上 */
volatile uint32_t g_bytes;        /* 目标写出去的字节数（阻塞模式下应 ≈ 主机读到的） */
volatile uint32_t g_loops;        /* 循环次数（×13 = g_bytes） */
volatile uint32_t g_ms;           /* SysTick 毫秒数：**不涨才是真卡死** */
volatile uint32_t g_sysclk_hz;    /* HAL 维护的 SystemCoreClock（实际生效频率） */
volatile uint32_t g_clk_src;      /* 0 = HSE→PLL（正常），1 = HSI→PLL（后备） */
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{
  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* Enable I-Cache---------------------------------------------------------*/
  SCB_EnableICache();

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* USER CODE BEGIN 2 */
  /* HAL 在 HAL_RCC_ClockConfig 里会维护 SystemCoreClock，直接读它就知道实际主频 */
  g_sysclk_hz = SystemCoreClock;
  g_clk_src = (RCC->CR & RCC_CR_HSERDY) ? 0u : 1u;      /* 有 HSE 就是 0，说明走的晶振 */

  SEGGER_RTT_Init();
  SEGGER_RTT_WriteString(0, "\r\n=== RTT 吞吐测试（STM32H7B0 / HAL demo 版）: BLOCK_IF_FIFO_FULL ===\r\n");
  {
    char info[96];
    int n = snprintf(info, sizeof(info), "clock: %lu Hz  src=%s\r\n",
                     (unsigned long)g_sysclk_hz, g_clk_src == 0 ? "HSE 25MHz->PLL" : "HSI->PLL");
    SEGGER_RTT_Write(0, info, (unsigned)n);
  }
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  static const char msg[] = "hello world!\n";          /* 13 字节 */
  while (1)
  {
    unsigned n = SEGGER_RTT_Write(0, msg, sizeof(msg) - 1);
    g_bytes += n;                                      /* 阻塞模式下 n 恒等于 13 */
    g_loops++;
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  *
  * ⚠️ 以下内容与 STM32H7B0VBT6 KIT 官方 demo 完全一致，**不要改**：
  *    HSE 25MHz /5 = 5MHz 参考 → ×112 = 560MHz VCO → /2 = 280MHz；
  *    PLLRGE = 4~8MHz 档、PLLVCOSEL = 宽量程、VOS0、Flash 7 等待周期。
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Supply configuration update enable
  */
  HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY);

  /** Configure the main internal regulator output voltage
  */
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE0);

  while(!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) {}

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = 5;
  RCC_OscInitStruct.PLL.PLLN = 112;
  RCC_OscInitStruct.PLL.PLLP = 2;
  RCC_OscInitStruct.PLL.PLLQ = 2;
  RCC_OscInitStruct.PLL.PLLR = 2;
  RCC_OscInitStruct.PLL.PLLRGE = RCC_PLL1VCIRANGE_2;
  RCC_OscInitStruct.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
  RCC_OscInitStruct.PLL.PLLFRACN = 0;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2
                              |RCC_CLOCKTYPE_D3PCLK1|RCC_CLOCKTYPE_D1PCLK1;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.SYSCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB3CLKDivider = RCC_APB3_DIV2;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_APB1_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_APB2_DIV2;
  RCC_ClkInitStruct.APB4CLKDivider = RCC_APB4_DIV2;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_7) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN 4 */

/**
  * @brief 覆写 HAL 的 SysTick 心跳（HAL 里是 __weak 定义）。
  *        这样**不用改 demo 的 stm32h7xx_it.c**，就顺手把毫秒数记进 g_ms ——
  *        主机读 g_ms 不涨 = 目标真卡死（阻塞在 RTT 写里时它照样涨）。
  */
void HAL_IncTick(void)
{
  uwTick += (uint32_t)uwTickFreq;
  g_ms++;
}

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* 出错就停机：时钟配不起来时**不要**继续往下跑（跑飞比停下更难查） */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}

#ifdef  USE_FULL_ASSERT
/**
  * @brief  HAL 的 assert_param 失败时进这里（hal_conf.h 里开了 USE_FULL_ASSERT）。
  *         用 RTT 喊一声再停机 —— 参数配错的现场比"莫名其妙跑飞"好查得多。
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  (void)file; (void)line;
  SEGGER_RTT_WriteString(0, "\r\n!! assert_failed (HAL 参数不合法)\r\n");
  __disable_irq();
  while (1)
  {
  }
}
#endif /* USE_FULL_ASSERT */
