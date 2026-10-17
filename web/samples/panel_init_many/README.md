# SF32 屏幕驱动初始化档案（panel_init）

从 `SiFli-SDK-main/customer/peripherals/` 下全部 **44 份 LCD 面板驱动**（40 种面板，含重复变体）提取的初始化数据主档案。

## 主档案

- **`panel_init/panel_init.json`** —— 最终主档案（随本文件夹整体拷走即可；工程根目录 `panel_init.json` 是同内容副本）：
  ```json
  {
    "meta": { ... 统计与生成时间 ... },
    "drivers": [ { ...44 条 driver 记录... } ]
  }
  ```
- `panel_init/schema.json` —— JSON Schema（draft-07），可用 `ajv validate -s panel_init/schema.json -d panel_init.json`（对 `drivers` 数组部分）做严格校验。
- `panel_init/SPEC.md` —— 字段语义与提取规则（AI 使用前建议先读）。

## 单条 driver 记录结构（要点）

| 字段 | 含义 |
|---|---|
| `id` | 驱动目录名（唯一）；`registered_name`=注册名；`variant`=同名驱动的变体目录 |
| `hor_res`/`ver_res` | 面板分辨率；来自板级宏/无宏时为 `null`（看 notes） |
| `interface_options[]` | 每套静态接口配置：`interface`(归一化)、`freq_hz`、`color_mode`、`timing`(DPI/DSI_VIDEO/JDI)、`guard`、`selected` |
| `init_sequences[]` | 每套完整初始化流程：`steps[]` 有序步骤 |
| `display_on_sequence`/`display_off_sequence` | 点亮/熄屏步骤 |
| `notes[]` | 所有源码异常、死代码、歧义点的说明 |

### steps 类型

- `{"type":"cmd","cmd":"0x11","data":["0x00"]}` —— 写命令（bytes 为 hex 字符串，含计算后的实际字节，`expr` 可选保留原表达式）
- `{"type":"delay","ms":120}` / `{"type":"delay_us","us":10}`
- `{"type":"reset","state":0|1}` —— `BSP_LCD_Reset`
- `{"type":"comment","text":"..."}` —— 需要保留的上下文说明

## 还原/使用方法

- **生成 C 初始化表**：从 JSON 还原很简单——`cmd` 写 `LCD_WriteReg(hlcdc, cmd, data, len)`，`delay` 写 `LCD_DRIVER_DELAY_MS(n)`，依次排入 `LCD_Init`。可用 `panel_init/merge.ps1`（合并）与 `panel_init/verify.ps1`（与源码交叉核对）作参考实现。
- **换屏适配**：新面板若 IC 在该库中（40 种），直接拉取对应记录的 `init_sequences[0].steps` + `interface_options`；若需不同的 gamma/MADCTL，修改对应 `cmd` 的 `data` 即可。
- **严格校验**：`cmd`/`data` 为 `0xNN` hex 字符串（如 `0x11`、`0xd0`；16 位命令如 `0x2900` 保留 4 位）；频率、分辨率、延时等非字节域为十进制。`data` 长度即实际写入参数长度（不含表内零填充）。

## 验证情况

- 11 批并行提取 → 合并 44 条 → 0 个结构问题；全部记录与源文件 `LCD_DRIVER_EXPORT2` 注册名交叉核对一致。
- 深度抽检：`st7789v`（内联写，18+18 步两条序列，寄存器宏与延时逐条核对）、`rm6d010`（扁平字节流 308 对，逐字节核对）、`axs15231b/e`、`st77916`（确认 DBI 配置为 `#if 0` 死代码）、41 处脚本级交叉差异全部溯源为核对脚本正则盲区（`QAD_SPI_ITF` 宏、运行时 `lcdc_int_cfg_spi.` 配置、`.h` 中的分辨率、DPI 时序字段），非数据错误。

## 生成物清单

```
panel_init/panel_init.json        主档案（44 drivers / 50 init_sequences / 4361 steps）
panel_init/SPEC.md                提取规格与字段语义
panel_init/schema.json            JSON Schema
panel_init/merge.ps1              合并脚本（parts → panel_init.json + hex 转换 + 基础校验）
panel_init/verify.ps1             与源码交叉核对脚本
panel_init/extract_tables.ps1     机械表提取器（表/配置/分辨率 dump）
panel_init/dumps/<id>.json        44 份机械解析 dump（对照底稿）
panel_init/parts/partNN.json      11 批提取原始结果（44 条记录）
（工程根目录 panel_init.json = 同内容副本）
```
