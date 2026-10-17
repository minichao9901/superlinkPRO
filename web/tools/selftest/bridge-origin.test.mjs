/**
 * 桥的 WebSocket **准入**自测（纯离线：不需要探针/OpenOCD/目标板）。
 *
 *   node tools/selftest/bridge-origin.test.mjs
 *
 * 为什么必须测：桥只监听 127.0.0.1 挡不住浏览器 —— 任意网页都能向 ws://127.0.0.1 发起
 * **跨源** WebSocket（CSWSH），而这座桥能读目标内存、还能烧固件。所以 upgrade 阶段必须校验
 * Origin（默认白名单）+ 可选口令，这里用裸 socket 手写握手请求把三种情况都钉住。
 *
 * 自己拉起一个桥实例（随机端口），只做握手，不发任何目标操作 RPC —— 不碰硬件。
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';

const here = dirname(fileURLToPath(import.meta.url));
const BRIDGE = join(here, '..', '..', 'bridge', 'rtt-bridge.mjs');
const PORT = Number(process.env.BRIDGE_TEST_PORT) || 17399;
const TOKEN = 'sk-test-token';

let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

/** 手写一次 WebSocket 握手，返回 { status, body, opened } */
function handshake({ origin, path = '/ws', token, protocol } = {}){
  return new Promise((resolve, reject) => {
    const key = crypto.randomBytes(16).toString('base64');
    const parts = [`GET ${path}${token ? (path.includes('?') ? '&' : '?') + 'token=' + token : ''} HTTP/1.1`,
                   `Host: 127.0.0.1:${PORT}`,
                   'Upgrade: websocket', 'Connection: Upgrade',
                   `Sec-WebSocket-Key: ${key}`, 'Sec-WebSocket-Version: 13'];
    if (origin !== undefined) parts.push('Origin: ' + origin);
    if (protocol) parts.push('Sec-WebSocket-Protocol: ' + protocol);
    const sock = net.connect(PORT, '127.0.0.1');
    let buf = '';
    const done = (out) => { try { sock.destroy(); } catch {} resolve(out); };
    const t = setTimeout(() => done({ status: 0, opened: false, note: '超时' }), 3000);
    sock.on('connect', () => sock.write(parts.join('\r\n') + '\r\n\r\n'));
    sock.on('error', e => { clearTimeout(t); reject(e); });
    sock.on('data', d => {
      buf += d.toString('latin1');
      const m = /^HTTP\/1\.1 (\d+)/.exec(buf);
      if (!m) return;
      const status = Number(m[1]);
      if (status === 101 || buf.includes('\r\n\r\n')){
        clearTimeout(t);
        done({ status, opened: status === 101, head: buf.split('\r\n')[0] });
      }
    });
  });
}

console.log('== 桥的 WebSocket 准入（Origin 白名单 + 口令）==');
const child = spawn(process.execPath, [BRIDGE, '--port', String(PORT), '--token', TOKEN], { stdio: ['ignore', 'pipe', 'pipe'] });
let bootLog = '';
child.stdout.on('data', d => { bootLog += d.toString(); });
child.stderr.on('data', d => { bootLog += d.toString(); });
try {
  // 等它起来
  let up = false;
  for (let i = 0; i < 40 && !up; i++){
    await sleep(150);
    up = await handshake({ origin: 'http://127.0.0.1', token: TOKEN, protocol: 'bearer.' + TOKEN })
      .then(r => r.opened).catch(() => false);
  }
  ok(up, '桥起来了，且白名单 Origin + 正确口令能握手成功', bootLog.slice(0, 200));

  const evil = await handshake({ origin: 'https://evil.example', token: TOKEN });
  ok(evil.status === 403 && !evil.opened, `陌生 Origin 被拒（${evil.head || evil.status}）`);
  const ghpages = await handshake({ origin: 'https://minichao9901.github.io', token: TOKEN });
  ok(ghpages.opened, 'GitHub Pages 那个 Origin 是白名单（线上页面可用）');
  const localPort = await handshake({ origin: 'http://127.0.0.1:8899', token: TOKEN });
  ok(localPort.opened, '本机任意端口的页面也放行（本地静态服务器）');
  const fileNull = await handshake({ origin: 'null', token: TOKEN });
  ok(fileNull.opened, 'file:// 打开的页面（Origin: null）放行');
  const noOrigin = await handshake({ token: TOKEN });
  ok(noOrigin.opened, '不带 Origin 的非浏览器客户端（脚本/curl）放行');

  const badToken = await handshake({ origin: 'http://127.0.0.1' });
  ok(badToken.status === 401 && !badToken.opened, `口令缺失被拒（${badToken.head || badToken.status}）`);
  const wrongToken = await handshake({ origin: 'http://127.0.0.1', token: 'nope' });
  ok(wrongToken.status === 401, '口令不对被拒（401）');
  const qsToken = await handshake({ origin: 'http://127.0.0.1', token: TOKEN });
  ok(qsToken.opened, '口令走 ?token= 也行');
  const protoToken = await handshake({ origin: 'http://127.0.0.1', protocol: 'bearer.' + TOKEN });
  ok(protoToken.opened, '口令走 Sec-WebSocket-Protocol: bearer.<token> 也行');
  const badPath = await handshake({ origin: 'http://127.0.0.1', path: '/nope', token: TOKEN });
  ok(!badPath.opened, '非 /ws 路径不接受升级');
} finally {
  child.kill();
  await sleep(200);
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
