/**
 * USB→I2C 转发桥（HID 0x36）—— **纯协议层**，不碰 DOM、不碰 USB。
 *
 * 协议出处（唯一真源，改那边必须同步这里）：
 *   E:\Share\github\akaLinkPro\firmware\application_5301\src\i2c_bridge\i2c_bridge_proto.h
 *   网页侧说明：E:\Share\github\akaLinkPro\docs\web-handoff-i2c-bridge.md
 *   已上板验证的参考实现：E:\Share\github\akaLinkPro\script_test\i2c_bridge_test.py
 *
 * ── 🚨 索引口径（这里最容易错一个字节，三种文档的编号**不是同一套**）────────────
 * proto.h 按**含 Report ID 的 64 B 报文**编号（req[0]=0x01、req[1]=长度、req[2]=0x36…）；
 * 网页侧 `AkaLinkHid.xfer()` 已经把 Report ID 剥掉（WebHID 把它放在 `event.reportId`，
 * 不在 `event.data` 里），所以本文件一律用**网页口径**，两者差 1：
 *
 *   请求  data = [action, ...参数]      → 线上 payload = [长度, 0x36, action, ...参数]
 *   响应  res[0]=长度  res[1]=0x36  res[2]=action 回显  res[3..6]=状态字(u32 小端)  res[7..]=数据
 *
 * 对照（本文件 ↔ proto.h）：res[3..6] ↔ res[4..7]；RESULT 的 err/n/data ↔ res[8]/res[9]/res[10..]。
 * 三重印证：proto.h（+1）、web-handoff 文档（网页口径）、i2c_bridge_test.py 的 `Resp`（已上板验证）。
 *
 * ── 为什么与 SPI 桥（0x35）完全不同 ────────────────────────────────────────
 * I2C 慢、事务小，所以**只走 HID**：不开 bulk 端点、不做 DMA。一次 XFER = 一次完整事务
 * （START … repeated START … STOP），由固件主循环执行（最长 ~5 ms），HID 中断只**登记请求**；
 * 主机发完 XFER 轮询 RESULT 取错误码与数据。**别指望一条 HID 往返就拿到数据。**
 */

export const HID_CMD = 0x36;

/** 动作表（proto.h 的 i2c_hid_action_t） */
export const ACT = {
  STATUS: 0, ENABLE: 1, RESET: 2, SET_CFG: 3, GET_CFG: 4,
  XFER: 5, RESULT: 6, SCAN: 7,
  DBG: 10, PINTEST: 11, BITPROBE: 12,
};
export const ACT_NAME = {
  0: 'STATUS', 1: 'ENABLE', 2: 'RESET', 3: 'SET_CFG', 4: 'GET_CFG',
  5: 'XFER', 6: 'RESULT', 7: 'SCAN', 10: 'DBG', 11: 'PINTEST', 12: 'BITPROBE',
};

/** 状态字（res[3..6]）的位域 */
export const ST = {
  ENABLED: 1 << 0,   // 桥已使能（引脚被 I2C 占用）
  PENDING: 1 << 1,   // 有一次请求已登记、还没执行完 → 继续轮询 RESULT
  BUS_OK: 1 << 2,    // 总线空闲
  SDA: 1 << 3,       // SDA 线电平（控制器线感知，事务中读也安全）
  SCL: 1 << 4,       // SCL 线电平
};
export const ST_SHIFT_ERR = 8;    // bit8..15  最近一次**完成**事务的错误码
export const ST_SHIFT_DONE = 16;  // bit16..23 已完成事务计数（低 8 位，回绕）
export const ST_SHIFT_CMD = 24;   // bit24..31 **本命令**的结果码

