/**
 * 探针的浏览器授权一把梭 —— **默认就把"你平时开网页的那个浏览器 profile"补上**。
 *
 *   node tools/selftest/serial-grant.mjs                 # 补：默认 profile 列表 × 默认来源列表，三类授权全写
 *   node tools/selftest/serial-grant.mjs --show          # 看：每个 profile、每个来源下有哪些授权
 *   node tools/selftest/serial-grant.mjs --clean         # 清：删掉换口/换探针留下的过期条目
 *   node tools/selftest/serial-grant.mjs --if-idle        # 只在"该 profile 的浏览器没在跑"时才补（给 Makefile 用）
 *   node tools/selftest/serial-grant.mjs --profile="%TEMP%\edge-rtt-tools-test" --origin=http://127.0.0.1:8899
 *   node tools/selftest/serial-grant.mjs --origins=all   # 来源：把用户自己 profile 里出现过的来源全覆盖一遍
 *
 * 为什么要这个（2026-10 用户现场："web 总是卡在授权，好麻烦" + "弹出来根本没得选"）：
 *   · **Web Serial / WebHID / WebUSB 三类授权都只能人工点一次**，CDP 也管不了
 *     （DeviceAccess 域不覆盖 Web Serial；`Browser.grantPermissions` 没有 'serial' 类型）。
 *   · 授权记录按「**来源 + 设备标识**」存进浏览器 profile，缺一不可：
 *       - 来源 = 页面地址（线上 `https://minichao9901.github.io:443`，本地 `http://127.0.0.1:8899`）；
 *       - serial_chooser_data 记**设备实例 ID**（`...MI_01\8&305E904C&6&0001`）——**换 USB 口 `&6&` 就变**，
 *         于是"昨天还能用，今天弹框里挑不到设备"；
 *       - hid_chooser_data / usb_chooser_data 记 vid/pid/serial（同一支探针换口仍认）。
 *   · 所以别每次手点，**把授权直接写进 profile**：本脚本从你自己 Chrome 的 profile 里把探针的
 *     HID/WebUSB 条目**抄**过来，再补上当前 CDC 口的实例 ID，写进下面这些 profile。
 *
 * 默认改哪些 profile（都能用 --profile= 覆盖/追加）：
 *   · `%TEMP%\edge-rtt-tools-test`    ← **`make open` / `make page-prep` 起的那个窗口**（你平时看的）
 *   · `%TEMP%\chrome-rtt-authorized`  ← 自动化基准脚本（hw-campaign*.mjs）用的
 *   ⚠️ **不碰**你自己日常的 Chrome profile（要用得显式 `--profile=user --force`）。
 *
 * ⚠️ Preferences 是浏览器写的：profile 正在被使用时改会被覆盖 —— 本脚本会先杀掉用该 profile 的
 *    chrome/msedge（`--if-idle` 模式下则跳过并在浏览器关闭后由 `make open` 自动补）。
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawn, execSync } from 'node:child_process';

const ARG = k => { const a = process.argv.find(x => x.startsWith(`--${k}=`)); return a ? a.split('=').slice(1).join('=') : null; };
const MODE = process.argv.includes('--clean') ? 'clean' : process.argv.includes('--show') ? 'show' : 'patch';
const IF_IDLE = process.argv.includes('--if-idle');
const FORCE = process.argv.includes('--force');
const SRC = path.join(process.env.LOCALAPPDATA, 'Google/Chrome/User Data');
const sleep = ms => new Promise(r => setTimeout(r, ms));

/** 默认来源：线上页 + 本地页（localhost 与 127.0.0.1 是两个不同的来源，都会用到）*/
const DEFAULT_ORIGINS = [
  'https://minichao9901.github.io:443',
  'http://127.0.0.1:8899',
  'http://localhost:8899',
];
/** 默认 profile：make open 起的那个 + 自动化脚本用的那个 */
const DEFAULT_PROFILES = [
  path.join(process.env.TEMP, 'edge-rtt-tools-test'),
  path.join(process.env.TEMP, 'chrome-rtt-authorized'),
];
function profiles(){
  const custom = ARG('profile');
  if (custom === 'user'){
    if (!FORCE){ console.error('❌ --profile=user 会改你日常的 Chrome profile（浏览器必须完全退出）。确认请加 --force'); process.exit(1); }
    return [SRC];
  }
  const list = custom ? custom.split(';').map(p => path.resolve(p.replace(/^"|"$/g, ''))) : [];
  return [...new Set([...list, ...DEFAULT_PROFILES])];
}
/** 来源：默认三条；--origins=all 再把用户自己 profile 里出现过的来源全覆盖一遍 */
function origins(){
  const custom = ARG('origin');
  const list = custom ? custom.split(';') : [...DEFAULT_ORIGINS];
  if (ARG('origins') === 'all'){
    try {
      const src = JSON.parse(fs.readFileSync(path.join(SRC, 'Default', 'Preferences'), 'utf8'));
      const exc = src?.profile?.content_settings?.exceptions || {};
      for (const k of ['serial_chooser_data', 'hid_chooser_data', 'usb_chooser_data'])
        for (const org of Object.keys(exc[k] || {})) list.push(org.replace(/,\*$/, ''));
    } catch {}
  }
  return [...new Set(list)].map(o => o.replace(/,\*$/, ''));
}

