/**
 * 兼容转发层：实现已搬到 `app/core/expr.js`（`#i2c` 与 `#spi` 两个页面共用同一套
 * `as` 解码表达式 —— 它在 SPI 侧用来把 ADC/传感器的读回字节变成有名字的量）。
 *
 * 这个文件**只做转发**，别再往里加实现：新代码请直接 import `../core/expr.js`。
 */
export * from '../core/expr.js';