/** 错误码（proto.h 的 i2c_status_t） */
export const E = {
  OK: 0, DISABLED: 1, BUSY: 2, NO_ADDR: 3, NO_ACK: 4,
  TIMEOUT: 5, RANGE: 6, BAD_FRAME: 7, BUS_STUCK: 8, STATE: 9,
};
export const ERR_NAME = [
  'OK', 'E_DISABLED', 'E_BUSY', 'E_NO_ADDR', 'E_NO_ACK',
  'E_TIMEOUT', 'E_RANGE', 'E_BAD_FRAME', 'E_BUS_STUCK', 'E_STATE',
];

/** 主机侧自造的伪错误码（不会出现在线上，专给"探针没响应/超时"用） */
export const E_HOST_TIMEOUT = 0xfe;

/**
 * 错误码 → 人话 + **下一步该怎么办**。
 * 判读口诀（web-handoff §7）：先跑 PINTEST —— bit16=1 且问题位图=0 ⇒ 桥没问题，去查器件侧。
 */
export function errText(code){
  switch (code){
    case E.OK: return '成功';
    case E.DISABLED: return '桥没使能（先点「使能」，或发 ENABLE 1）';
    case E.BUSY: return '上一条事务还没做完 —— 等一下重发（别并发发 XFER）';
    case E.NO_ADDR: return '地址相位没 ACK：器件不在 / 地址写错 / 没接 / 没供电（先扫描，再跑接线自检）';
    case E.NO_ACK: return '数据相位被 NACK：器件拒收（写保护、写周期还没结束、寄存器不可写）';
    case E.TIMEOUT: return '超时（器件把 SCL 拉住了？换低一档速率试试）';
    case E.RANGE: return '参数越界（写 ≤51 B、读 ≤54 B、子地址 ≤4 B、flags 必须 0）';
    case E.BAD_FRAME: return '报文不合法（未知 action / 保留位非 0）';
    case E.BUS_STUCK: return '总线被拉死（SDA 或 SCL 常低）→ 点「总线恢复」再试';
    case E.STATE: return '其它状态错误（控制器复位后仍不可用）';
    case E_HOST_TIMEOUT: return '主机侧等 RESULT 超时（探针没做完；探针可能已失联，试试重连/拔插）';
    default: return `未知错误码 ${code}`;
  }
}
export const errName = code => ERR_NAME[code] || `E_${code}`;

// ============================================================================
// 尺寸上限（proto.h I2C_WR_MAX / I2C_RD_MAX / I2C_XFER_HDR / I2C_SCAN_*）
// ============================================================================
export const WR_MAX = 51;          // 单次写数据字节
export const RD_MAX = 54;          // 单次读数据字节
/**
 * 一次**逻辑读**的自动分片上限。
 * 单次 54 B 是 HID 报文的限制（响应侧只有 56 B 装数据），属于实现细节 —— 填命令的人
 * 不该每次都自己拆。所以 `rd 0x50 0x00 256` 会被 `session.readLong()` 自动拆成 5 笔、
 * 拼回一整块再交出去（日志只出一行）。写**不**自动分片：EEPROM 页写跨页会绕回页首，
 * 自动拆是危险的（见 docs/i2c-page.md）。
 */
export const RD_TOTAL_MAX = 4096;
export const ADDR_MAX = 4;         // 子地址字节数
export const XFER_HDR = 9;         // flags/dev/addr_len/wr_len/rd_len/addr(4)
export const SCAN_FIRST = 0x08;
export const SCAN_LAST = 0x77;
export const SCAN_BYTES = 14;      // ceil((0x77-0x08+1)/8)
export const STAT_WORDS = 10;
export const CFG_SIZE = 16;
/** HID 报文一次能装的分析仪数据（响应侧 res[7..62] = 56 B，减去 err/n 两个字节） */
export const RESULT_DATA_MAX = 54;

// ============================================================================
// 组包（返回 data 数组 = `[action, ...参数]`，直接喂给 AkaLinkHid.xfer(HID_CMD, data)）
// ============================================================================