/** 取当前 CDC 口（MI_01）的实例 ID；顺带写进 tmp/cdc-instance.txt 备查 */
function currentInstance(){
  const out = path.join('tmp', 'cdc-instance.txt');
  spawn('pwsh', ['-NoProfile', '-Command',
    `(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_0D28&PID_0204&MI_01' }).InstanceId | Set-Content -Encoding UTF8 ${out}`],
    { stdio: 'ignore' });
  for (let i = 0; i < 20; i++){
    try { const v = fs.readFileSync(out, 'utf8').trim(); if (v) return v.split(/\r?\n/)[0].trim(); } catch {}
    const t0 = Date.now(); while (Date.now() - t0 < 200) { /* spin */ }
  }
  return '';
}
/** 该 profile 有没有浏览器正在用（有就不能改 Preferences） */
function browserRunningFor(dir){
  try {
    const out = execSync(
      `pwsh -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \\"Name='chrome.exe' OR Name='msedge.exe'\\" | Where-Object { $_.CommandLine -like '*${dir.replace(/'/g, '')}*' } | Measure-Object).Count"`,
      { encoding: 'utf8', timeout: 25000 });
    return Number(String(out).trim()) > 0;
  } catch { return false; }
}
function killBrowsersFor(dir){
  const ps = `Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" | `
    + `Where-Object { $_.CommandLine -like '*${dir.replace(/'/g, '')}*' } | `
    + `ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`;
  try { execSync(`pwsh -NoProfile -Command "${ps.replace(/"/g, '\\"')}"`, { timeout: 25000, stdio: 'ignore' }); } catch {}
}
/** 从用户自己的 Chrome profile 里抄探针的 HID/WebUSB 授权条目（按 vid/pid/serial 记，换口也通用） */
function probeEntries(kind){
  try {
    const src = JSON.parse(fs.readFileSync(path.join(SRC, 'Default', 'Preferences'), 'utf8'));
    const all = src?.profile?.content_settings?.exceptions?.[kind + '_chooser_data'] || {};
    const out = [];
    for (const v of Object.values(all))
      for (const o of (v?.setting?.['chosen-objects'] || []))
        if (/akaLink|MicroLink|CMSIS|DAP/i.test(o.name || '')) out.push(o);
    return out.filter((o, i) => out.findIndex(x => x['serial-number'] === o['serial-number'] && x['product-id'] === o['product-id']) === i);
  } catch { return []; }
}
const nowStr = () => String((Date.now() + 11644473600000) * 1000);

/* ============================================================ --show */
if (MODE === 'show'){
  const inst = currentInstance();
  console.log(`当前 CDC 口：${inst || '(没读到，探针插着吗)'}\n用户 profile 里可抄的探针条目：HID ${probeEntries('hid').length} 条 · WebUSB ${probeEntries('usb').length} 条\n`);
  for (const prof of profiles()){
    const pref = path.join(prof, 'Default', 'Preferences');
    if (!fs.existsSync(pref)){ console.log(`── ${prof}\n   （没有这个 profile）\n`); continue; }
    let exc = {};
    try { exc = JSON.parse(fs.readFileSync(pref, 'utf8'))?.profile?.content_settings?.exceptions || {}; } catch {}
    console.log(`── ${prof}${browserRunningFor(prof) ? '  【浏览器正在用】' : ''}`);
    let n = 0;
    for (const k of ['serial_chooser_data', 'hid_chooser_data', 'usb_chooser_data']){
      for (const [org, v] of Object.entries(exc[k] || {})){
        const objs = v?.setting?.['chosen-objects'] || [];
        if (!objs.length) continue;
        const hasCur = objs.some(o => o.device_instance_id === inst);
        console.log(`   ${k}  ${org}${k === 'serial_chooser_data' && hasCur ? '  ✅ 含当前口' : ''}`);
        for (const o of objs){
          const id = o.device_instance_id
            || `vid=0x${Number(o['vendor-id'] || 0).toString(16)} pid=0x${Number(o['product-id'] || 0).toString(16)} sn=${o['serial-number'] || '—'}`;
          console.log('      ' + id + '  (' + (o.name || '?') + ')');
          n++;
        }
      }
    }
    if (!n) console.log('   （没有探针相关授权）');
    console.log('');
  }
  process.exit(0);
}

