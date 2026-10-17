/**
 * 固件文件解析（ELF/HEX/BIN）的离线自测。
 *
 * 🚨 这一份是 2026-10 HPM6800EVK 真机烧录之后的"事故复盘测试"：
 *    HPM SDK 的 flash_xip 镜像把**启动头 `.boot_header`（0x80001000）放在任何 PT_LOAD 之外**，
 *    而 ROM 靠它认镜像。我们原来只按 PT_LOAD 段取内容 → 代码都烧进去了、启动头没烧 →
 *    复位后停在 boot ROM。OpenOCD 是**按节(section)**烧的，所以它烧 ELF 一直没问题。
 *    下面用一个手工构造的 ELF 把这个形状钉住（真机上也复验过：烧完 flash 偏移 0x1000 是 `01 00 f9 fc`）。
 *
 *   node tools/selftest/flash-image.test.mjs
 */
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const url = p => 'file://' + join(root, p).replace(/\\/g, '/');
const { parseElfImage, parseIntelHex, parseFirmware } = await import(url('app/flash/image.js'));

let pass = 0, fail = 0;
const ok = (cond, name, extra = '') => {
  if (cond){ pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); }
};

/**
 * 手工造一个 ELF32 小端：
 *   · 一个 PT_LOAD：VMA 0x01200000 / LMA 0x8000d5d0（模拟"VMA 在 SRAM、LMA 在 flash"的 .data）
 *   · 一个 ALLOC+PROGBITS 节**不在任何段里**，地址 0x80001000（模拟 .boot_header）
 *   · 一个 NOBITS 节（.bss，不该被当成数据）
 */
function buildElf(){
  const ehsize = 52, phentsize = 32, shentsize = 40;
  const loadData = Uint8Array.from([0xaa, 0xbb, 0xcc, 0xdd, 0x11, 0x22, 0x33, 0x44]);      // .data
  const bootData = Uint8Array.from([0x01, 0x00, 0xf9, 0xfc, 0x07, 0x00, 0x00, 0x00]);      // .boot_header
  const phoff = ehsize;
  const dataOff = phoff + phentsize * 1;
  const bootOff = dataOff + loadData.length;
  const shoff = bootOff + bootData.length;
  const shnum = 4;                                     // NULL + .data + .boot_header + .bss
  const buf = new Uint8Array(shoff + shentsize * shnum);
  const dv = new DataView(buf.buffer);
  buf.set([0x7f, 0x45, 0x4c, 0x46, 1, 1, 1, 0], 0);
  dv.setUint16(16, 2, true); dv.setUint16(18, 243, true); dv.setUint32(20, 1, true);
  dv.setUint32(24, 0x8000d5d0, true);                  // e_entry
  dv.setUint32(28, phoff, true); dv.setUint32(32, shoff, true);
  dv.setUint16(40, ehsize, true); dv.setUint16(42, phentsize, true); dv.setUint16(44, 1, true);
  dv.setUint16(46, shentsize, true); dv.setUint16(48, shnum, true); dv.setUint16(50, 0, true);
  // PT_LOAD
  dv.setUint32(phoff + 0, 1, true);
  dv.setUint32(phoff + 4, dataOff, true);
  dv.setUint32(phoff + 8, 0x01200000, true);           // p_vaddr（SRAM）
  dv.setUint32(phoff + 12, 0x8000d5d0, true);          // p_paddr（flash LMA）
  dv.setUint32(phoff + 16, loadData.length, true);
  dv.setUint32(phoff + 20, loadData.length, true);
  dv.setUint32(phoff + 24, 4, true);
  // 节表
  const put = (i, nameOff, type, flags, addr, off, size) => {
    const o = shoff + i * shentsize;
    dv.setUint32(o + 0, nameOff, true); dv.setUint32(o + 4, type, true);
    dv.setUint32(o + 8, flags, true); dv.setUint32(o + 12, addr, true);
    dv.setUint32(o + 16, off, true); dv.setUint32(o + 20, size, true);
    dv.setUint32(o + 24, 0, true); dv.setUint32(o + 28, 0, true);
    dv.setUint32(o + 32, 4, true); dv.setUint32(o + 36, 0, true);
  };
  put(0, 0, 0, 0, 0, 0, 0);
  put(1, 1, 1, 0x3, 0x01200000, dataOff, loadData.length);     // .data：ALLOC|WRITE，PROGBITS，在段里
  put(2, 7, 1, 0x2, 0x80001000, bootOff, bootData.length);     // .boot_header：ALLOC，PROGBITS，**不在段里**
  put(3, 20, 8, 0x3, 0x012001d8, 0, 64);                       // .bss：NOBITS（不该被烧）
  buf.set(loadData, dataOff);
  buf.set(bootData, bootOff);
  return buf;
}

