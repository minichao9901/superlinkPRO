import { BUILD } from '../core/build.js';
import { AkaLinkHid, selectedHidDevice } from '../hid/probe.js';

export function initProbeFirmwareInfo(){
  const button = document.getElementById('btn-probe-info');
  const result = document.getElementById('probe-info-result');
  const version = document.getElementById('web-build-info');
  const read = url => fetch(url, { cache:'no-store', signal:AbortSignal.timeout(3000) })
    .then(response => response.ok ? response.json() : null).catch(() => null);
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
  const bundled = window.__useWebBundle;
  Promise.all([
    bundled ? read(new URL('../site/manifest.json', import.meta.url)) : null,
    local ? read('/etm-launcher.json') : null,
  ]).then(([web, launcher]) => {
    const manifestMatches = web?.outputSha256 && window.__webBundleUrl?.endsWith(web.outputSha256.slice(0, 16));
    const builtAt = manifestMatches && web.builtAt;
    const lines = [`Web 版本　${BUILD}`, `Web 构建时间　${builtAt ? builtAt.replace('T', ' ').replace('Z', ' UTC') : bundled ? '当前构建未提供' : '源码模式'}`];
    if (launcher?.portable){
      lines.push(`Windows 工作台版本　${launcher.version}`);
      if (launcher.builtAt) lines.push(`Windows 构建时间　${launcher.builtAt.replace('T',' ').replace('Z',' UTC')}`);
    }
    version.textContent = lines.join('\n');
  });
  button.addEventListener('click', async () => {
    button.disabled = true;
    result.textContent = '正在读取探针…';
    const client = new AkaLinkHid();
    try {
      const device = selectedHidDevice();
      if (device) await client.open(device);
      else await client.reconnect();
      const { model, fw, sn, buildTime } = await client.firmwareInfo();
      result.textContent = [
        `型号　${model || '未提供'}`,
        `序列号　${sn || '未提供'}`,
        `固件版本　${fw || '未提供'}`,
        `编译时间　${buildTime || '未提供'}`,
      ].join('\n');
    } catch (error){
      result.textContent = error.message;
    } finally {
      try { await client.close(); }
      catch (error){ result.textContent += '\n关闭读取会话失败：' + error.message; }
      button.disabled = false;
    }
  });
}
