/** 时间/数值格式化。 */

export function pad(n, w = 2){ return String(n).padStart(w, '0'); }

/** 页面打开时刻（本模块加载 ≈ 页面打开）：相对时间戳的零点 */
const T0 = Date.now();

/**
 * 时间戳。
 *   `absolute = false`（默认）→ **自页面打开起**的累计时间：`+mm:ss.mmm`（超一小时 `+hh:mm:ss.mmm`）
 *   `absolute = true`          → 当天钟点（绝对时间）：`hh:mm:ss.mmm`
 *
 * 🚨 早先这两个分支**一模一样**（都返回钟点），等于页面上那个「绝对时间」复选框是个摆设
 *    （代码审查抓到的："勾了没有任何效果"）。既然控件在，就把语义做实：
 *    默认给相对时间（排查时序时"这条比上一条晚多久"比钟点有用），勾上给当天钟点。
 */
export function stamp(d = new Date(), absolute = false){
  const hhmmss = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  if (absolute) return `${hhmmss}.${pad(d.getMilliseconds(), 3)}`;
  const el = Math.max(0, d.getTime() - T0);
  const ms = el % 1000;
  const sec = Math.floor(el / 1000);
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
  return `+${h > 0 ? pad(h) + ':' : ''}${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
}

export function bytes(n){
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(2)} MB`;
}

export function rate(n){
  if (n < 1024) return `${Math.round(n)} B/s`;
  return `${(n / 1024).toFixed(1)} KB/s`;
}

/** 文件名安全的时间戳：20260926-153012 */
export function fileStamp(d = new Date()){
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

/** 触发浏览器下载 */
export function download(filename, content, mime = 'text/plain;charset=utf-8'){
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
