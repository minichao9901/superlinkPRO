/**
 * 极简 ZIP 打包（store 模式，不压缩）。
 *
 * 为什么手写：本仓库坚持零依赖（见 README），而"把几个小文件塞进一个 zip 下载"
 * 只需要 CRC32 + 三段固定结构，不值得引一个压缩库。生成的是标准 ZIP，
 * Windows 资源管理器 / 7-Zip / unzip 都能直接解。
 *
 * 用法：zipStore([{ name:'Makefile.jlink', data: uint8 }]) → Uint8Array
 */

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++){
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();

const enc = new TextEncoder();

export function crc32(bytes){
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

/** ZIP 的时间戳是 DOS 格式（1980 起算，秒只有 2 秒精度） */
function dosStamp(d){
  const time = ((d.getHours() & 31) << 11) | ((d.getMinutes() & 63) << 5) | ((d.getSeconds() >> 1) & 31);
  const date = (((d.getFullYear() - 1980) & 127) << 9) | (((d.getMonth() + 1) & 15) << 5) | (d.getDate() & 31);
  return { time, date };
}

/**
 * @param {{name:string, data:Uint8Array|string}[]} files
 * @param {Date} when 时间戳（可注入，便于做逐字节可复现的测试）
 * @returns {Uint8Array}
 */
export function zipStore(files, when = new Date()){
  const { time, date } = dosStamp(when);
  const head = [];       // 本地头 + 数据
  const central = [];    // 中央目录
  let offset = 0;

  for (const f of files){
    const name = enc.encode(f.name);
    const data = f.data instanceof Uint8Array ? f.data : enc.encode(String(f.data ?? ''));
    const crc = crc32(data);

    const lh = new Uint8Array(30 + name.length);
    const lv = new DataView(lh.buffer);
    lv.setUint32(0, 0x04034b50, true);   // 本地文件头签名
    lv.setUint16(4, 20, true);           // 解压所需版本 2.0
    lv.setUint16(6, 0x0800, true);       // 标志位：文件名是 UTF-8
    lv.setUint16(8, 0, true);            // 压缩方法 0 = store
    lv.setUint16(10, time, true);
    lv.setUint16(12, date, true);
    lv.setUint32(14, crc, true);
    lv.setUint32(18, data.length, true); // 压缩后大小（store = 原大小）
    lv.setUint32(22, data.length, true); // 原始大小
    lv.setUint16(26, name.length, true);
    lv.setUint16(28, 0, true);           // 扩展字段长度
    lh.set(name, 30);
    head.push(lh, data);

    const ch = new Uint8Array(46 + name.length);
    const cv = new DataView(ch.buffer);
    cv.setUint32(0, 0x02014b50, true);   // 中央目录项签名
    cv.setUint16(4, 20, true);           // 生成程序版本
    cv.setUint16(6, 20, true);           // 解压所需版本
    cv.setUint16(8, 0x0800, true);
    cv.setUint16(10, 0, true);
    cv.setUint16(12, time, true);
    cv.setUint16(14, date, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, name.length, true);
    cv.setUint32(42, offset, true);      // 对应本地头的偏移
    ch.set(name, 46);
    central.push(ch);

    offset += lh.length + data.length;
  }

  const cdSize = central.reduce((a, c) => a + c.length, 0);
  const eocd = new Uint8Array(22);
  const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true);     // 中央目录结束记录
  ev.setUint16(8, files.length, true);   // 本盘条目数
  ev.setUint16(10, files.length, true);  // 总条目数
  ev.setUint32(12, cdSize, true);
  ev.setUint32(16, offset, true);        // 中央目录起始偏移
  // 注释为空

  const all = [...head, ...central, eocd];
  const total = all.reduce((a, c) => a + c.length, 0);
  const out = new Uint8Array(total);
  let p = 0;
  for (const c of all){ out.set(c, p); p += c.length; }
  return out;
}