export const actStatus = () => Uint8Array.of(ACT.STATUS);
export const actReset = () => Uint8Array.of(ACT.RESET);
export const actGetCfg = () => Uint8Array.of(ACT.GET_CFG);
export const actResult = () => Uint8Array.of(ACT.RESULT);
export const actScan = () => Uint8Array.of(ACT.SCAN);
export const actDbg = () => Uint8Array.of(ACT.DBG);
export const actPinTest = () => Uint8Array.of(ACT.PINTEST);

/** ENABLE：req[4] = 0 关 / 1 开。**开**才配引脚 + 初始化 I2C；关会把两根脚放成高阻输入 */
export const actEnable = on => Uint8Array.of(ACT.ENABLE, on ? 1 : 0);

/** BITPROBE（实验性）：纯 GPIO 位翻转发一次 START+地址+STOP。**别用它判器件在不在** */
export const actBitProbe = dev => Uint8Array.of(ACT.BITPROBE, dev & 0x7f);

/**
 * 配置块 → 16 B（**按显式字节偏移打包，别按结构体对齐猜**）：
 *   [0..3] scl_hz(u32 LE)  [4] pullup  [5] retries  [6] flags(必须 0)  [7] 保留
 *   [8..11] actual_scl_hz（只读）  [12..15] 保留
 */
export function packCfg({ sclHz = 100000, pullup = 0, retries = 0 } = {}){
  const b = new Uint8Array(CFG_SIZE);
  const dv = new DataView(b.buffer);
  dv.setUint32(0, sclHz >>> 0, true);
  b[4] = pullup ? 1 : 0;
  b[5] = Math.max(0, Math.min(8, retries | 0));
  b[6] = 0;
  return b;
}

/** SET_CFG：req[4..19] = 配置块 16 B。注意 `scl_hz` 是**档位选择器**不是精确频率 */
export function actSetCfg(cfg){
  const data = new Uint8Array(1 + CFG_SIZE);
  data[0] = ACT.SET_CFG;
  data.set(packCfg(cfg), 1);
  return data;
}

/**
 * XFER：一次事务。参数区 60 B。
 *
 * 子地址是**一串字节，不是 u32** —— `addr[0]` 先发。
 * （早期固件按"u32 小端取低 addr_len 字节、MSB 在前"解释，addr_len=1 时发出去的恒是 0x00，
 *   读 EEPROM 永远从地址 0 开始；上游已修，但主机侧也别再犯。）
 *
 * @param {{dev:number, addr?:number[]|Uint8Array, wr?:number[]|Uint8Array, rd?:number, flags?:number}} x
 * @returns {Uint8Array} data（`[action, flags, dev, addr_len, wr_len, rd_len, a0..a3, ...wr]`）
 */
export function actXfer(x){
  const dev = (x.dev ?? 0) & 0x7f;
  const addr = Array.from(x.addr || []);
  const wr = Array.from(x.wr || []);
  const rd = x.rd | 0;
  if (x.dev != null && (x.dev & 0x80)) throw new Error(`从机地址 0x${(x.dev | 0).toString(16)} 超过 7 位（bit7 必须为 0）`);
  if (addr.length > ADDR_MAX) throw new Error(`子地址 ${addr.length} B 超过 ${ADDR_MAX} B`);
  if (wr.length > WR_MAX) throw new Error(`写数据 ${wr.length} B 超过 ${WR_MAX} B（分片写）`);
  if (rd > RD_MAX) throw new Error(`读长度 ${rd} B 超过 ${RD_MAX} B（分片读，或按地址自增办法分片）`);
  const data = new Uint8Array(1 + XFER_HDR + wr.length);
  data[0] = ACT.XFER;
  data[1] = (x.flags ?? 0) & 0xff;      // 保留，必须 0
  data[2] = dev;
  data[3] = addr.length;
  data[4] = wr.length;
  data[5] = rd;
  for (let i = 0; i < ADDR_MAX; i++) data[6 + i] = addr[i] ?? 0;
  data.set(wr, 1 + XFER_HDR);
  return data;
}

