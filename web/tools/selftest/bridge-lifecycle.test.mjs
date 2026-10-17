/**
 * 桥的**生命周期**自测（纯离线：不需要探针 / OpenOCD / 目标板）：
 *   node tools/selftest/bridge-lifecycle.test.mjs
 *
 * 咬的是"桥退出了，调试器子进程还在不在"这件事 —— 用户在 2026-09-30 要求补上：
 * Windows 上父进程退出**不会**带走子进程，桥一退 OpenOCD/JLinkGDBServerCL 就变孤儿占着探针，
 * 之后网页再连就是"被占 / 连不上"（项目里那条老经验的一大半根因）。
 *
 * 三条：
 *   ① `cleanupAndExit()` —— 收到终止信号时的行为（调 stop、只清一次、然后退出）；
 *   ② 两个后端的 `stop()` —— 用**真的子进程**（node -e setInterval）验它真的被收掉；
 *   ③ 主程序路径 —— `node rtt-bridge.mjs` 跑起来时确实挂上了退出钩子（看横幅那句）。
 *
 * ⚠️ Windows 上没法给子进程发"真 Ctrl+C"（Node 的 SIGINT 是 CTRL_C_EVENT，只能由控制台产生），
 *    所以①是直接单测处理函数、②是真收真子进程 —— 两者合起来覆盖了这条链路的全部代码。
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import net from 'node:net';

const here = dirname(fileURLToPath(import.meta.url));
const ROOT = join(here, '..', '..');
const BRIDGE = join(ROOT, 'bridge', 'rtt-bridge.mjs');

let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const alive = ch => !!ch && ch.exitCode === null && ch.signalCode === null;
const waitExit = (ch, ms = 3000) => new Promise(res => {
  if (!alive(ch)) return res(true);
  const t = setTimeout(() => res(false), ms);
  ch.once('exit', () => { clearTimeout(t); res(true); });
});
/** 一个"像调试器一样赖着不走"的子进程（跨平台：就是 node 自己） */
const fakeChild = () => spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });

const { OpenOcdBackend, JLinkBackend, cleanupAndExit } = await import('file://' + BRIDGE.replace(/\\/g, '/') + '?t=' + Date.now());

// ==================================================================== 1
console.log('== 1. cleanupAndExit：收到终止信号时干什么 ==');
{
  let stopped = 0, exited = null;
  const fake = { stop(){ stopped++; } };
  const logs = [];
  const ret = cleanupAndExit('SIGINT', { be: fake, exit: c => { exited = c; }, delayMs: 20, log: s => logs.push(s) });
  ok(ret === true, '第一次调用：开始清理');
  ok(stopped === 1, `收掉了当前后端（stop 调用 ${stopped} 次）`);
  ok(logs.join(' ').includes('SIGINT'), `日志里说明是哪个信号触发的：「${(logs[0] || '').trim().slice(0, 42)}…」`);
  await sleep(80);
  ok(exited === 0, `落地之后以 0 退出（exit(${exited})）`);

  // 连按两次 Ctrl+C / 两个信号连着来：只清一次（否则重复 taskkill 没意义还可能报错）
  const fake2 = { stop(){ stopped++; } };
  const ret2 = cleanupAndExit('SIGTERM', { be: fake2, exit: () => {}, delayMs: 1, log: () => {} });
  ok(ret2 === false && stopped === 1, '再来一个信号：只清一次（第二次直接忽略）');
}

// ==================================================================== 2
console.log('== 2. 后端 stop()：真的把子进程收掉（用真子进程验）==');
{
  const be = new OpenOcdBackend({});
  be.child = fakeChild();
  await sleep(300);
  ok(alive(be.child), `OpenOCD 后端：先起了个"赖着不走"的子进程（pid=${be.child.pid}）`);
  const child1 = be.child;                    // stop() 会把引用清空，先留一份再等它退出
  be.stop();
  const gone = await waitExit(child1);
  ok(gone, 'OpenOCD 后端 stop() → 子进程真的退出了（taskkill /T /F，非 Windows 退回 child.kill()）');
  ok(be.child === null && be.sock === null, 'stop() 之后内部引用清空（不会残留半死状态）');
}

