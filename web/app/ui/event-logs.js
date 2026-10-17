import { toast } from './toast.js';

/** Keep the existing clear handlers and log nodes; add the same tools to each bus log. */
export function initEventLogs(){
  for (const [prefix, label] of [['sp','SPI 日志'], ['pn','屏日志'], ['i2','I2C 日志']]){
    const log = document.getElementById(`${prefix}-log`), clear = document.getElementById(`${prefix}-log-clear`);
    const head = document.createElement('div'); head.className = 'loghead';
    const title = document.createElement('strong'); title.textContent = label;
    const spacer = document.createElement('span'); spacer.className = 'spacer';
    head.append(title, spacer);
    for (const action of ['复制','保存']){
      const button = document.createElement('button'); button.className = 'mini'; button.textContent = action;
      button.addEventListener('click', async () => {
        const text = log.innerText;
        if (!text){ toast('暂无日志', 'warn'); return; }
        try {
          if (action === '复制'){ await navigator.clipboard.writeText(text); toast('日志已复制', 'ok'); }
          else {
            const url = URL.createObjectURL(new Blob([text], { type:'text/plain;charset=utf-8' }));
            const a = document.createElement('a'); a.href = url; a.download = `${prefix}-log.txt`; a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          }
        } catch (e){ toast(`${action}日志失败：${e.message}`, 'err'); }
      });
      head.append(button);
    }
    clear.classList.remove('logclear'); head.append(clear);
    log.before(head);
  }
}