console.log('== 1. ELF：按节取 + VMA→LMA 换算 ==');
{
  const segs = parseElfImage(buildElf());
  const byAddr = Object.fromEntries(segs.map(s => ['0x' + s.addr.toString(16), Array.from(s.data)]));
  ok(!!byAddr['0x8000d5d0'], '段内节用 p_paddr 换算出的 LMA（0x8000d5d0，而不是 VMA 0x01200000）',
     JSON.stringify(Object.keys(byAddr)));
  ok(!!byAddr['0x80001000'], '**不在任何 PT_LOAD 里的节也要烧**（.boot_header → 0x80001000）',
     JSON.stringify(Object.keys(byAddr)));
  ok(byAddr['0x80001000'] && byAddr['0x80001000'].join(',') === '1,0,249,252,7,0,0,0',
     '.boot_header 的内容原样带过来（01 00 f9 fc = HPM 启动头魔数）');
  ok(!Object.keys(byAddr).some(k => k === '0x12001d8'), 'NOBITS（.bss）不会被当成数据烧进去');
  ok(segs.every(s => s.data.length > 0), '不产生空段');
}

console.log('== 2. 退化情形 ==');
{
  // 没有节表的裸 ELF（shoff=0）→ 退回按段取，别报错
  const elf = buildElf();
  new DataView(elf.buffer).setUint32(32, 0, true);
  const segs = parseElfImage(elf);
  ok(segs.length === 1 && segs[0].addr === 0x8000d5d0, '没有节表时退回按 PT_LOAD 取（至少能烧代码）');
  let threw = false;
  try { parseElfImage(Uint8Array.from([1, 2, 3, 4])); } catch { threw = true; }
  ok(threw, '不是 ELF 就报错（不静默返回空）');
}

console.log('== 3. Intel HEX 与 .bin ==');
{
  const hex = [':10010000214601360121470136007EFE09D2190140', ':00000001FF'].join('\n');
  const segs = parseIntelHex(hex);
  ok(segs.length === 1 && segs[0].addr === 0x0100 && segs[0].data.length === 16, 'HEX 记录解析出地址与 16 B 数据');
  let threw = false;
  try { parseFirmware('a.bin', new Uint8Array(4), 0); } catch { threw = true; }
  ok(threw, '.bin 不给基地址就报错（不猜地址）');
  const b = parseFirmware('a.bin', Uint8Array.from([1, 2, 3, 4]), 0x08000000);
  ok(b.length === 1 && b[0].addr === 0x08000000, '.bin 用调用方给的基地址');
}

