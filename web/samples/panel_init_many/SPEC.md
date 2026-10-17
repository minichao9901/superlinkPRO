# SF32 屏驱动提取规格（SPEC）

## 目标

从 `SiFli-SDK-main/customer/peripherals/` 下每个 LCD 面板驱动 `.c` 文件中，提取该面板的**完整初始化流程**（有序命令序列 + 延时 + 复位）以及元数据（分辨率、接口配置、颜色格式、时序、变体、备注），输出为 JSON 数组元素。

每个驱动文件 = 1 条 driver 记录。文件内若有多套初始化表或多接口配置，都属于同一条记录的子项。

## 关键背景

- 驱动注册宏 `LCD_DRIVER_EXPORT2(name, id, init_cfg, dev_ops, pixel_align)` 定义于 `rtos/rtthread/bsp/sifli/drivers/drv_lcd.h`。
- 写命令函数：`LCD_WriteReg(hlcdc, cmd, param, len)`。延时：`HAL_Delay_us` / `HAL_Delay` / `LCD_DRIVER_DELAY_MS`。复位：`BSP_LCD_Reset(0/1)`。
- 常见初始化表模式：{% raw %}`static const uint8_t 表名[][MAX_CMD_LEN] = {{cmd, len, p0..p5}, ...}`{% endraw %}，消费循环一般写作
  `LCD_WriteReg(hlcdc, 表[i][0], (uint8_t *)&表[i][2], 表[i][1])`（**必须以该文件实际消费代码为准**，有些文件用别的偏移/写法）。

## driver 对象字段

| 字段 | 说明 |
|---|---|
| `id` | 驱动目录名（唯一），如 `st7789v`、`gc9a01` |
| `registered_name` | `LCD_DRIVER_EXPORT2` 的第一个参数（导出名） |
| `ic` | 面板驱动 IC 名（按 export 名/文件注释，如 `ST7789V`、`ICN3311`；注意 `gc9a01` 目录实际是 ST7789V_GMT024） |
| `panel` | 具体屏名（注释/宏中出现，如 `H034A01 800x800`、`GMT024`），无则 `null` |
| `variant` | 同一 registered_name 出现在多个文件时填目录名，否则 `null` |
| `file` | 相对路径，正斜杠，如 `customer/peripherals/st7789v/st7789v.c` |
| `hor_res` / `ver_res` | 从文件中的 `THE_LCD_PIXEL_WIDTH/HEIGHT` 或 `LCD_IC_PIXEL_*` 等宏解析出的整数；若分辨率来自板级宏（`LCD_HOR_RES_MAX` 等）则填 `null` 并在 notes 说明 |
| `pixel_align` | EXPORT2 最后一个参数（整数） |
| `interface_options` | 数组中每个静态 `LCDC_InitTypeDef` 一条（见下节） |
| `init_sequences` | 每套初始化流程一项（见下节） |
| `display_on_sequence` / `display_off_sequence` | `LCD_DisplayOn`/`LCD_DisplayOff` 中的步骤数组，空数组 `[]` |
| `notes` | 字符串数组：无初始化表的纯时序屏、多表条件不明、分辨率来自板级宏、运行时动态选接口、被注释的旧表、表外 0x11/0x29 写入等一切需要注意的点 |

### interface_options 条目

```json
{
  "label": "lcdc_int_cfg" | "lcdc_int_cfg_dsi" | ...,
  "guard": "#ifdef BSP_LCDC_USING_DSI" 或 null,
  "selected": true/false,
  "interface": "spi_dcx_1data",
  "freq_hz": 40000000,
  "color_mode": "rgb565",
  "timing": { ... }   // 仅 DPI/DSI_VIDEO/JDI 类填 .cfg 内容（字段名保留原样，值转数字），其余 null
}
```

- `interface` 归一化表：
  `LCDC_INTF_DSI`→`dsi`，`LCDC_INTF_DSI_VIDEO`→`dsi_video`，`LCDC_INTF_SPI_NODCX_1DATA`→`spi_nodcx_1data`，`SPI_NODCX_2DATA`→`spi_nodcx_2data`，`SPI_NODCX_4DATA`→`spi_nodcx_4data`，`SPI_DCX_1DATA`→`spi_dcx_1data`，`SPI_DCX_2DATA`→`spi_dcx_2data`，`SPI_DCX_4DATA`→`spi_dcx_4data`，`LCDC_INTF_SPI_DCX_4DATA_AUX`→`spi_dcx_4data_aux`，`LCDC_INTF_DBI_8BIT_B`→`dbi_8bit_b`，`LCDC_INTF_JDI_PARALLEL`→`jdi_parallel`，`AUTO_SELECTED_DPI_INTFACE`→`dpi`（其余遇到的原样保留字符串并加进 notes）。