// ============================================================================
// 解包
// ============================================================================

/** 状态字 u32 小端（res[3..6]） */
export function statusWord(res){
  return (res[3] | (res[4] << 8) | (res[5] << 16) | (res[6] << 24)) >>> 0;
}

/** 状态字 → 好用的对象 */
export function parseStatus(res){
  const raw = statusWord(res);
  return {
    raw,
    enabled: !!(raw & ST.ENABLED),
    pending: !!(raw & ST.PENDING),
    busOk: !!(raw & ST.BUS_OK),
    sda: !!(raw & ST.SDA),
    scl: !!(raw & ST.SCL),
    lastErr: (raw >>> ST_SHIFT_ERR) & 0xff,
    done: (raw >>> ST_SHIFT_DONE) & 0xff,
    cmdRc: (raw >>> ST_SHIFT_CMD) & 0xff,
  };
}

/** 数据段 res[7..] */
export const dataOf = res => res.subarray(7);

/** RESULT 的响应 → {err, data}（res[7]=错误码、res[8]=长度、res[9..]=数据；出错时 n=0） */
export function parseResult(res){
  const err = res[7] ?? 0xff;
  const n = res[8] ?? 0;
  return { err, n, data: res.subarray(9, 9 + n) };
}

/** GET_CFG 的 16 B → 好用的对象 */
export function parseCfg(bytes){
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return {
    sclHz: dv.getUint32(0, true),
    pullup: bytes[4] || 0,
    retries: bytes[5] || 0,
    flags: bytes[6] || 0,
    actualSclHz: dv.getUint32(8, true),
  };
}

/** STATUS 的 10 × u32 → 计数器对象（按线序读，别按结构体） */
export function parseCounters(bytes){
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const w = i => dv.getUint32(i * 4, true);
  return {
    framesOk: w(0), framesErr: w(1), bytesTx: w(2), bytesRx: w(3),
    nackAddr: w(4), nackData: w(5), timeouts: w(6), busRecover: w(7),
    actualSclHz: w(8), lastTicks: w(9),
  };
}
/** 最近一次事务耗时：MCHTMR ticks（24 MHz）→ µs */
export const ticksToUs = t => t / 24;

/** PINTEST 的 u32 → 好用的对象（判读口诀见 web-handoff §7） */
export function parsePinTest(bytes){
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const v = dv.getUint32(0, true);
  // 🚨 问题位图在 **bit8..15**，不是 bit0..2：直接 `v & 0x01` 会把"空闲 SDA/SCL=1"
  //    这两个正常信号当成故障（实测：桥一切正常却报"SCL 在事务里从未被拉低"）。
  const prob = (v >>> 8) & 0xff;
  const problems = [];
  if (prob & 0x01) problems.push('SCL 在事务里从未被拉低（桥侧驱动异常）');
  if (prob & 0x02) problems.push('空闲 SCL 常低（被拽住 / 短路）');
  if (prob & 0x04) problems.push('空闲 SDA 常低（被拽住 / 短路）');
  return {
    raw: v,
    idleSda: v & 1, idleScl: (v >>> 1) & 1,
    pullupSda: (v >>> 2) & 1, pullupScl: (v >>> 3) & 1,
    droveScl: (v >>> 16) & 1, droveSda: (v >>> 17) & 1,
    prob,
    problems,
    /** bit16=1 且问题位图=0 ⇒ 桥没问题，没 ACK 就往器件侧查 */
    bridgeOk: !!(v & (1 << 16)) && problems.length === 0,
  };
}

