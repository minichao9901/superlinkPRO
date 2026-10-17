/**
 * 在小端二进制里找常量（用来确认某个外设基址/位模式确实编进固件了）。
 * 用法：node tools/dev/find-const.mjs <file.bin> 0x58000400 0x58024800 ...
 */
import fs from 'node:fs';

const [file, ...consts] = process.argv.slice(2);
if (!file || !consts.length){
  console.log('用法：node tools/dev/find-const.mjs <file.bin> 0x........ [...]');
  process.exit(2);
}
const buf = fs.readFileSync(file);
for (const c of consts){
  const v = Number(c) >>> 0;
  const pat = Buffer.from([v & 0xff, (v >>> 8) & 0xff, (v >>> 16) & 0xff, (v >>> 24) & 0xff]);
  const hits = [];
  let i = buf.indexOf(pat);
  while (i >= 0 && hits.length < 5){ hits.push('0x' + i.toString(16)); i = buf.indexOf(pat, i + 1); }
  console.log(`${c.padEnd(12)} ${hits.length ? '✓ 出现在 ' + hits.join(', ') : '✗ 没找到（可能被折叠/优化掉了）'}`);
}
