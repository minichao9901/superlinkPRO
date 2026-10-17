/**
 * cfg 文件选择器（RTT Viewer 与 烧录器 共用）。
 *
 * 为什么不用浏览器的文件选择框：出于安全，`<input type=file>` **只给文件名，不给路径**，
 * 而 OpenOCD 需要的是 `target/stm32f4x.cfg` 这种**相对 scripts 目录**的路径 —— 拿到
 * `stm32f4x.cfg` 是没用的。
 * 所以列表由**桥**提供（桥就跑在本机，能直接看到 OpenOCD 的 scripts 目录），
 * 点「选择…」弹出一个列表让你挑，填进输入框的一定是能用的相对路径。
 * 桥没起也不拦着：会提示你手动填，或先启动桥再来点。
 */
import { BridgeClient } from '../rtt/bridge.js';

const CSS_ID = 'cfg-picker-style';

function ensureStyle(){
  if (document.getElementById(CSS_ID)) return;
  const s = document.createElement('style');
  s.id = CSS_ID;
  s.textContent = `
.cfgpick-mask{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:9999}
.cfgpick{background:var(--bg2,#1e1f24);color:var(--fg,#e6e6e6);border:1px solid var(--line,#3a3d45);border-radius:10px;
  width:min(620px,92vw);max-height:80vh;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.5)}
.cfgpick h3{margin:0;padding:11px 14px;font-size:13.5px;border-bottom:1px solid var(--line,#3a3d45);font-weight:600}
.cfgpick .sub{padding:6px 14px;font-size:11.5px;color:var(--fg2,#9aa0a6);word-break:break-all}
.cfgpick .tools{display:flex;gap:8px;padding:8px 14px}
.cfgpick .tools input{flex:1;min-width:0}
.cfgpick ul{list-style:none;margin:0;padding:4px 6px 8px;overflow:auto;flex:1}
.cfgpick li{padding:5px 9px;border-radius:6px;cursor:pointer;font-size:12.5px;font-family:ui-monospace,Consolas,monospace;white-space:nowrap}
.cfgpick li:hover{background:rgba(255,255,255,.07)}
.cfgpick li.on{background:var(--acc,#2f6feb);color:#fff}
.cfgpick .foot{display:flex;gap:8px;justify-content:flex-end;padding:10px 14px;border-top:1px solid var(--line,#3a3d45)}
.cfgpick .foot button{min-width:88px}
.cfgpick .empty{padding:14px;font-size:12.5px;color:var(--fg2,#9aa0a6);line-height:1.6}
`;
  document.head.appendChild(s);
}

/**
 * 弹出 cfg 选择框。
 * @param {{bridgeUrl?:string, current?:string, multiple?:boolean, title?:string}} opt
 * @returns {Promise<string|null>} 选择好的 cfg 串（逗号分隔）；用户取消返回 null
 */
export function pickCfgs(opt = {}){
  const { bridgeUrl = 'ws://127.0.0.1:17321', current = '', multiple = true, title = '选择 OpenOCD cfg 文件' } = opt;
  ensureStyle();
  return new Promise(resolve => {
    const mask = document.createElement('div');
    mask.className = 'cfgpick-mask';
    mask.innerHTML = `<div class="cfgpick">
      <h3>${title}</h3>
      <div class="sub">列表取自桥所在机器的 OpenOCD scripts 目录；点条目多选（可点多次切换）</div>
      <div class="tools"><input placeholder="过滤，如 stm32f4 / cmsis-dap"><span></span></div>
      <ul></ul>
      <div class="foot"><button class="ok primary" disabled>确定</button><button class="cancel">取消</button></div>
    </div>`;
    document.body.appendChild(mask);
    const $ = sel => mask.querySelector(sel);
    const filter = $('.tools input');
    const ul = $('ul');
    const okBtn = $('.ok');
    const cur = new Set(String(current || '').split(/[,\s;]+/).map(s => s.trim()).filter(Boolean));
    let all = [];

    const close = v => { mask.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = e => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    $('.cancel').onclick = () => close(null);
    mask.onclick = e => { if (e.target === mask) close(null); };
    okBtn.onclick = () => close([...cur].join(','));

    const render = () => {
      const q = filter.value.trim().toLowerCase();
      const list = all.filter(c => !q || c.toLowerCase().includes(q));
      ul.innerHTML = '';
      if (!list.length){
        ul.innerHTML = `<div class="empty">没找到匹配的 cfg。${all.length ? '' : '<br>桥连不上或者找不到 OpenOCD？先双击 <b>bridge/start-bridge.bat</b> 把桥起起来，再来点这里；也可以直接在输入框里手填（如 <code>interface/cmsis-dap.cfg</code>）。'}</div>`;
        return;
      }
      for (const c of list){
        const li = document.createElement('li');
        li.textContent = c;
        if (cur.has(c)) li.className = 'on';
        li.onclick = () => {
          if (cur.has(c)) cur.delete(c);
          else { if (!multiple) cur.clear(); cur.add(c); }
          li.className = cur.has(c) ? 'on' : '';
          okBtn.disabled = cur.size === 0;
        };
        ul.appendChild(li);
      }
    };
    okBtn.disabled = cur.size === 0;
    filter.oninput = render;
    setTimeout(() => filter.focus(), 30);

    // 取列表：桥端 target.cfgs
    (async () => {
      try {
        const bc = new BridgeClient(bridgeUrl);
        await bc.connect({ version: 1 });
        const r = await bc.cfgs();
        bc.close();
        all = r?.cfgs || [];
        const sub = $('.sub');
        if (r?.scripts) sub.textContent = `${all.length} 个 cfg · 来自 ${r.scripts}`;
      } catch (e){
        all = [];
        $('.sub').textContent = `桥连不上（${e?.message || e}）—— 先起 bridge/start-bridge.bat`;
      }
      render();
    })();
  });
}