/** DBG 的 12 × u32（+ 尾部 busy_rej）→ 带名字的列表 */
export const DBG_NAMES = [
  'I2C CTRL', 'I2C STATUS', 'I2C ADDR', 'I2C CMD', 'I2C SETUP', 'I2C INTEN',
  'PA28 FUNC|PAD', 'PA29 FUNC|PAD', 'cfg.scl_hz', 'actual_scl_hz', 'status 快照', 'done 计数',
];
export function parseDbg(bytes){
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = DBG_NAMES.map((name, i) => ({ name, value: dv.getUint32(i * 4, true) }));
  if (bytes.length >= 52) out.push({ name: 'busy 被拒次数', value: dv.getUint32(48, true) });
  return out;
}

/** SCAN 的 14 B 位图 → 地址数组（bit0 = 0x08） */
export function scanBitmapToAddrs(bm){
  const out = [];
  for (let i = 0; i < bm.length * 8; i++){
    const a = SCAN_FIRST + i;
    if (a > SCAN_LAST) break;
    if (bm[i >> 3] & (1 << (i & 7))) out.push(a);
  }
  return out;
}

// ============================================================================
// 便于显示 / 日志的小工具
// ============================================================================

export const hex2 = v => '0x' + (v & 0xff).toString(16).padStart(2, '0').toUpperCase();
export const hexBytes = (a, sep = ' ') => Array.from(a || []).map(v => v.toString(16).padStart(2, '0').toUpperCase()).join(sep);
export const addr7 = v => '0x' + (v & 0x7f).toString(16).padStart(2, '0').toUpperCase();

/** C 表导出用的字节数组字面量：`(uint8_t[]){0x11, 0x22}`，空则 NULL */
export function cBytes(arr){
  const a = Array.from(arr || []);
  return a.length ? `(uint8_t[]){${a.map(v => hex2(v)).join(', ')}}` : 'NULL';
}

/**
 * 一次事务的**线上形状**（给日志看的一行字）——四种子情况都由固件一支笔完成：
 *   wr=0 rd=0            → START+dev+W+STOP          （地址探测）
 *   wr>0 rd=0            → START+dev+W+子地址+数据+STOP
 *   wr=0 rd>0 addr_len=0 → START+dev+R+读+STOP
 *   其余                 → START+dev+W+子地址+数据 + rSTART+dev+R+读+STOP
 */
export function describeXfer({ dev, addr = [], wr = [], rd = 0 }){
  const a = Array.from(addr);
  const w = Array.from(wr);
  const parts = [];
  if (!a.length && !w.length && !rd) return `探测 ${addr7(dev)}（只问 ACK）`;
  parts.push(`START ${addr7(dev)}W`);
  if (a.length) parts.push(`[${hexBytes(a)}]`);
  if (w.length) parts.push(hexBytes(w));
  if (rd){
    if (a.length || w.length) parts.push('rSTART');
    parts.push(`${addr7(dev)}R`);
    parts.push(`×${rd}`);
  }
  parts.push('STOP');
  return parts.join(' ');
}

/**
 * 大块读的**分片计划**（纯函数，Node 自测直接打；`session.readLong()` 就是按它执行的）。
 *
 * 读跨越 54 B 就必须分片。两种办法：
 *   · `'reset'`（**默认**，每片都重新写子地址）：对 EEPROM、寄存器型器件（MPU6050/ADS1115/
 *     Si5351）全都成立 —— 每片是"START dev+W + 子地址 + rSTART dev+R + 读 + STOP"，
 *     语义上等价于把一次长读拆开，**最稳**，慢一倍。
 *   · `'ptr'`（地址指针自增）：先发一次"把地址指针推到 start"的**零长度写**
 *     （addr=[start], wr=0, rd=0 → 线上就是 `START dev+W + 子地址 + STOP`，
 *     正好是设置地址指针，一个数据字节都不写），再分片 `rd 54、54、…`（每片 addr_len=0），
 *     器件的内部地址指针会在片间自增。**FIFO 型器件只能用这个**（读一次弹一个数，
 *     每片重发子地址会把数据丢光）；反过来，读一次弹一个数的器件也不能用 'reset'。
 *
 * ⚠️ 子地址为空（addr_len=0）时 `'reset'` 退化成"连着发 N 笔纯读" —— 没有子地址可递增，
 *    这是唯一能做的；能不能接上完全看器件自己（多数寄存器型器件会接着上次的指针走）。
 *
 * @returns {{cmds:Array<{addr:number[],wr:number[],rd:number,note:string}>, total:number, mode:string}}
 */