- `color_mode`：`LCDC_PIXEL_FORMAT_RGB565`→`rgb565`、`RGB888`→`rgb888`、`RGB666`→`rgb666`（其余原样保留）。
- `selected`：按 ifdef 链判断 LCD_Init 默认路径 memcpy 的是哪个；运行时动态选（如 `lcdc_int_cfg_spi` 由 msh 参数改）的配置全为 `false` 并在 notes 说明。

### init_sequences 条目

```json
{
  "label": "default",              // 一套流程一个唯一名
  "guard": null 或 "#if defined(...)",
  "source": "table lcd_init_cmds" | "inline LCD_Init" | "table xxx + extra writes",
  "table_name": "lcd_init_cmds" 或 null,
  "steps": [ ... ]
}
```

**steps 元素类型**（按实际执行顺序、完整列出该套流程的全部操作：复位、延时、表内命令、表外命令）：

```json
{"type":"cmd","cmd":"0x11","data":["0x00"],"expr":"可选：原始C表达式"}
{"type":"delay","ms":120}          // HAL_Delay / LCD_DRIVER_DELAY_MS
{"type":"delay_us","us":10}
{"type":"reset","state":0}         // BSP_LCD_Reset(0) 拉低
{"type":"comment","text":"..."}
```

> **字节域一律用 hex 字符串**：`cmd` / `data` 写成 `0xNN`（小写，如 `0xd0`）；命令值 > 0xFF 时保留实际宽度（如 FT2308 的 16 位命令 `0x2900`）。频率、分辨率、延时（ms/us）、pixel_align 等**非字节域保持十进制整数**。

展开规则：

- 表行：`row[0]`=命令、`row[1]`=参数个数 len、`row[2..2+len-1]`=参数，参数只取前 len 个（表内填充的 0 若超过 len 不取）。
- 内联写命令（如 st7789v 直接 `LCD_WriteReg(hlcdc, REG_X, parameter, 4)`）：跟随调用点前面 `parameter[k] = ...` 的赋值，**计算成实际字节值**；含 `THE_LCD_PIXEL_WIDTH-1` 之类表达式时算出数值，并可在 `expr` 里保留原表达式。注意 `parameter[]` 数组被复用，值以写入时刻为准。
- `LCD_ReadData`/`LCD_ReadID` 等读操作**不记录**（需要说明放 notes）。
- 若断电时序/上电等待（如 `LCD must at sleep ... 100ms`）以注释形式存在于代码，转成 `{"type":"delay"}` 或 `{"type":"comment"}` 如实记录。

### 多表变体

- 一个文件若有多张初始化表由预处理条件选择（如 st7701s 的 `lcd_init_cmds1/2`、st77903/gc9b72/spd2012 的两张表），每张表对应的整套流程各占一个 `init_sequences` 条目，`guard` 写实际 `#if defined(...)` 条件；条件无法确定时写 `null` 并在 notes 说明。
- 表外共用的步骤（如 0x11+延时、0x29+延时）要按源码顺序并入每套流程。

## 准确性铁律

1. 不得臆造任何数值；每个命令/字节都必须在原文件对应处可溯源。
2. `#if 0`、被注释的旧表/旧配置不进入主数据，但要在 notes 提及。
3. JSON 必须合法：UTF-8、无注释、无尾逗号；命令与参数字节一律 hex 字符串（`0xNN`，小写；命令 >0xFF 保留 4 位如 `0x2900`），其余数值字段用十进制。
4. 拿不准时用 best-effort 遵循上述规则，并在 notes 明确记录，不得悄悄略过。

## 提交

- 用 write 工具把数组写入 `panel_init/parts/partNN.json`（NN 为批次号，由任务 prompt 指定）。
- 最终回复只汇报摘要（每个文件的 id、init_sequences 数、steps 总数、interface_options 数、异常点），**不要粘贴 JSON 正文**。
