/** 小型 DOM 助手。 */
export const $ = id => document.getElementById(id);

/**
 * 把一段文本转义成能安全放进 `innerHTML` 的形式。
 *
 * 🚨 什么时候必须用：拼进 innerHTML 的字符串**只要有一点可能来自外部**（ELF 里的变量名、
 *    用户粘贴的面板初始化表、器件名、串口读回来的文本……）就得套一层。
 *    本项目的输入面比一般网页大：ELF / HEX / 用户粘贴的 C 数组 / 目标回来的日志都算外部输入，
 *    不转义的话 `<img onerror=…>` 这类东西会被真的解析执行（self-XSS）。
 *    （2026-10 代码审查：scope/view.js 与 spi/panel-view.js 有三处直接拼 name。）
 *
 * 只转 `& < > "` 四个字符就够（进得去 HTML 文本节点与双引号属性值），别写成 HTML 实体大全。
 */
export const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/**
 * 分段按钮组（HTML 里 <div class="seg" data-group="xxx">），返回可读写的小对象。
 * 独立成函数是因为到处都要用（ASCII/HEX、终端/文本/HEX…）。
 */
export function seg(group, initial, onChange){
  const btns = [...group.querySelectorAll('button')];
  let val = initial;
  const api = {
    get value(){ return val; },
    set(v){
      val = v;
      for (const b of btns) b.classList.toggle('on', b.dataset.v === v);
    },
    enable(on){ for (const b of btns) b.disabled = !on; },
  };
  for (const b of btns){
    b.addEventListener('click', () => {
      if (b.dataset.v === val) return;
      api.set(b.dataset.v);
      onChange?.(val);
    });
  }
  api.set(initial);
  return api;
}

/** 顶栏状态标签 */
export function setFlag(el, text, kind){
  el.textContent = text;
  el.className = 'flag ' + (kind === 'on' ? 'flag-on' : kind === 'warn' ? 'flag-warn' : 'flag-off');
}

/** 状态行文本 + 颜色类 */
export function setStatus(el, text, kind){
  el.textContent = text;
  el.className = kind === 'ok' ? 'ok' : kind === 'err' ? 'err' : kind === 'warn' ? 'warn' : '';
}

/** 防抖 */
export function debounce(fn, ms = 120){
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/** kHz → 人看的标签：8000 → "8 MHz"、500 → "500 kHz" */
export function mhzLabel(khz){
  const n = Number(khz);
  if (!n) return '';
  return n >= 1000 ? (n / 1000) + ' MHz' : n + ' kHz';
}

/**
 * 给下拉框补一个"当前值"选项。
 * localStorage 里可能存着老版本的值（比如 8000/12000/20000 —— 新候选里没有），
 * 不补的话 select 会显示成空白或跳回第一项，等于**悄悄改了用户的设置**。
 */
export function ensureSelectOption(sel, value, label){
  const v = String(value ?? '');
  if (!sel || v === '' || [...sel.options].some(o => o.value === v)) return;
  const o = document.createElement('option');
  o.value = v;
  o.textContent = label || v;
  sel.appendChild(o);
}

/**
 * 往日志面板追加一行，并把"滚到底"**合并**掉。
 *
 * 🚨 别在每行后面直接写 `el.scrollTop = el.scrollHeight`：读 `scrollHeight` 会强制浏览器
 *    **同步重算样式与布局**，实测**每行约 17 ms**（2026-09-30 真机量：ring 只有 36 条、
 *    面板文本才 1045 字符，照样 16.8 ms/行）。而 `session.log()` 是在发帧 / 刷图 / 回放 /
 *    读回的**热路径里同步调用**的 —— 刷一张图光"刷图开始"那一行就白吃 17 ms，
 *    一口气几十行的场景会被它整片拖住。
 *    这里改成：追加只做写入；滚动用 rAF 合并（一帧最多滚一次），
 *    并且只在用户本来就贴着底部时才自动跟随，翻历史时不会被拽回底部。
 *
 * @param {HTMLElement} el 日志容器（`#pn-log` / `#sp-log`）
 * @param {string} text 行文本
 * @param {string} cls 行样式类（`ok` / `err` / `warn` / `dim`）
 * @param {number} max 最多保留多少行，超出丢最老的
 */
export function appendLogLine(el, text, cls = 'dim', max = 500){
  if (!el) return;
  const d = document.createElement('div');
  d.className = cls;
  d.textContent = text;
  el.appendChild(d);
  while (el.childNodes.length > max) el.removeChild(el.firstChild);
  if (el._stick === undefined) el._stick = true;          // 默认跟随底部
  if (!el._scrollHooked){
    el._scrollHooked = true;
    el.addEventListener('scroll', () => {
      el._stick = (el.scrollHeight - el.clientHeight - el.scrollTop) < 40;
    }, { passive: true });
  }
  if (el._scrollPending) return;                          // 一帧内只滚一次
  el._scrollPending = true;
  requestAnimationFrame(() => {
    el._scrollPending = false;
    if (el._stick) el.scrollTop = el.scrollHeight;
  });
}
