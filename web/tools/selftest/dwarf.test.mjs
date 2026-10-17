/**
 * 纯 Node 自测（不需要浏览器、不需要硬件）：
 *   node tools/selftest/dwarf.test.mjs
 * 覆盖：ELF 容器（段表/符号表）+ DWARF 4 变量提取（类型映射、结构体展开、剔除原因）。
 *
 * 基线是 tools/fixtures/dwarf/ 里的**真 ELF 快照**（二进制入库，见那里的 README）。
 * 期望值是"变量契约"，不是"跑出来什么样" —— 地址对不上就是链接结果真的变了。
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const app = join(root, 'app');
const url = p => 'file://' + join(app, p).replace(/\\/g, '/');
const fix = p => join(root, 'tools', 'fixtures', 'dwarf', p);

const { Elf, looksLikeElf } = await import(url('elf/elf.js'));
const { Dwarf, listSampleable, listFromSymtab, SCALARS, DEFAULT_RAM, ramWindowsOf } = await import(url('elf/dwarf.js'));
const hex32 = n => '0x' + (n >>> 0).toString(16).padStart(8, '0');

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name} ${extra}`); }
};
const load = p => new Elf(new Uint8Array(readFileSync(fix(p))));
const hex = n => '0x' + (n >>> 0).toString(16).padStart(8, '0');

// ------------------------------------------------------------------ 1
console.log('== 1. ELF 容器 ==');
{
  const elf = load('stm32f103_scope.elf');
  ok(elf.bits === 32 && elf.le === true, '32 位 + 小端');
  ok(elf.section('.debug_info')?.size > 0, '.debug_info 存在');
  ok(elf.section('.debug_abbrev')?.size > 0, '.debug_abbrev 存在');
  ok(elf.section('.debug_str')?.size > 0, '.debug_str 存在');
  ok(!elf.has('.debug_str_offsets'), '没有 DWARF5 的 .debug_str_offsets（确认是 DWARF 4 的形态）');
  ok(elf.data('.debug_info').length === elf.section('.debug_info').size, 'data() 长度与段表一致');

  const syms = elf.symbols();
  const gp = syms.find(s => s.name === 'g_pack');
  ok(!!gp && gp.isObject, '符号表里有 g_pack 且是 OBJECT');
  ok(gp.addr === 0x20001014 && gp.size === 24, 'g_pack 地址/大小 = 0x20001014 / 24 B',
     `实际 ${hex(gp.addr)} / ${gp.size}`);
  ok(syms.some(s => s.name === 'g_vectors' && s.addr === 0x08000000), 'flash 里的 g_vectors 也在符号表里（后面要滤掉它）');

  // 坏了/不是 ELF 要明确报错，而不是解析出垃圾
  let threw = '';
  try { new Elf(new Uint8Array(200)); } catch (e){ threw = e.message; }
  ok(/不是 ELF|太小/.test(threw), '垃圾字节 → 明确抛错', threw);
  const trunc = new Uint8Array(64);
  trunc.set([0x7f, 0x45, 0x4c, 0x46, 1, 1]);
  threw = '';
  try { new Elf(trunc); } catch (e){ threw = e.message; }
  ok(/段表/.test(threw), '只有 ELF 头没有段表 → 明确抛错', threw);
  ok(looksLikeElf(readFileSync(fix('stm32f103_scope.elf'))) === true, 'looksLikeElf 认得真 ELF');
}

// ------------------------------------------------------------------ 2
console.log('== 2. DWARF：靶子固件的 19 个通道（结构体展开 + 类型映射）==');
{
  const elf = load('stm32f103_scope.elf');
  const r = listSampleable(elf);
  ok(r.source === 'dwarf', '走的是 DWARF 路径');
  ok(r.versions.length === 1 && r.versions[0] === 4, 'DWARF 版本 = 4');
  ok(r.stats.cu === 2, 'CU 数 = 2（main.c / startup.c）', `实际 ${r.stats.cu}`);
  ok(Dwarf.available(elf) === true, 'Dwarf.available 认得出调试信息');

  // 变量契约（地址/大小/类型），与 stm32f103_scope/src/main.c 的表一一对应
  const want = [
    ['g_lfsr',                 0x20000000, 4, 'u32'],
    ['g_far_cnt',              0x20000008, 4, 'u32'],
    ['g_far_sq100',            0x2000000c, 2, 'i16'],
    ['g_isr_count',            0x20001010, 4, 'u32'],
    ['g_pack.f_sin',           0x20001014, 4, 'f32'],
    ['g_pack.f_tri',           0x20001018, 4, 'f32'],
    ['g_pack.i_tick',          0x2000101c, 4, 'i32'],
    ['g_pack.u_ramp',          0x20001020, 2, 'u16'],
    ['g_pack.i_sq1k',          0x20001022, 2, 'i16'],
    ['g_pack.u_cnt',           0x20001024, 1, 'u8'],
    ['g_pack.i_saw',           0x20001025, 1, 'i8'],
    ['g_pack.rsv0',            0x20001026, 1, 'u8'],
    ['g_pack.u_hi',            0x20001028, 4, 'u32'],
    ['g_pair_a',               0x2000102c, 2, 'u16'],
    ['g_pair_b',               0x2000102e, 2, 'u16'],
    ['g_pulse',                0x20001030, 1, 'u8'],
    ['g_ramp64',               0x20001038, 8, 'f64'],
    ['g_sq5k',                 0x20001040, 2, 'i16'],
    ['g_tick',                 0x20001044, 4, 'u32'],
  ];
  const got = r.sampleable.map(v => [v.name, v.addr, v.size, v.scalar]);
  ok(got.length === want.length, `可采样通道数 = ${want.length}`, `实际 ${got.length}`);
  for (const [nm, addr, size, sc] of want){
    const hit = r.sampleable.find(v => v.name === nm);
    ok(!!hit && hit.addr === addr && hit.size === size && hit.scalar === sc,
       `${nm.padEnd(18)} ${hex(addr)} ${size}B ${sc}`,
       hit ? `实际 ${hex(hit.addr)} ${hit.size}B ${hit.scalar}` : '没找到');
  }
  // 结构体成员必须都带上"哪来的"
  ok(r.sampleable.filter(v => v.group === 'g_pack').length === 9, 'g_pack 展开出 9 个成员（含保留字节 rsv0）');
  ok(r.sampleable.every(v => v.path === v.name), '每个通道都带完整路径（g_pack.f_sin）');

  // 采不了的要**带原因**
  const hole = r.skipped.find(s => s.name === 'g_hole');
  ok(!!hole && /数组/.test(hole.reason), 'g_hole（4096 B 数组）被剔除并说明是数组', hole?.reason);
  ok(!r.sampleable.some(v => v.name.startsWith('g_hole')), 'g_hole 不在可采样列表里');
  ok(r.sampleable.every(v => v.addr >= DEFAULT_RAM[0] && v.addr < DEFAULT_RAM[1]), '所有通道都落在 RAM 窗口内');
  ok(!r.sampleable.some(v => /SIN100|g_vectors/.test(v.name)), 'rodata（SIN100）与向量表（g_vectors）都被滤掉');

  // 类型表与 HID 0x32 的编码一致
  ok(SCALARS.f32.code === 6 && SCALARS.f64.code === 7 && SCALARS.u8.code === 0, 'SCALARS 的 code 与协议表一致');
  ok(Object.keys(SCALARS).length === 8, '类型表 8 种（u8/i8/u16/i16/u32/i32/f32/f64）');
}

// ------------------------------------------------------------------ 3
console.log('== 3. 没有 DWARF 时的退化路径（符号表）==');
{
  const elf = load('stm32f103_scope.elf');
  const r = listFromSymtab(elf);
  ok(r.source === 'symtab', 'source = symtab');
  const gp = r.sampleable.find(v => v.name === 'g_pack');
  ok(!!gp && gp.addr === 0x20001014 && gp.size === 24 && gp.scalar === null,
     'g_pack 在列（24 B）但**类型未知**（要用户手选）');
  ok(r.skipped.some(s => s.name === 'g_vectors' && /RAM/.test(s.reason)), 'flash 里的符号被剔除并说明不在 RAM');
  ok(!r.sampleable.some(v => v.name === '_estack'), '_estack（栈顶符号）也被 RAM 窗口滤掉');
}

// ------------------------------------------------------------------ 3.5
console.log('== 3.5. DWARF 5（GCC 11+ 默认）：间接表单 strx/addrx/line_strp ==');
{
  // 夹具是**真 ELF**（riscv32 gcc 13.2 -gdwarf-5，见 fixtures/dwarf/README.md）：
  // 结构体 g_v 展开成 6 个带类型的成员、g_updates/g_flag 是标量、g_tri_buf 是数组（剔除）。
  const elf = load('riscv_dwarf5.elf');
  const r = listSampleable(elf);
  ok(r.source === 'dwarf' && r.note === '', `DWARF 5 直接解析成功（source=${r.source}${r.note ? ' note=' + r.note : ''}）`);
  ok(r.versions.length === 1 && r.versions[0] === 5, `版本识别为 ${JSON.stringify(r.versions)}`);
  const names = r.sampleable.map(v => v.name);
  ok(names.includes('g_v.f_sin') && names.includes('g_v.i_sq1k') && names.includes('g_v.ramp'),
     '结构体成员展开出来了：' + names.filter(n => n.startsWith('g_v.')).join(', '));
  const fs2 = r.sampleable.find(v => v.name === 'g_v.f_sin');
  ok(fs2 && fs2.scalar === 'f32' && fs2.size === 4, `g_v.f_sin 类型正确（${fs2?.scalar} ${fs2?.size} B）`);
  const i32v = r.sampleable.find(v => v.name === 'g_v.i_sq1k');
  ok(i32v && i32v.scalar === 'i32', `g_v.i_sq1k 有符号类型正确（${i32v?.scalar}）`);
  ok(r.sampleable.some(v => v.name === 'g_updates') && r.sampleable.some(v => v.name === 'g_flag'),
     '普通全局标量也在列表里（g_updates / g_flag）');
  ok(r.skipped.some(s => s.name === 'g_tri_buf' && /数组/.test(s.reason)), '数组 g_tri_buf 被剔除并说明原因');
  // RAM 窗口是"问 ELF 自己的可写节"得来的（这份夹具的数据段就在 0x8000xxxx，不是 0x2xxxxxxx）
  ok(Array.isArray(r.ram) && r.ram.some(([lo]) => lo <= 0x80001000), '可写节给出的窗口覆盖了夹具里的变量地址');
}

// ------------------------------------------------------------------ 3.6
console.log('== 3.6. 解析不了时必须退回符号表（别整盘失败）==');
{
  // 故意把 CU 头的 debug_abbrev_offset 指到 .debug_abbrev 之外 → 解析必炸
  const raw = new Uint8Array(readFileSync(fix('riscv_dwarf5.elf')));
  const dv = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const info = new Elf(raw).section('.debug_info');
  dv.setUint32(info.off + 8, 0x00ffffff, true);          // length(4)|version(2)|unit_type(1)|addr_size(1) → 8
  const r = listSampleable(new Elf(raw));
  ok(r.source === 'symtab', `坏文件 → 退回符号表（source=${r.source}）`);
  ok(/DWARF 解析失败/.test(r.note || ''), '退回时给出原因：' + (r.note || '（没有 note）'));
  ok(r.sampleable.length > 0, `退回后照样列得出符号（${r.sampleable.length} 个）`);
}

// ------------------------------------------------------------------ 3.7
console.log('== 3.7. RAM 窗口按 ELF 可写节自动识别（RISC-V 的 ILM/SRAM 不在 0x2xxxxxxx）==');
{
  const w = ramWindowsOf(new Elf(new Uint8Array(readFileSync(fix('stm32f103_scope.elf')))));
  ok(w.some(([lo, hi]) => lo <= 0x20000000 && hi > 0x20000000) || w.some(([lo]) => lo === DEFAULT_RAM[0]),
     `STM32 的窗口仍然覆盖 0x20000000（${w.map(([a, b]) => hex32(a) + '~' + hex32(b)).join(' / ')}）`);
  const fake = { sections: () => [
    { name: '.text', type: 1, flags: 0x6, addr: 0x80000000, size: 0x1000 },      // ALLOC|EXEC，不算 RAM
    { name: '.data', type: 1, flags: 0x3, addr: 0x01200000, size: 0x400 },       // ALLOC|WRITE → RAM
    { name: '.bss',  type: 8, flags: 0x3, addr: 0x01200400, size: 0x400 },       // NOBITS + WRITE → RAM
  ] };
  const w2 = ramWindowsOf(fake);
  ok(w2.some(([lo, hi]) => lo <= 0x01200000 && hi >= 0x01200800),
     `RISC-V 风格的 0x01200000 被认成 RAM（${w2.map(([a, b]) => hex32(a) + '~' + hex32(b)).join(' / ')}）`);
}

// ------------------------------------------------------------------ 4
console.log('== 4. 真工程风格：多 CU + typedef 结构体 + DW_AT_specification ==');
{
  const elf = load('stm32f103_rtt_speed.elf');
  const r = listSampleable(elf);
  ok(r.source === 'dwarf' && r.versions[0] === 4, 'DWARF 4，走 DWARF 路径');
  const want = [
    ['g_bytes',                      0x20000000, 'u32'],
    ['g_loops',                      0x20000004, 'u32'],
    ['g_ms',                         0x20000008, 'u32'],
    ['_SEGGER_RTT.MaxNumUpBuffers',  0x2000001c, 'i32'],
    ['_SEGGER_RTT.MaxNumDownBuffers', 0x20000020, 'i32'],
  ];
  for (const [nm, addr, sc] of want){
    const hit = r.sampleable.find(v => v.name === nm);
    ok(!!hit && hit.addr === addr && hit.scalar === sc, `${nm.padEnd(30)} ${hex(addr)} ${sc}`,
       hit ? `实际 ${hex(hit.addr)} ${hit.scalar}` : '没找到');
  }
  // 这一条是关键：定义 DIE 无名，名字/类型在"声明"那侧 —— 不追 specification 就整块漏掉
  ok(r.skipped.some(s => s.name === '_SEGGER_RTT.acID'), '追到了声明里的成员名（_SEGGER_RTT.acID）');
  ok(r.skipped.some(s => /_SEGGER_RTT\.aUp/.test(s.name)), '_SEGGER_RTT.aUp（数组）也带原因列出');
  ok(!r.skipped.some(s => /^\(无名变量/.test(s.name)), '没有"无名变量"这种漏网（说明 specification 追上了）');
}

// ------------------------------------------------------------------ 5
console.log('== 5. 稳健性：不崩、不乱、原因可读 ==');
{
  const elf = load('stm32f103_scope.elf');
  const r = listSampleable(elf);
  ok(r.skipped.every(s => typeof s.reason === 'string' && s.reason.length > 0), '每个被剔除的条目都有原因文案');
  ok(r.skipped.some(s => /位置列表|寄存器|栈帧/.test(s.reason)), '被优化掉的局部变量给出"没有固定地址"类原因');

  // 重复调用要给同样的结果（索引/缓存不能互相污染）
  const again = listSampleable(load('stm32f103_scope.elf'));
  ok(JSON.stringify(again.sampleable) === JSON.stringify(r.sampleable), '两次解析结果完全一致');

  // 自定义 RAM 窗口：把窗口收窄到一个不覆盖任何变量的区间 → 全部被剔除
  const narrow = listSampleable(load('stm32f103_scope.elf'), { ram: [0x30000000, 0x30001000] });
  ok(narrow.sampleable.length === 0 && narrow.skipped.some(s => /RAM 窗口/.test(s.reason)),
     'RAM 窗口可配置（收窄后全部剔除并说明原因）');
}

// ------------------------------------------------------------------ 6
console.log('== 6. 表单解析不能调不存在的方法（审查：DW_FORM_ref_sig8 → r.skip 崩溃）==');
{
  // DWARF4 的 type unit 表单（GCC -fdebug-types-section 会出现）：解析不了没关系，
  // 但**必须把 8 字节跳过**，不能抛 "r.skip is not a function" 把整个解析带崩。
  const fakeElf = { data: n => (n === '.debug_info' ? new Uint8Array([0]) : new Uint8Array(0)) };
  const d = new Dwarf(fakeElf);
  let bytesArg = 0, advanced = 0;
  const reader = { o: 0, bytes(n){ bytesArg = n; this.o += n; return new Uint8Array(n); },
                   u8(){ this.o += 1; return 0; }, u16(){ this.o += 2; return 0; },
                   u32(){ this.o += 4; return 0; }, u64(){ this.o += 8; return 0; },
                   uleb(){ this.o += 1; return 0; }, sleb(){ this.o += 1; return 0; } };
  let threw = null, res = null;
  try { res = d._value(reader, 0x20 /* DW_FORM_ref_sig8 */, 0, { offset: 0, addrSize: 4 }); }
  catch (e){ threw = e; }
  advanced = reader.o;
  ok(!threw, 'DW_FORM_ref_sig8 不抛异常', threw ? String(threw.message) : '');
  ok(bytesArg === 8 && advanced === 8, `正好跳过 8 字节（bytes(${bytesArg})，游标前进 ${advanced}）`);
  ok(res && res.value === null, '返回"解析不出值"而不是垃圾值');

  // 静态兜底：`_value` 里用到的每个 reader 方法都必须在 R 类里真的存在
  // （这条能抓住"改了方法名/写了个不存在的方法"这一类，正是这次那个 bug 的形状）
  const src = readFileSync(join(app, 'elf', 'dwarf.js'), 'utf8');
  const rBody = /class R \{([\s\S]*?)\n\}/.exec(src);
  const methods = new Set([...rBody[1].matchAll(/^\s{2}([a-zA-Z_]\w*)\s*\(/gm)].map(m => m[1]));
  const valueBody = /_value\(r, form, ic, cu\)\{([\s\S]*?)\n  \}/.exec(src)[1]
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');   // 去掉注释（注释里提到过 r.skip 这个坑）
  const used = new Set([...valueBody.matchAll(/\br\.([a-zA-Z_]\w*)\(/g)].map(m => m[1]));
  const missing = [...used].filter(n => !methods.has(n));
  ok(missing.length === 0, `_value 用到的 reader 方法都存在（用了 ${[...used].join('/')}）`,
     missing.length ? '缺：' + missing.join(', ') : '');
}

console.log(`\n${fail ? '❌' : '✅'} dwarf.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
