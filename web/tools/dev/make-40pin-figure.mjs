/**
 * 生成 README 开头的「HPM5301EVKLite J3 40pin 引脚定义图」。
 *   node tools/dev/make-40pin-figure.mjs
 * 产出：docs/shots/40pin-j3.html（中间件，方便手改）+ docs/shots/40pin-j3.png
 *
 * 数据来源（两处对齐，别凭记忆改）：
 *   · 引脚表：官方 UG `HPM5301EVKLite_UG_V1.1.pdf` 表 2 + 官方原理图（akaLinkPro
 *     `docs/spi-bridge-wiring.md` §1/§2 是它的抄录，本文件的角色标记以**本仓当前实现**为准）；
 *   · 可用性标记：本仓 `app/spi/protocol.js` 的 `PADS` 表 + 2026-09-30/10-02 的 LA/真机实测结论。
 *
 * 🚨 标记口径（与页面里的引脚下拉一致，改了那边记得同步这张图）：
 *   ★ 桥固定占用（SPI2 的 SCLK/MISO/MOSI/CS；quad 档再加 PB14/PB15）
 *   ○ 空闲，可当辅助脚（DC/RST/BL/CS_AUX/TE）
 *   △ 能用但要留意（按键脚 / 板上 10k 上拉网络）
 *   ⛔ 别用（log 口 / PIOC 域不支持 / 被板载电路按住 / 板载 LED 任务占用）
 *   ● 电源与地、CDC 虚拟串口（VCOM）
 */
import { writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');

/* 40 个脚：n=脚号, silk=板载丝印, mcu=MCU 脚, role=现在的角色, k=标记类别 */
const ODD = [
  [1, '3V3', '', '3.3V 电源（给屏供电）', 'pwr'],
  [3, 'I2C_SDA', 'PB09', 'CDC 虚拟串口 RX（UART2_RXD）', 'pwr'],
  [5, 'I2C_SCL', 'PB08', 'CDC 虚拟串口 TX（UART2_TXD）', 'pwr'],
  [7, 'GPIO', 'PA02', '空闲 · 辅助脚（推荐 RST）', 'ok'],
  [9, 'GND', '', '地', 'pwr'],
  [11, 'GPIO', 'PA31', '空闲 · 辅助脚（推荐 BL）', 'ok'],
  [13, 'GPIO', 'PB11', 'SCLK（SPI2_SCLK，固定）', 'fix'],
  [15, 'NC', '', '未连接', 'nc'],
  [17, '3V3', '', '3.3V 电源（给屏供电）', 'pwr'],
  [19, 'SPI_MOSI', 'PA29', '空闲（原 SPI1 MOSI）· 板上有 10k 上拉，可当辅助脚', 'warn'],
  [21, 'SPI_MISO', 'PA28', '空闲（原 SPI1 MISO）', 'ok'],
  [23, 'SPI_SCLK', 'PA27', '空闲（原 SPI1 SCLK）', 'ok'],
  [25, 'GND', '', '地', 'pwr'],
  [27, 'GPIO', 'PB12', 'D1 / MISO（SPI2_MISO，固定）', 'fix'],
  [29, 'GPIO', 'PY00', 'PIOC 域 —— 固件 v1 不支持', 'bad'],
  [31, 'GPIO', 'PY01', 'PIOC 域 —— 固件 v1 不支持', 'bad'],
  [33, 'GPIO', 'PA10', '板载 LED 任务每 50 ms 写它 → 出不来持续电平', 'bad'],
  [35, 'NC', '', '未连接', 'nc'],
  [37, 'GPIO', 'PA30', 'USB0_PWR：被板上 Q1 常态短到地，拉不动', 'bad'],
  [39, 'GND', '', '地', 'pwr'],
];
const EVEN = [
  [2, '5V', '', '5V（一般不用，屏多是 3.3V）', 'pwr'],
  [4, '5V', '', '5V', 'pwr'],
  [6, 'GND', '', '地', 'pwr'],
  [8, 'UART_TXD', 'PB15', 'D3 / IO3（quad 四线档用）', 'fix'],
  [10, 'UART_RXD', 'PB14', 'D2 / IO2（quad 四线档用）', 'fix'],
  [12, 'NC', '', '未连接', 'nc'],
  [14, 'GND', '', '地', 'pwr'],
  [16, 'NC', '', '未连接', 'nc'],
  [18, 'NC', '', '未连接', 'nc'],
  [20, 'GND', '', '地', 'pwr'],
  [22, 'NC', '', '未连接', 'nc'],
  [24, 'SPI_CS0', 'PA26', '空闲（原 SPI1 显示 CS）· 辅助脚', 'ok'],
  [26, 'SPI_CS1', 'PB10', 'CS（SPI2_CS，固定）', 'fix'],
  [28, 'GPIO', 'PB13', 'D0 / MOSI（SPI2_MOSI，固定）', 'fix'],
  [30, 'GND', '', '地', 'pwr'],
  [32, 'GPIO', 'PA09', '空闲 · TinyUF2 按键脚，慎用', 'warn'],
  [34, 'GND', '', '地', 'pwr'],
  [36, 'UART_LOG_TX', 'PA00', 'log_printf 占用（UART0 控制台）', 'bad'],
  [38, 'UART_LOG_RX', 'PA01', 'log_printf 占用（UART0 控制台）', 'bad'],
  [40, 'NC', '', '未连接', 'nc'],
];

const cell = ([n, silk, mcu, role, k]) => `
      <div class="pin k-${k}">
        <span class="n">${n}</span>
        <span class="silk">${silk || '&nbsp;'}</span>
        <span class="mcu">${mcu || ''}</span>
        <span class="role">${role}</span>
      </div>`;

const row = i => `
    <div class="row">${cell(ODD[i])}${cell(EVEN[i])}</div>`;

const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>HPM5301EVKLite J3 40pin</title>
<style>
  :root{
    --line:#d6dae1; --fg:#1b1f24; --fg2:#5a6472; --bg:#fff;
    --fix:#1f6feb; --ok:#1a7f37; --warn:#9a6700; --bad:#b42318; --pwr:#6b7280; --nc:#a8b0bb;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:13px/1.35 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  .wrap{width:1120px;padding:20px 22px 16px}
  h1{margin:0 0 2px;font-size:19px;letter-spacing:.2px}
  .sub{color:var(--fg2);font-size:12px;margin-bottom:12px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:0 18px}
  .pin{display:grid;grid-template-columns:30px 84px 46px 1fr;align-items:center;
    gap:6px;border-bottom:1px solid var(--line);border-left:3px solid var(--nc);
    padding:3.5px 6px;min-height:23px}
  .n{color:var(--fg2);font:12px/1 ui-monospace,Consolas,monospace;text-align:right}
  .silk{font:11.5px/1 ui-monospace,Consolas,monospace;color:var(--fg2);overflow:hidden;white-space:nowrap}
  .mcu{font:12px/1 ui-monospace,Consolas,monospace;font-weight:600}
  .role{font-size:12px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
  .k-fix{border-left-color:var(--fix)}   .k-fix .role{color:var(--fix)}
  .k-ok{border-left-color:var(--ok)}     .k-ok .role{color:var(--ok)}
  .k-warn{border-left-color:var(--warn)} .k-warn .role{color:var(--warn)}
  .k-bad{border-left-color:var(--bad)}   .k-bad .role{color:var(--bad)}
  .k-pwr{border-left-color:var(--pwr)}   .k-pwr .role{color:var(--fg2)}
  .k-nc{border-left-color:#e6e9ee}       .k-nc .role{color:var(--nc)}
  .legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;font-size:11.5px;color:var(--fg2)}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
  .note{margin-top:9px;font-size:11.5px;color:var(--fg2);line-height:1.6}
  .note b{color:var(--fg)}
  .hw{margin-top:10px;border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#fafbfc;
      font:11.5px/1.75 ui-monospace,Consolas,monospace;color:#24292f;white-space:pre}
</style></head>
<body><div class="wrap">
  <h1>HPM5301EVKLite · J3 40pin 引脚定义</h1>
  <div class="sub">俯视（1 / 2 脚在 USB 那一端）· 标记口径与页面里的「引脚分配图」一致 ·
    星号脚是 SPI/QSPI 桥固定占用，换不了；圆圈脚是辅助脚（DC / RST / BL / CS_AUX / TE）可以随便挑</div>
${ODD.map((_, i) => row(i)).join('')}
  <div class="legend">
    <span><i style="background:var(--fix)"></i>★ 桥固定占用（SPI2）</span>
    <span><i style="background:var(--ok)"></i>○ 空闲，可当辅助脚</span>
    <span><i style="background:var(--warn)"></i>△ 能用但要留意</span>
    <span><i style="background:var(--bad)"></i>⛔ 别用</span>
    <span><i style="background:var(--pwr)"></i>● 电源 / 地 / VCOM</span>
  </div>
  <div class="note">
    <b>实测推荐</b>：RST = <b>J3[7] PA02</b>、BL = <b>J3[11] PA31</b>（两根都用逻辑分析仪量过，电平干净）。
    接屏时 <b>GND 必须接</b>（6 / 9 / 14 / 20 / 25 / 30 / 34 / 39 任一根），VCC 接 <b>J3[1] 或 J3[17]</b> 的 3V3。
  </div>
  <div class="hw">4 线 SPI + DC（AXS15352 档 1）   SCLK=J3[13]  MOSI=J3[28]  CS=J3[26]  DC=J3[24]  RST=J3[7]  BL=J3[11]
QSPI 四线（ST77916 档 2）     SCLK=J3[13]  D0=J3[28]  D1=J3[27]  D2=J3[10]  D3=J3[8]  CS=J3[26]  RST=J3[7]  BL=J3[11]
外接 SPI NOR flash（四线）     CS=J3[26]  SCLK=J3[13]  IO0=J3[28]  IO1=J3[27]  IO2=J3[10]  IO3=J3[8]
回环自检（一根跳线）           J3[28] MOSI ──跳线── J3[27] MISO</div>
</div></body></html>`;

mkdirSync(join(root, 'docs', 'shots'), { recursive: true });
const htmlPath = join(root, 'docs', 'shots', '40pin-j3.html');
writeFileSync(htmlPath, html);
console.log('写出 ' + htmlPath);

/* 渲染成 PNG：无头 Chrome/Edge 截图（本仓的既有做法，见 memory 里"PDF/公式渲染标准管道"）。
 * 🚨 `--user-data-dir` 必须给一个**独立** profile：共用已开着的浏览器 profile 时第二次跑不出文件。 */
const BROWSERS = [
  join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Bin', 'chrome.exe'),
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(p => { try { return existsSync(p); } catch { return false; } });

if (!BROWSERS.length){
  console.log('⚠ 没找到 Chrome/Edge，HTML 已生成，自己截个图存成 docs/shots/40pin-j3.png 即可');
  process.exit(0);
}
const pngPath = join(root, 'docs', 'shots', '40pin-j3.png');
const { spawnSync } = await import('node:child_process');
const r = spawnSync(BROWSERS[0], [
  '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=2',
  '--window-size=1120,716',
  '--user-data-dir=' + join(tmpdir(), 'chrome-40pin-fig'),
  '--screenshot=' + pngPath,
  'file:///' + htmlPath.replace(/\\/g, '/'),
], { stdio: 'ignore', timeout: 60000 });
console.log(r.status === 0 && existsSync(pngPath) ? '写出 ' + pngPath : '⚠ 截图失败（status=' + r.status + '），HTML 还在，可手动截');