console.log('== 4. HEX 完整性：校验和 / 长度 / 段重叠（2026-10 代码审查）==');
{
  /* 🚨 以前这两样都不查：坏数据照烧，而"回读校验"比对的正是这份坏数据 → 界面显示校验通过。 */
  const good = ':10010000214601360121470136007EFE09D2190140\n:00000001FF';
  ok(parseIntelHex(good).length === 1, '好文件照常解析');

  // ① 数据位翻错（0x21 → 0x20），校验和就不再是 0
  let msg = '';
  try { parseIntelHex(good.replace('2146', '2046')); } catch (e){ msg = e.message; }
  ok(/校验和/.test(msg), '数据位翻错 → 报"校验和不对"', msg);

  // ② 行被截断：长度字段说 16 B，实际只有 3 B 数据 + 校验字节
  msg = '';
  try { parseIntelHex(':10010000214601' + '40\n:00000001FF'); } catch (e){ msg = e.message; }
  ok(/长度不对/.test(msg), '行被截断 → 报"长度不对"（不会把校验字节当数据收下）', msg);

  // ③ 报错要带**行号**，不然几千行的 HEX 没法查
  //    （注意 EOF 记录会让解析提前 break，所以坏行要排在 EOF 之前）
  msg = '';
  try { parseIntelHex([':0400000000010203F6', ':0400040004050607E3', ':00000001FF'].join('\n')); }
  catch (e){ msg = e.message; }
  ok(/第 2 行/.test(msg), '报错带行号（这里应是第 2 行）', msg);

  // ④ 地址重叠的两段 → 直接报错（老代码会把后一段接到前一段末尾，地址静默错位）
  const over = [':0400000000010203F6', ':0400020004050607E4', ':00000001FF'].join('\n');
  msg = '';
  try { parseIntelHex(over); } catch (e){ msg = e.message; }
  ok(/重叠/.test(msg), '地址重叠的记录 → 报"重叠"而不是悄悄挪地址', msg);

  // ⑤ 正常相邻的段仍然照合（别把合法文件也拦了）
  const adj = [':0400000000010203F6', ':0400040004050607E2', ':00000001FF'].join('\n');
  const s = parseIntelHex(adj);
  ok(s.length === 1 && s[0].data.length === 8, '相邻不重叠的两段照常合并成 8 B');
}

console.log('== 5. 损坏 ELF 必须在烧录前拒绝 ==');
{
  const rejects = (buf, label) => {
    let message = '';
    try { parseElfImage(buf); } catch (e){ message = e.message; }
    ok(/ELF/.test(message), label, message);
  };
  rejects(buildElf().subarray(0, 30), '拒绝截断文件头');
  for (const [field, value, label] of [
    [42, 8, '拒绝过小程序头项'], [46, 8, '拒绝过小节表项'],
  ]){
    const b = buildElf(); new DataView(b.buffer).setUint16(field, value, true); rejects(b, label);
  }
  const table = buildElf();
  rejects(table.subarray(0, table.length - 1), '拒绝截断节表，不忽略最后一节');
  const section = buildElf(), sdv = new DataView(section.buffer);
  sdv.setUint32(sdv.getUint32(32, true) + 2 * 40 + 20, section.length, true);
  rejects(section, '有正常代码节时也拒绝损坏启动头');
  const stripped = buildElf(), pdv = new DataView(stripped.buffer);
  pdv.setUint32(32, 0, true); pdv.setUint32(52 + 16, stripped.length, true);
  rejects(stripped, '没有节表时拒绝截断 PT_LOAD，不接受 slice 截短');
  const address = buildElf(); new DataView(address.buffer).setUint32(52 + 12, 0xfffffffc, true);
  rejects(address, '拒绝加载段跨 u32 边界');
  const framed = new Uint8Array(table.length + 9); framed.set(table, 5);
  ok(parseElfImage(framed.subarray(5, 5 + table.length)).length === 2, '带 byteOffset 的正常 ELF 保持正确');
}
// F103CB 的 -funwind-tables ELF 使用 ARM_EXIDX；它不是 PROGBITS，但必须加载。
for (const [type, label] of [[0x70000001, 'ARM_EXIDX 展开表'], [14, 'INIT_ARRAY 初始化数组']]){
  const elf = buildElf(), dv = new DataView(elf.buffer);
  const section = dv.getUint32(32, true) + 2 * 40;
  dv.setUint32(section + 4, type, true);
  const segments = parseElfImage(elf);
  ok(segments.some(s => s.addr === 0x80001000 && s.data[0] === 1 && s.data[3] === 0xfc), `${label} 保留文件内容`);
  dv.setUint32(section + 20, elf.length, true);
  let message = '';
  try { parseElfImage(elf); } catch (e){ message = e.message; }
  ok(/截断|越界/.test(message), `${label} 截断时拒绝烧录`);
}
console.log(`\n${fail ? '❌' : '✅'} flash-image.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