export function planRead(start, len, { mode = 'reset', addrLen } = {}){
  const cmds = [];
  const total = Math.max(0, len | 0);
  const m = mode === 'ptr' ? 'ptr' : 'reset';
  if (!total) return { cmds, total: 0, mode: m };
  const src = Array.from(start || []);
  // 🚨 默认按**实际的子地址字节数**截取，不能固定 1：`planRead([0x00,0x10], 256)` 曾被
  //    截成 [0x00]，第二个地址字节直接丢了（读 AT24C32 那种 2 字节地址的片子会全错）。
  const n = addrLen == null ? src.length : Math.max(0, Math.min(ADDR_MAX, addrLen | 0));
  const base = src.slice(0, n);
  if (m === 'reset'){
    for (let off = 0; off < total; off += RD_MAX){
      cmds.push({ addr: bump(base, off), wr: [], rd: Math.min(RD_MAX, total - off), note: `片@+${off}` });
    }
    return { cmds, total, mode: m };
  }
  if (base.length) cmds.push({ addr: base, wr: [], rd: 0, note: '设地址指针' });
  for (let off = 0; off < total; off += RD_MAX){
    cmds.push({ addr: [], wr: [], rd: Math.min(RD_MAX, total - off), note: `续读@+${off}` });
  }
  return { cmds, total, mode: m };
}

/**
 * 大块写的**分片计划**（纯函数；`session.writeLong()` 按它执行）。
 *
 * 与 `planRead` 对称，但有一处**本质不同**：读可以靠器件的地址指针自增，写不行 ——
 * 每片都必须**自带子地址**（`bump` 过），因为 STOP 之后器件指针回到哪儿是不保证的。
 * 所以这里的每一片线上都是 `START dev+W + 子地址 + 数据 + STOP`。
 *
 * ⚠️ **EEPROM 的页写会回卷**：器件内部只按"页"缓存，一页写超了就从页首重新盖
 *    （AT24C02 页 = 8 B、AT24C32 = 32 B）。所以往 EEPROM 写长块要**同时**给两个参数：
 *      · `pageSize`  = 器件的页大小 → 本函数会把每一片**收窄到不跨页**（真正的防线）；
 *      · `chunkMax`  = 单片数据上限 → 只在没有 pageSize 时单独用。
 *    🚨 **只把 `chunkMax` 设成页大小是不够的**（2026-10 代码审查抓到）：起始地址不是页倍数时，
 *       第一片照样跨页 —— 例如从 0x05 起按 8 B 分片，第一片就是 0x05..0x0C，越过 0x08 那条页界，
 *       器件把 0x08..0x0C 绕回页首写成 0x00..0x04，回读与写入不一致而器件全程老实 ACK。
 *    寄存器型器件（MPU6050 / ADS1115 / Si5351…）没有页，`pageSize` 给 0 即可。
 *
 * @param {number[]} start 子地址（原序字节）
 * @param {Uint8Array|number[]} bytes 要写的完整数据（`offsets` 是它里面的下标）
 * @param {object} [o]
 *   · `addrLen`   子地址字节数（缺省 = `start.length`）
 *   · `chunkMax`  单片数据上限（缺省 `WR_MAX` = 51，即协议上限）
 *   · `pageSize`  器件页大小（0 = 不限，见上）。给了就保证**每一片都落在同一页内**
 *   · `offsets`   只写这些下标（缺省 = 整块）。**相邻下标会合并成一片**，
 *                 中间断开就分成两片（"只写改过的那几个字节"就靠它）
 * @returns {{cmds:Array<{addr:number[],wr:number[],off:number,note:string}>, bytes:number, chunks:number, pageSize:number}}
 */