const PROFILES = profiles();
const ORIGINS = origins();
const inst = currentInstance();
if (!inst) console.log('⚠️ 没读到 CDC 实例 ID（探针的 CDC 口 MI_01 插着吗？）—— 串口这条会跳过（HID/WebUSB 不受影响）');

let touched = 0, skipped = 0;
for (const prof of PROFILES){
  const prefDir = path.join(prof, 'Default');
  const pref = path.join(prefDir, 'Preferences');
  const running = browserRunningFor(prof);
  if (running && MODE !== 'show'){
    if (IF_IDLE){ console.log(`⏭ ${prof}：浏览器正在用（--if-idle 跳过；下次 make open 会补）`); skipped++; continue; }
    console.log(`── ${prof}：浏览器在用 → 先关掉它（Preferences 归它写）`);
    killBrowsersFor(prof);
    await sleep(2000);
  }
  if (!fs.existsSync(pref)){
    if (MODE === 'clean'){ console.log(`── ${prof}：（没有这个 profile，跳过）`); continue; }
    console.log(`── ${prof}：从用户 Chrome 拷一份 profile 底子`);
    fs.mkdirSync(prefDir, { recursive: true });
    for (const f of ['Local State', 'Default/Preferences', 'Default/Secure Preferences']){
      try { fs.copyFileSync(path.join(SRC, f), path.join(prof, f)); } catch {}
    }
  }
  const cfg = JSON.parse(fs.readFileSync(pref, 'utf8'));
  cfg.profile = cfg.profile || {};
  cfg.profile.content_settings = cfg.profile.content_settings || {};
  cfg.profile.content_settings.exceptions = cfg.profile.content_settings.exceptions || {};
  const exc = cfg.profile.content_settings.exceptions;
  const bucket = (k, key) => {
    exc[k] = exc[k] || {};
    exc[k][key] = exc[k][key] || { last_modified: nowStr(), setting: { 'chosen-objects': [] } };
    exc[k][key].setting = exc[k][key].setting || {};
    exc[k][key].setting['chosen-objects'] = exc[k][key].setting['chosen-objects'] || [];
    return exc[k][key].setting['chosen-objects'];
  };
  console.log(`── ${prof}`);

  if (MODE === 'clean'){
    const isProbe = o => /VID_0D28|akaLink|MicroLink|CMSIS/i.test((o.device_instance_id || '') + (o.name || '')) || Number(o['vendor-id']) === 0x0d28;
    let dropped = 0;
    for (const k of ['serial_chooser_data', 'hid_chooser_data', 'usb_chooser_data']){
      for (const [org, v] of Object.entries(exc[k] || {})){
        const objs = v?.setting?.['chosen-objects'] || [];
        const keep = objs.filter(o => !isProbe(o) || (o.device_instance_id && o.device_instance_id === inst));
        dropped += objs.length - keep.length;
        v.setting['chosen-objects'] = keep;
        if (!keep.length) delete exc[k][org];
      }
    }
    console.log(`   删掉 ${dropped} 条过期探针授权；当前 CDC 口 ${inst || '(没读到)'} 保留`);
  } else {
    for (const org of ORIGINS){
      const key = org + ',*';
      const ser = bucket('serial_chooser_data', key);
      if (inst && !ser.some(o => o.device_instance_id === inst)){
        ser.push({ name: 'akaLinkPro CMSIS-DAP', device_instance_id: inst });
        console.log(`   串口：补当前口 → ${org}`);
      }
      for (const [kind, label] of [['hid', 'HID'], ['usb', 'WebUSB']]){
        const list = bucket(kind + '_chooser_data', key);
        const want = probeEntries(kind);
        if (!want.length){ console.log(`   ⚠️ 用户 profile 里没有可抄的 ${label} 条目（先在页面上手点一次，之后本脚本就能抄）`); continue; }
        for (const o of want){
          if (!list.some(x => x['serial-number'] === o['serial-number'] && x['product-id'] === o['product-id'])){
            list.push(o);
            console.log(`   ${label}：补 vid=0x${Number(o['vendor-id'] || 0).toString(16)} pid=0x${Number(o['product-id'] || 0).toString(16)} → ${org}`);
          }
        }
      }
    }
    const serAll = ORIGINS.map(o => bucket('serial_chooser_data', o + ',*').length).reduce((a, b) => a + b, 0);
    console.log(`   ${ORIGINS.length} 个来源都写好了（串口条目合计 ${serAll} 条）`);
  }
  fs.writeFileSync(pref, JSON.stringify(cfg));
  console.log(`   已写回 ${pref}`);
  touched++;
}
console.log(`\n完成：改了 ${touched} 个 profile${skipped ? `，跳过 ${skipped} 个（浏览器在用）` : ''}；来源：${ORIGINS.join(' · ')}`);
console.log('提示：换 USB 口 / 换探针之后重跑一次即可；`make open` 每次都会自动补（只在浏览器没跑时改）。');
