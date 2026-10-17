/**
 * 迷你曲线（20 px 高的 sparkline）—— **两个页面的「实时值」共用一份**：
 * `#i2c`（app/i2c/view.js）与 `#spi`（app/spi/view.js）。
 *
 * 为什么抽出来：I2C 页先写了它，SPI 页的定时采集要一模一样的东西（同样的自动量程、同样的
 * "背板宽度跟着 CSS 走"这条纪律）。两处各写一份的话，改一处忘一处就会出现"一个页面曲线糊、
 * 另一个不糊"这种只有肉眼能发现的问题。
 */

/**
 * 画一条迷你折线（自动量程，不用第三方库）。
 * @param {HTMLCanvasElement} cv 画布（高度由 CSS 定，宽度按实际布局取）
 * @param {number[]} buf 采样缓冲（最新的在末尾）
 */
export function drawSpark(cv, buf){
  const ctx = cv.getContext('2d');
  // 背板宽度跟着 CSS 实际宽度走（否则固定宽度被 CSS 拉伸会糊）——
  // 量不到（元素还藏着）就退回上次的值/默认值，别把背板设成 0。
  const cssW = Math.round(cv.getBoundingClientRect().width);
  if (cssW > 0 && cv.width !== cssW) cv.width = cssW;
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--acc') || '#4aa3ff';
  ctx.lineWidth = 1;
  ctx.beginPath();
  let lo = Infinity, hi = -Infinity;
  for (const v of buf){ if (v < lo) lo = v; if (v > hi) hi = v; }
  const span = (hi - lo) || 1;
  for (let i = 0; i < buf.length; i++){
    const x = buf.length > 1 ? (i / (buf.length - 1)) * (W - 2) + 1 : W / 2;
    const y = H - 2 - ((buf[i] - lo) / span) * (H - 4);
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  }
  ctx.stroke();
}