export function planWrite(start, bytes, { addrLen, chunkMax = WR_MAX, offsets = null, pageSize = 0 } = {}){
  const data = Array.from(bytes || []);
  const src = Array.from(start || []);
  const n = addrLen == null ? src.length : Math.max(0, Math.min(ADDR_MAX, addrLen | 0));
  const base = src.slice(0, n);
  const cap = Math.max(1, Math.min(WR_MAX, chunkMax | 0 || WR_MAX));
  const page = Math.max(0, pageSize | 0);
  // 越界的下标直接丢掉（调用方给的是"改过的下标"，不该因为它整笔失败）
  const offs = (offsets == null ? data.map((_, i) => i) : Array.from(offsets))
    .filter(o => Number.isInteger(o) && o >= 0 && o < data.length)
    .sort((a, b) => a - b);
  const cmds = [];
  for (let i = 0; i < offs.length;){
    const off = offs[i];
    /* 本片最多几个字节：协议上限 cap，**且不许跨页**。
       页内偏移要按**完整的子地址**算（bump 之后的那个地址），不能拿 `off` 当地址用 ——
       起始地址可能是 1 B 也可能是 2 B，而且 bump 会在地址位宽处回绕。 */
    let maxLen = cap;
    if (page > 0){
      const inPage = addrNum(bump(base, off)) % page;
      maxLen = Math.max(1, Math.min(cap, page - inPage));
    }
    let j = i + 1;
    while (j < offs.length && offs[j] === offs[j - 1] + 1 && (offs[j] - off) < maxLen) j++;
    const wr = [];
    for (let k = i; k < j; k++) wr.push(data[offs[k]] & 0xff);
    cmds.push({ addr: bump(base, off), wr, off, note: `片@+${off} × ${wr.length}` });
    i = j;
  }
  return { cmds, bytes: offs.length, chunks: cmds.length, pageSize: page };
}

/** 子地址字节数组（大端）→ 数字。只用来算页边界（例：AT24C32 的 `[0x01,0x05]` → 0x105）*/
export function addrNum(bytes){
  let v = 0;
  for (const b of bytes || []) v = ((v * 256) + (b & 0xff)) >>> 0;
  return v;
}

/** 子地址按"原序字节"做加法（EEPROM 是 8 位地址就只加最低字节） */

export function bump(bytes, delta){
  const a = Array.from(bytes || []);
  let carry = delta | 0;
  for (let i = a.length - 1; i >= 0 && carry > 0; i--){
    const v = (a[i] + (carry & 0xff)) & 0xff;
    carry = (carry >>> 8) + ((a[i] + (carry & 0xff)) > 0xff ? 1 : 0);
    a[i] = v;
  }
  return a;
}

/** 器件地址 → 常见器件名（扫描结果里直接看得出"这是什么"，纯提示，不参与逻辑） */
const KNOWN = [
  [0x50, 0x57, 'AT24Cxx EEPROM'],
  [0x48, 0x4b, 'ADS1115 ADC'],
  [0x68, 0x69, 'MPU6050 / DS1307'],
  [0x60, 0x60, 'Si5351 时钟'],
  [0x3c, 0x3d, 'SSD1306 OLED'],
  [0x76, 0x77, 'BMP280 / BME280'],
  [0x40, 0x40, 'INA219 / Si7021'],
  [0x1e, 0x1e, 'HMC5883L'],
  [0x53, 0x53, 'ADXL345'],
  [0x69, 0x69, 'ITG3200 / MPU6050(AD0=1)'],
];
export function guessDevice(addr){
  for (const [lo, hi, name] of KNOWN) if (addr >= lo && addr <= hi) return name;
  const r = addr & 0x78;
  if (r === 0x70) return 'PCA9685 / 其它 0x7x';
  return '';
}
