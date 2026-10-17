# 面板初始化样例（`samples/panel-init`）

给网页「SPI/QSPI 屏」页（`#panel`）用的**面板初始化序列** —— 全是标准 C 数组，
**整段贴进页面的『面板初始化』大框**就能解析（页面解析器吃的就是 C 数组，注释会被剥掉）。

| 文件 | 屏 | 规模 | 来源 |
|---|---|---|---|
| `gc9a01_init_cmds.h` | GC9A01（圆屏；模块常见规格 1.28 吋 / 240×240）· SPI + DC · RGB565 · 48 MHz | **51 条命令 / 参数 136 B / 延时 360 ms** | 厂商源码 `st7789_gmt024_08_spi8p.c`（SiFli-SDK `customer/peripherals/gc9a01/`，注册名 `ST7789V_GMT024`；同款文件见 [Gitee wiki](https://gitee.com/xu-jianchao/fpga-development/wikis/sf32lb52%E9%A9%B1%E5%8A%A8%EF%BC%9Agc9a01%E7%9A%84%E5%9C%86%E5%B1%8F%EF%BC%88240RGB%EF%BC%89)） |

> 表体 **48 条**来自厂商初始化表（含 `0xEF`/`0xFE`/`0xEF` 解锁与末尾 `0x21` 开反显 —— 厂商源码里标了 `critical`），
> 另外 **3 条表外命令**按驱动 `LCD_Init` 的次序补进来并加了注释：`0x01`(软复位, 120 ms) → `0x11`(sleep out, 120 ms) → …表… → `0x29`(display on)。
> 上屏前别忘了**复位**：RST 低 → 20 ms → RST 高 → ≥120 ms。

## 怎么用

1. 打开网页 →「**SPI/QSPI 屏**」页 → 找到「**面板初始化**」大框；
2. 把整个 `.h` 的内容**整段粘进去**；
3. 点「解析并预览」→ 表里逐条列出命令与参数，可直接「重放全部」；
4. 几何（宽×高）、档位（`spi_dcx` / `qspi`）、RST/BL 脚在页面上另外选。

## 怎么重新生成

由 `tools/dev/panel-json-to-c.mjs` 生成，两种输入都支持：

```powershell
# ① 厂商 C 源码（**推荐**：不会丢零参数命令；GC9A01 用的就是这条）
node tools/dev/panel-json-to-c.mjs --from-c=<厂商的 .c> --id=gc9a01 `
     --before="0x01:120,0x11:120" --after="0x29:0" --last-delay=120 `
     --note="圆屏模块常见规格 1.28 吋 240×240，以你手上模块为准"

# ② dump JSON（44 份现成的，但**会把零参数命令整行丢掉**，脚本会告警）
node tools/dev/panel-json-to-c.mjs gc9a01
node tools/dev/panel-json-to-c.mjs --all          # 44 份全导
```

## ⚠️ 三个坑（生成的文件头注释里也写着）

- **dump JSON 会把"零参数命令"整行丢掉**（2026-10 拿 GC9A01 与厂商源码逐条对出来的）：
  `{0xEF, 0}` / `{0xFE, 0}` / `{0x35, 0}` / `{0x21, 0}` 这类没有数据字节的行全没了 —— 而它们恰恰是
  **解锁寄存器**和**开反显**的关键命令。关键屏请用 `--from-c` 走厂商源码；dump 模式现在会主动告警。
- **表内没有延时项**，也**不含 `0x11`（sleep out）/ `0x29`（display on）** —— SDK 驱动把它们写在
  初始化表**外面**。上屏前自己补：复位（RST 低 → 20 ms → 高 → ≥120 ms）→ `0x11` → 等 ≥120 ms → `0x29`；
  GC9A01 这类 IPS 圆屏模块还要 `0x21` 开**反显**，颜色才正。
- dump 里的 `res_w` / `res_h` 常常是 `null`（分辨率取自板级宏），**别当成已知值**；
  文件头那行"常见规格"只是提示，实际以你手上模块为准。

> 校验方式：把文件交给页面同款解析器（`app/spi/panel-code.js` 的 `parsePanelCode`），
> 断言"51 条 / 参数 136 B / 延时 360 ms / 0 错误"，并钉住那 5 条零参数命令不许再丢 —— 见
> `tools/selftest/spi-panel-code.test.mjs` 的「samples 样例」一节。