// ==================================================================== 3
console.log('== 3. J-Link 后端 stop()：收子进程 + 删 logger 临时文件 ==');
{
  const be = new JLinkBackend({});
  const tmpFile = join(os.tmpdir(), `rtt-bridge-selftest-${Date.now()}.log`);
  fs.writeFileSync(tmpFile, 'fake rtt stream\n');
  be.mode = 'logger';
  be.file = tmpFile;
  be.child = fakeChild();
  await sleep(300);
  ok(alive(be.child) && fs.existsSync(tmpFile), '先造出"logger 在写临时文件"的现场');
  const child2 = be.child;
  be.stop();
  const gone = await waitExit(child2);
  ok(gone, 'J-Link 后端 stop() → 子进程退出');
  ok(!fs.existsSync(tmpFile), `logger 的 %TEMP% 日志被删掉（1.4MB/s 不删一小时就是 5GB）：${tmpFile.split(/[\\/]/).pop()}`);
}

// ==================================================================== 4
console.log('== 4. 主程序路径：直接跑桥时确实挂了退出钩子 ==');
{
  const src = fs.readFileSync(BRIDGE, 'utf8');
  ok(/for \(const sig of \['SIGINT', 'SIGTERM', 'SIGHUP'\]\)/.test(src) && /process\.on\('exit'/.test(src),
     '源码里三个终止信号 + exit 兜底都挂着');
  // isMain 门里的结构：--doctor 走预检（只读），否则才起服务 + 挂退出钩子
  ok(/if \(isMain\)\{[\s\S]{0,400}?installExitHooks\(\);/.test(src) && /args\.doctor/.test(src),
     'installExitHooks() 在"被当脚本跑"的那条路上被调用（import 时不起服务、不挂钩子）；--doctor 只跑预检');

  // 真起一个桥（随机端口），看横幅 —— 它跑起来就说明主路径 + 钩子都进来了
  const port = 17400 + (process.pid % 90);
  const ch = spawn(process.execPath, [BRIDGE, '--port', String(port), '--host', '127.0.0.1'], { stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  ch.stdout.on('data', d => { out += d; });
  ch.stderr.on('data', d => { out += d; });
  let up = false;
  for (let i = 0; i < 40 && !up; i++){
    await sleep(150);
    up = await new Promise(res => {
      const s = net.connect(port, '127.0.0.1');
      const t = setTimeout(() => { s.destroy(); res(false); }, 400);
      s.once('connect', () => { clearTimeout(t); s.destroy(); res(true); });
      s.once('error', () => { clearTimeout(t); res(false); });
    });
  }
  ok(up, `桥在 127.0.0.1:${port} 起来了`);
  ok(/Ctrl\+C 退出/.test(out) && /子进程一起收掉/.test(out), `横幅讲清了退出行为：「${(out.match(/Ctrl\+C[^\n]*/) || [''])[0].trim()}」`);
  ch.kill();
  await waitExit(ch, 2000);
}

// ==================================================================== 5
console.log('== 5. --doctor 环境预检（只读：不占端口、不起服务）==');
{
  const root = join(here, '..', '..');
  const run = args => new Promise(res => {
    const c = spawn(process.execPath, [BRIDGE, ...args], { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    c.stdout.on('data', d => { out += d; });
    c.stderr.on('data', d => { out += d; });
    c.once('exit', code => res({ out, code }));
  });
  const port = 17410 + (process.pid % 80);
  const good = await run(['--doctor', '--port', String(port), '--target', 'stm32f103']);
  ok(/环境预检/.test(good.out) && /Node\s*:/.test(good.out) && /端口/.test(good.out) && /OpenOCD/.test(good.out),
     '预检把 Node / 端口 / OpenOCD / J-Link 逐项列出来');
  ok(!/rtt-bridge 1\.0\s*\n\s*网页地址/.test(good.out), '预检**不启服务**（没有那几行启动横幅）');
  const free = await new Promise(res => {           // 预检跑完后端口应该还是空的
    const s = net.connect(port, '127.0.0.1');
    const t = setTimeout(() => { s.destroy(); res(true); }, 500);
    s.once('connect', () => { clearTimeout(t); s.destroy(); res(false); });
    s.once('error', () => { clearTimeout(t); res(true); });
  });
  ok(free, '预检不占端口（跑完那口还是空的）');
  ok(good.code === 0 || /要处理/.test(good.out), `退出码与结论一致（code=${good.code}）`);

  const bad = await run(['--doctor', '--port', String(port), '--target', 'nosuchtarget']);
  ok(/没有这个目标/.test(bad.out) && bad.code === 1, `目标不存在 → 指出来并以 1 退出（code=${bad.code}）`);
  ok(/bridge\.config\.json/.test(bad.out), '报错里带上"该改哪个文件"（不是光说失败）');
}

console.log(`\n${fail ? '❌' : '✅'} bridge-lifecycle.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
