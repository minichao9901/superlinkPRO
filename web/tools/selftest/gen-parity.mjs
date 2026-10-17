/**
 * 「工程生成」页的对账自测（纯 Node，不需要浏览器/硬件）：
 *   node tools/selftest/gen-parity.mjs
 *
 * 对账基线 = 用户自己的 Python 工具 uvprojx2cmake.py 的**真实产物**（拷在 tools/fixtures/gen/mdk-arm/）。
 * 验收标准很硬：默认参数下**逐字节相同**（含 CRLF 换行）。
 * 另外还测：LF 模式只差换行、单项覆盖只改那一行、ZIP 能被自己解回来。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, existsSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const app = join(root, 'app');
const fixtures = join(root, 'tools', 'fixtures', 'gen', 'mdk-arm');

const load = p => import('file://' + p.replace(/\\/g, '/'));
const { buildOutputs, PARAM_DEFAULTS, parseUvprojx, suggestFromDevice, matchFamily, normalizeNewlines } = await load(join(app, 'gen', 'model.js'));
const { zipStore, crc32 } = await load(join(app, 'gen', 'zip.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); }
};

// 与 tools/fixtures/gen/mdk-arm 对应的输入参数（取自 Python 工具那次生成时的实际值）
// fixes: null = 不做任何修正 → 这一节要证明的正是"移植是逐字节精确的"
const FIXTURE_PARAMS = {
  projectName: 'MDK-ARM',
  jlinkDevice: 'STM32F103RB',
  flashStart: '0x08000000',
  rttAddress: '0x20002000',
  pyocdTarget: 'stm32f103rb',
  openocdRoot: 'E:/MounRiver/MounRiver_Studio2/resources/app/resources/win32/components/WCH/OpenOCD/OpenOCD',
  openocdInterface: 'interface/cmsis-dap.cfg',
  openocdTarget: 'target/stm32f1x.cfg',
  newline: 'crlf',
  fixes: null,
  checks: { jlink: true, gdb: true, pyocd: true, openocd: true, testBin: true },
};

const EXPEXT = [
  'Makefile.jlink', 'jlink_gdb.script', 'Makefile.pyocd',
  'Makefile.openocd', 'rtt_logger.py', 'test_sram.bin',
];

console.log('== 1. 默认参数 → 与 Python 工具产物逐字节相同 ==');
{
  if (!existsSync(fixtures)){
    fail++; console.log(`  FAIL  找不到对账基线目录 ${fixtures}`);
  } else {
    const files = buildOutputs(FIXTURE_PARAMS);
    ok(files.map(f => f.name).join(',') === EXPEXT.join(','), '产物清单（顺序 + 名字）', files.map(f => f.name).join(','));

    for (const f of files){
      const want = readFileSync(join(fixtures, f.name));
      const got = Buffer.from(f.data);
      if (want.equals(got)){
        ok(true, `${f.name} 逐字节相同（${got.length} B）`);
      } else {
        const n = Math.min(want.length, got.length);
        let at = -1;
        for (let i = 0; i < n; i++) if (want[i] !== got[i]){ at = i; break; }
        if (at < 0 && want.length !== got.length) at = n;
        const ctx = at < 0 ? '' : ` 基线:${JSON.stringify(want.slice(Math.max(0, at - 20), at + 20).toString('latin1'))} 我们:${JSON.stringify(got.slice(Math.max(0, at - 20), at + 20).toString('latin1'))}`;
        ok(false, `${f.name} 逐字节相同`, `长度 ${want.length} vs ${got.length}，首个差异 @${at}${ctx}`);
      }
    }
  }
}

console.log('== 2. 换行符 ==');
{
  const crlf = buildOutputs(FIXTURE_PARAMS).find(f => f.name === 'Makefile.jlink');
  const lf = buildOutputs({ ...FIXTURE_PARAMS, newline: 'lf' }).find(f => f.name === 'Makefile.jlink');
  const crlfText = Buffer.from(crlf.data).toString('latin1');
  const lfText = Buffer.from(lf.data).toString('latin1');
  ok(!crlfText.replace(/\r\n/g, '').includes('\n'), 'CRLF 模式下没有裸 LF');
  ok(!lfText.includes('\r'), 'LF 模式下没有 CR');
  ok(crlfText.replace(/\r\n/g, '\n') === lfText, '两种模式除了换行符完全一样');
  ok(crlf.data.length - lf.data.length === (crlfText.match(/\r\n/g) || []).length, 'CRLF 比 LF 多出的字节数 = 行数');
}

console.log('== 3. 单项覆盖只改那一行 ==');
{
  const base = Buffer.from(buildOutputs(FIXTURE_PARAMS).find(f => f.name === 'Makefile.jlink').data).toString('latin1');
  const tuned = Buffer.from(buildOutputs({ ...FIXTURE_PARAMS, jlinkSpeed: '50000' }).find(f => f.name === 'Makefile.jlink').data).toString('latin1');
  const a = base.split('\r\n'), b = tuned.split('\r\n');
  ok(a.length === b.length, '行数不变', `${a.length} vs ${b.length}`);
  const diff = a.map((l, i) => l === b[i] ? null : i).filter(i => i !== null);
  ok(diff.length === 1, `只改了 1 行（实际 ${diff.length}）`, diff.map(i => `#${i}: ${a[i]} → ${b[i]}`).join(' | '));
  if (diff.length === 1){
    ok(a[diff[0]].includes('JLINK_SPEED ?= 25000') && b[diff[0]].includes('JLINK_SPEED ?= 50000'), '改的正是 JLINK_SPEED 那行', `${a[diff[0]]} → ${b[diff[0]]}`);
  }

  // 默认值显式传入 == 不传（不然"填了默认值"会意外改写产物）
  const same = Buffer.from(buildOutputs({ ...FIXTURE_PARAMS, jlinkSpeed: '25000', gdbPort: '3333', buildDir: 'build' }).find(f => f.name === 'Makefile.jlink').data);
  ok(same.equals(Buffer.from(buildOutputs(FIXTURE_PARAMS).find(f => f.name === 'Makefile.jlink').data)), '填了默认值 = 没填（逐字节）');

  // OpenOCD 频率
  const ob = Buffer.from(buildOutputs(FIXTURE_PARAMS).find(f => f.name === 'Makefile.openocd').data).toString('latin1');
  const ot = Buffer.from(buildOutputs({ ...FIXTURE_PARAMS, openocdFreq: '4000' }).find(f => f.name === 'Makefile.openocd').data).toString('latin1');
  const od = ob.split('\r\n').map((l, i) => l === ot.split('\r\n')[i] ? null : i).filter(i => i !== null);
  ok(od.length === 1 && ot.split('\r\n')[od[0]].includes('OPENOCD_FREQ ?= 4000'), `OpenOCD 频率同样只改 1 行（实际 ${od.length}）`,
     od.map(i => ob.split('\r\n')[i] + ' → ' + ot.split('\r\n')[i]).join(' | '));
}

console.log('== 4. SRAM 测试图案 ==');
{
  const bin = buildOutputs(FIXTURE_PARAMS).find(f => f.name === 'test_sram.bin');
  ok(bin.data.length === 20480, `大小 20480（实际 ${bin.data.length}）`);
  let bad = -1;
  for (let i = 0; i < bin.data.length; i++) if (bin.data[i] !== (i % 256)){ bad = i; break; }
  ok(bad < 0, '整文件都是 i % 256 递增图案', bad < 0 ? '' : `@${bad}`);
  const small = buildOutputs({ ...FIXTURE_PARAMS, testBinSize: '4096' }).find(f => f.name === 'test_sram.bin');
  ok(small.data.length === 4096, 'Size 可改（4096）');
}

console.log('== 5. ZIP 打包（自己解回来验）==');
{
  const files = buildOutputs(FIXTURE_PARAMS).map(f => ({ name: f.name, data: f.data }));
  const when = new Date(2026, 0, 2, 3, 4, 6);
  const zip = zipStore(files, when);
  ok(zip.length > files.reduce((a, f) => a + f.data.length, 0), 'ZIP 比裸数据大（有头 + 目录）');
  // 末尾 22 字节是 EOCD
  const dv = new DataView(zip.buffer, zip.byteOffset, zip.byteLength);
  const tail = zip.length - 22;
  ok(dv.getUint32(tail, true) === 0x06054b50, 'EOCD 签名正确');
  const count = dv.getUint16(tail + 10, true);
  ok(count === files.length, `条目数 ${count} = ${files.length}`);
  const cdOff = dv.getUint32(tail + 16, true);
  ok(dv.getUint32(cdOff, true) === 0x02014b50, '中央目录签名正确');

  // 顺着中央目录把每个文件读出来比对
  let p = cdOff, allOk = true, detail = [];
  const dec = new TextDecoder();
  for (let i = 0; i < count; i++){
    if (dv.getUint32(p, true) !== 0x02014b50){ allOk = false; detail.push(`#${i} 目录项签名错`); break; }
    const crc = dv.getUint32(p + 16, true), size = dv.getUint32(p + 24, true);
    const nameLen = dv.getUint16(p + 28, true), extraLen = dv.getUint16(p + 30, true), cmtLen = dv.getUint16(p + 32, true);
    const lho = dv.getUint32(p + 42, true);
    const name = dec.decode(zip.subarray(p + 46, p + 46 + nameLen));
    const data = zip.subarray(lho + 30 + dv.getUint16(lho + 26, true) + dv.getUint16(lho + 28, true),
                              lho + 30 + dv.getUint16(lho + 26, true) + dv.getUint16(lho + 28, true) + size);
    const want = files[i];
    if (name !== want.name){ allOk = false; detail.push(`#${i} 名字 ${name} ≠ ${want.name}`); }
    else if (crc !== crc32(want.data)){ allOk = false; detail.push(`${name} CRC 不符`); }
    else if (Buffer.compare(Buffer.from(data), Buffer.from(want.data)) !== 0){ allOk = false; detail.push(`${name} 内容不符`); }
    p += 46 + nameLen + extraLen + cmtLen;
  }
  ok(allOk, `6 个文件都能从 ZIP 里原样解回来（${zip.length} B）`, detail.join(' | '));

  // 同一时刻打包两次必须完全一样（时间戳可注入 → 可复现）
  ok(Buffer.compare(Buffer.from(zip), Buffer.from(zipStore(files, when))) === 0, '同时间戳打包结果可复现');
}

console.log('== 6. .uvprojx 解析（拿真实工程文件试）==');
{
  const p = join(root, 'tools', 'fixtures', 'gen', 'uvprojx-sample', 'CubeMX_Config.uvprojx');
  if (!existsSync(p)){
    console.log('  SKIP  没放 uvprojx 样例');
  } else {
    const info = parseUvprojx(readFileSync(p, 'utf8'));
    ok(info.device === 'STM32F103C8', `器件 = STM32F103C8（实际 ${info.device}）`);
    ok(info.cpu.includes('Cortex-M3'), `内核 = Cortex-M3（实际 ${info.cpu}）`);
    ok(info.flashStart === '0x08000000', `Flash 起址 = 0x08000000（dash 写法也要认；实际 ${info.flashStart}）`);
    ok(info.flashSize === '0x10000', `Flash 大小 = 0x10000（实际 ${info.flashSize}）`);
    ok(info.ramStart === '0x20000000' && info.ramSize === '0x5000', `RAM = 0x20000000+0x5000（实际 ${info.ramStart}+${info.ramSize}）`);
    ok(info.targetName === 'CubeMX_Config', `TargetName = CubeMX_Config（实际 ${info.targetName}）`);
    const sug = suggestFromDevice(info.device);
    ok(sug.jlinkDevice === 'STM32F103RB' && sug.pyocdTarget === 'stm32f103rb' && sug.openocdTarget === 'target/stm32f1x.cfg',
       '由器件推出的三项参数（按族映射，与 Python 工具一致）', JSON.stringify(sug));
    ok(matchFamily('STM32F407VGT6') === 'STM32F4xx', '表外型号按前缀归族');
    ok(matchFamily('STM32H7B3VI') === 'STM32H7B3VI', '表内型号精确命中');
  }
}

console.log('== 7. 4 项固定修正（页面默认就套；用户 2026-09-27 逐条采纳）==');
{
  const { applyFixes, FIX_KEYS } = await import('file://' + join(app, 'gen', 'fixes.js'));
  const all = Object.fromEntries(FIX_KEYS.map(k => [k, true]));
  const base = name => normalizeNewlines(readFileSync(join(fixtures, name), 'utf8'));
  const fixed = name => applyFixes(name, base(name), all);
  // 用"行集合"比，不用下标比 —— 有删行时下标会整体错位
  const removed = (a, b) => a.split('\n').filter(l => !b.split('\n').includes(l));
  const added = (a, b) => b.split('\n').filter(l => !a.split('\n').includes(l));

  ok(FIX_KEYS.length === 4, `共 4 项修正（实际 ${FIX_KEYS.length}）`, FIX_KEYS.join(','));

  // ① RTT 范围
  const jl = fixed('Makefile.jlink');
  ok(/RTT_SIZE \?= 0x2000/.test(jl), '① RTT_SIZE 改成 0x2000');
  ok(!/RTT_SIZE \?= 0x5000/.test(jl), '① 没有残留 0x5000');

  // ② clean-jlink 不再删日志
  ok(!/del \*\.log/.test(jl), '② clean-jlink 里没有删 *.log 那行');
  ok(/clean-jlink:/.test(jl) && /del jlink_\*\.script/.test(jl), '② clean-jlink 本身还在、还删临时脚本');

  // ③ openocd-rtt 双引号
  const oc = fixed('Makefile.openocd');
  const rttLine = oc.split('\n').find(l => l.includes('rtt server start')) || '';
  ok(rttLine.includes('-c "init;') && rttLine.includes('\\"SEGGER RTT\\"') && !rttLine.includes("-c '"),
     '③ openocd-rtt 用双引号 + 内层引号转义', rttLine.slice(-70));
  ok(!/-c '/.test(oc), "③ 整个 Makefile.openocd 里再没有 -c '…'");

  // ④ 去掉 jlink-swo
  ok(!/jlink-swo/.test(jl), '④ 没有任何 jlink-swo 残留（目标/用法注释/.PHONY/help）');
  ok(!/JLinkSWOViewerCL/.test(jl), '④ JLinkSWOViewerCL 那行也删了');
  ok(/\.PHONY:/.test(jl) && !/\.PHONY:.*jlink-swo/.test(jl), '④ .PHONY 里也清了');

  // 改动范围：少的行 = 7 行删除（用法注释 1 + SWO 目标块 4 + help 1 + 删日志 1）+ 2 行被改写；多的行 = 2 行
  const rmJ = removed(base('Makefile.jlink'), jl), addJ = added(base('Makefile.jlink'), jl);
  ok(rmJ.length === 9, `Makefile.jlink 少了 9 行（7 删除 + 2 改写：RTT_SIZE/.PHONY，实际 ${rmJ.length}）`, rmJ.map(s => JSON.stringify(s)).join(' | '));
  ok(addJ.length === 2 && addJ.some(l => l.includes('RTT_SIZE ?= 0x2000')) && addJ.some(l => l.startsWith('.PHONY:') && !l.includes('jlink-swo')),
     `新增/改写的行 = 2 行（RTT_SIZE + .PHONY，实际 ${addJ.length}）`, addJ.map(s => JSON.stringify(s)).join(' | '));
  const belongs = /jlink-swo|JLinkSWOViewerCL|SWO viewer|SWO Viewer|del \*\.log|^\.PHONY:|^RTT_SIZE \?= 0x5000$/i;
  ok(rmJ.every(l => belongs.test(l)), '少的这些行全部属于这 4 项修正，没有误伤',
     rmJ.filter(l => !belongs.test(l)).map(s => JSON.stringify(s)).join(' | '));

  const rmO = removed(base('Makefile.openocd'), oc), addO = added(base('Makefile.openocd'), oc);
  ok(rmO.length === 1 && addO.length === 1 && rmO[0].includes('rtt server start'), 'Makefile.openocd 只换了 rtt 那一行', `${rmO.length}/${addO.length}`);

  // 其它文件不受影响
  ok(fixed('Makefile.pyocd') === base('Makefile.pyocd'), 'Makefile.pyocd 不受修正影响');
  ok(fixed('jlink_gdb.script') === base('jlink_gdb.script'), 'jlink_gdb.script 不受修正影响');
  ok(fixed('rtt_logger.py') === base('rtt_logger.py'), 'rtt_logger.py 不受修正影响');
  ok(fixed('test_sram.bin') === base('test_sram.bin'), 'test_sram.bin 不受修正影响');

  // 用户自己填过的值要尊重（不能被修正"纠正"回 0x2000）
  const mine = buildOutputs({ ...FIXTURE_PARAMS, fixes: all, rttSize: '0x1000' }).find(f => f.name === 'Makefile.jlink').text;
  ok(/RTT_SIZE \?= 0x1000/.test(mine), '用户把 RTT 范围填成 0x1000 时，修正不覆盖它');
  // 默认参数走页面路径（不传 fixes）= 套修正
  const pageDefault = buildOutputs({ ...FIXTURE_PARAMS, fixes: undefined }).find(f => f.name === 'Makefile.jlink').text;
  ok(/RTT_SIZE \?= 0x2000/.test(pageDefault) && !/jlink-swo/.test(pageDefault), '不传 fixes 时默认就套修正（页面行为）');

  // 模板漂移要报错，不能静默产出
  let threw = false;
  try { applyFixes('Makefile.jlink', 'some unrelated text\n', all); } catch { threw = true; }
  ok(threw, '模板对不上时抛错（不静默产出半成品）');
}

console.log(`\n${fail ? 'FAIL' : 'OK'}  ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
